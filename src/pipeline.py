"""Phase orchestration: pull, download, push, verify, reap, reconcile.

Backends are injected so the whole pipeline can be exercised against fakes
without touching the network -- see tests/.

One Pipeline belongs to one account, and `cfg` is that account's Account
object -- Proton session, staging subtree and Immich key in a single value.
Nothing is read from a module-level default, so there is no way to pair one
account's staging directory with another's API key.

`reconcile` and `execute_deletes` are the v2 additions, and they are
deliberately asymmetric: reconcile runs unattended at the end of every sync and
never touches Proton -- it keeps the staged list in step with Immich's trash,
adding what has been trashed and withdrawing what has been restored -- while
execute_deletes, the only destructive code in the pipeline, runs when a human
asks and re-checks every node against Proton before touching it.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import immich as immich_mod
from . import log, proton, state
from .immich import (ImmichAuthError, ImmichClient, ImmichCliUploader,
                     ImmichConfigError, ImmichError, sha1_file)
from .proton import AuthError, ProtonError, RemoteNode


@dataclass
class Stats:
    discovered: int = 0
    changed: int = 0
    unchanged: int = 0
    skipped: int = 0
    downloaded: int = 0
    uploaded: int = 0
    duplicates: int = 0
    verified: int = 0
    skipped_present: int = 0
    purged: int = 0
    failed: int = 0
    quarantined: int = 0
    bytes_downloaded: int = 0
    # v2: the delete queue. `staged` is what reconcile added this pass;
    # `trashed`/`delete_failed`/`delete_skipped` are what execute did.
    staged: int = 0
    # Assets Immich recognised by checksum but held in its trash, brought back
    # into the library rather than recorded as an upload that never happened.
    restored: int = 0
    # Staged rows taken back off the queue because the asset left Immich's
    # trash -- someone restored it, so the Proton original must not be deleted.
    cancelled: int = 0
    trashed: int = 0
    delete_failed: int = 0
    delete_skipped: int = 0
    aborted: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuthFailure(Exception):
    """Raised to surface exit code 2 to the CLI layer."""


class CircuitBreaker:
    """Stop a pass after N consecutive failures.

    Per-asset exponential backoff already exists; this is the global one it
    was missing. The case it is for: Proton starts rate-limiting mid-backfill,
    every file fails in quick succession, and each failure burns an attempt --
    so one bad night quarantines hundreds of files that were never broken.
    `requeue` recovers them, but only once somebody notices.

    Tripping stops the pass, which is the whole point: the rows not reached
    keep their full attempt budget for the next run.
    """

    def __init__(self, threshold: int):
        # 0 disables it. Anything below that is a typo, not a request for a
        # breaker that trips on the first failure.
        self.threshold = max(0, int(threshold))
        self.consecutive = 0
        self.tripped = False

    def record_success(self) -> None:
        self.consecutive = 0

    def record_failure(self) -> bool:
        """Returns True once the pass should stop."""
        self.consecutive += 1
        if self.threshold and self.consecutive >= self.threshold:
            self.tripped = True
        return self.tripped


class Pipeline:
    def __init__(self, cfg, conn: sqlite3.Connection, backend=None, client=None,
                 uploader=None, run_id: str | None = None, dry_run: bool = False):
        # cfg is an Account: one object carrying this identity's Proton
        # session, staging subtree and Immich key. Nothing here reads any of
        # those from anywhere else, which is what stops one account's photos
        # reaching the other's library.
        self.cfg = cfg
        self.account = str(getattr(cfg, "account_name", None) or "default")
        self.conn = conn
        self.dry_run = dry_run
        self.run_id = run_id or time.strftime("%Y%m%dT%H%M%S")
        self._backend = backend
        self._client = client
        self._uploader = uploader
        self.stats = Stats()
        self.auth_ok: bool | None = None
        self.immich_ok: bool | None = None

    # -- lazily constructed collaborators ---------------------------------
    @property
    def backend(self):
        if self._backend is None:
            self._backend = proton.get_backend(self.cfg)
        return self._backend

    @property
    def client(self) -> ImmichClient:
        if self._client is None:
            self._client = ImmichClient(self.cfg)
        return self._client

    @property
    def uploader(self) -> ImmichCliUploader:
        if self._uploader is None:
            self._uploader = ImmichCliUploader(self.cfg)
        return self._uploader

    # -- helpers -----------------------------------------------------------
    def _limits(self, backfill: bool = False) -> tuple[int | None, int | None]:
        section = "backfill" if backfill else "limits"
        return (self.cfg.get(f"{section}.max_files"), self.cfg.get(f"{section}.max_bytes"))

    def _free_bytes(self) -> int:
        try:
            return shutil.disk_usage(str(self.cfg.staging)).free
        except OSError:
            return 0

    def _min_free_bytes(self) -> int:
        return int(float(self.cfg.get("staging.min_free_gb", 20)) * 1e9)

    def _breaker(self) -> CircuitBreaker:
        return CircuitBreaker(self.cfg.get("limits.consecutive_failures", 25))

    def _trip(self, breaker: CircuitBreaker, stage: str) -> None:
        message = (f"{stage} stopped after {breaker.consecutive} consecutive "
                   f"failures; the rows not reached keep their attempts")
        log.error("circuit.tripped", stage=stage,
                  consecutive=breaker.consecutive, threshold=breaker.threshold)
        self.stats.aborted.append(message)

    def _fail(self, node_id: str, stage: str, error: str, previous: str) -> None:
        status = state.mark_failed(
            self.conn, self.account, node_id, stage, error,
            max_attempts=int(self.cfg.get("limits.max_attempts", 5)))
        self.stats.failed += 1
        if status == state.QUARANTINED:
            self.stats.quarantined += 1
        log.transition(node_id, previous, status, stage=stage,
                       error=log.condense(error, 300))

    # -- Phase 1: pull -----------------------------------------------------
    def pull(self, dry_run: bool | None = None) -> Stats:
        dry = self.dry_run if dry_run is None else dry_run
        extensions = list(self.cfg.get("proton.extensions", []) or [])
        excludes = list(self.cfg.get("proton.exclude_globs", []) or [])
        media_prefixes = list(self.cfg.get("proton.media_type_prefixes", []) or [])
        roots = list(self.cfg.get("proton.roots", []) or [])

        try:
            self.auth_ok = self.backend.auth_ok()
        except ProtonError as exc:
            self.auth_ok = False
            raise AuthFailure(str(exc)) from exc
        if not self.auth_ok:
            raise AuthFailure("proton backend reports no valid session")

        for root in roots:
            log.info("pull.root", root=root, backend=self.backend.name)
            try:
                for node in self.backend.walk(root):
                    if not proton.should_include(node, extensions, excludes,
                                                 media_prefixes):
                        self.stats.skipped += 1
                        continue
                    if dry:
                        row = state.get(self.conn, self.account, node.node_id)
                        if row is None:
                            self.stats.discovered += 1
                        elif ((row["remote_size"] or 0) != (node.size or 0)
                              or (row["remote_modified"] or "") != (node.modified or "")):
                            self.stats.changed += 1
                        else:
                            self.stats.unchanged += 1
                        continue

                    result = state.upsert_discovered(
                        self.conn, self.account, node.node_id, node.path,
                        node.name,
                        node.size, node.modified,
                        claimed_sha1=node.sha1, capture_time=node.capture_time)
                    if result == "new":
                        self.stats.discovered += 1
                        log.transition(node.node_id, "-", state.DISCOVERED, path=node.path)
                    elif result == "changed":
                        self.stats.changed += 1
                        log.transition(node.node_id, "*", state.DISCOVERED,
                                       path=node.path, reason="remote changed")
                    else:
                        self.stats.unchanged += 1
            except AuthError as exc:
                self.auth_ok = False
                raise AuthFailure(str(exc)) from exc
            except ProtonError as exc:
                log.error("pull.failed", root=root, error=str(exc)[:300])
                self.stats.aborted.append(f"pull {root}: {exc}")

        if not dry:
            self.conn.commit()
        log.info("pull.done", new=self.stats.discovered, changed=self.stats.changed,
                 unchanged=self.stats.unchanged, skipped=self.stats.skipped, dry_run=dry)
        return self.stats

    # -- Phase 1b: ask Immich before transferring anything -----------------
    def precheck(self, limit: int | None = None, chunk: int = 500) -> Stats:
        """Skip downloading anything Immich already holds.

        Proton hands over a sha1 for every file at discovery time, and Immich
        dedupes on sha1, so the two can be matched before a byte moves. The
        digest is the uploader's claim rather than a server guarantee, which is
        why this is opt-in: a wrong claim that happened to match a different
        asset already in Immich would skip a file that never actually arrived.
        A wrong claim that matches nothing simply downloads as normal.
        """
        rows = state.select_for_precheck(self.conn, self.account, limit=limit)
        if not rows:
            log.info("precheck.nothing_to_do")
            return self.stats

        for start in range(0, len(rows), chunk):
            batch = rows[start:start + chunk]
            try:
                known = self.client.bulk_upload_check(
                    [(r["node_id"], r["claimed_sha1"]) for r in batch])
            except ImmichAuthError as exc:
                raise AuthFailure(f"immich: {exc}") from exc
            except ImmichError as exc:
                # Purely an optimisation: fall back to downloading.
                log.warn("precheck.unavailable", detail=str(exc)[:200])
                return self.stats

            for row in batch:
                hit = known.get(row["node_id"])
                if not (hit and hit.found and hit.asset_id):
                    continue
                if self.dry_run:
                    log.info("precheck.dry_run_present", node_id=row["node_id"])
                    self.stats.skipped_present += 1
                    continue
                state.mark_uploaded(self.conn, self.account, row["node_id"], hit.asset_id,
                                    is_duplicate=True, sha1=row["claimed_sha1"])
                log.transition(row["node_id"], state.DISCOVERED, state.UPLOADED,
                               asset_id=hit.asset_id, reason="already in immich",
                               downloaded=False)
                self.stats.skipped_present += 1
                self.stats.duplicates += 1

        log.info("precheck.done", already_present=self.stats.skipped_present,
                 checked=len(rows))
        return self.stats

    # -- Phase 2: download -------------------------------------------------
    def _ready_path(self, row: sqlite3.Row, sha1: str) -> Path:
        # Bucket by capture date, not by when the file reached Proton: a bulk
        # import stamps every node with the migration date, which would put the
        # entire library in one directory.
        ts = (state.parse_ts(_row_get(row, "capture_time"))
              or state.parse_ts(row["remote_modified"])
              or datetime.now(timezone.utc))
        bucket = self.cfg.ready_dir / ts.strftime("%Y-%m")
        candidate = bucket / row["remote_name"]
        if candidate.exists():
            try:
                if sha1_file(candidate) == sha1:
                    return candidate
            except OSError:
                pass
            stem, ext = os.path.splitext(row["remote_name"])
            suffix = hashlib.sha1(str(row["node_id"]).encode()).hexdigest()[:8]
            candidate = bucket / f"{stem}-{suffix}{ext}"
        return candidate

    def _plan_batches(self, rows: list[sqlite3.Row],
                      batch_size: int) -> list[list[sqlite3.Row]]:
        """Group rows into one-invocation batches.

        `filesystem download path... localFolder` takes any number of source
        paths but **one destination folder**, and the CLI names each file
        itself. Two constraints follow, and both are enforced here rather than
        left to the backend:

        * **One source folder per batch.** Names are unique within a folder,
          so nothing can collide in the destination.
        * **No two files with the same name in one batch.** A name can repeat
          across folders, and an undecryptable name falls back to a node uid,
          so uniqueness is checked rather than assumed. Anything that would
          collide -- or whose name is not a safe basename -- gets a batch of
          its own, which is the v1 behaviour and always safe.

        `batch_size` of 1 restores v1 exactly: one invocation per file.
        """
        if batch_size <= 1:
            return [[row] for row in rows]

        # dict preserves insertion order, so folders stay in the order the
        # selection produced them and the oldest files are still fetched first.
        by_folder: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_folder.setdefault(posixpath.dirname(row["remote_path"] or ""),
                                 []).append(row)

        batches: list[list[sqlite3.Row]] = []
        for group in by_folder.values():
            current: list[sqlite3.Row] = []
            names: set[str] = set()
            for row in group:
                name = row["remote_name"] or ""
                unsafe = (not name) or name != proton.safe_filename(name)
                if unsafe or name in names:
                    batches.append([row])
                    continue
                current.append(row)
                names.add(name)
                if len(current) >= batch_size:
                    batches.append(current)
                    current, names = [], set()
            if current:
                batches.append(current)
        return batches

    def _node_for(self, row: sqlite3.Row) -> RemoteNode:
        return RemoteNode(node_id=row["node_id"], path=row["remote_path"],
                          name=row["remote_name"], size=row["remote_size"],
                          modified=row["remote_modified"], is_folder=False)

    def _promote(self, row: sqlite3.Row, scratch: Path) -> int:
        """Check one downloaded file and move it into ready/. Returns bytes.

        Raises ProtonError or OSError, which the caller charges to the row --
        this is the same set of checks v1 did per file, unchanged by batching.
        """
        size = row["remote_size"] or 0
        actual = scratch.stat().st_size
        if size and actual != size:
            raise ProtonError(f"size mismatch: remote {size} bytes, got {actual}")
        if actual == 0:
            raise ProtonError("downloaded file is empty")
        digest = sha1_file(scratch)
        claimed = _row_get(row, "claimed_sha1")
        if claimed and claimed != digest:
            # Proton reports sha1Verified: false, so the claim is the
            # uploader's word. Our own digest is what Immich gets.
            log.warn("download.digest_mismatch", node_id=row["node_id"],
                     claimed=claimed, actual=digest)
        target = self._ready_path(row, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(scratch, target)  # same filesystem: atomic

        state.mark_downloaded(self.conn, self.account, row["node_id"],
                              str(target), digest)
        log.transition(row["node_id"], state.DOWNLOADING, state.DOWNLOADED,
                       local_path=str(target), sha1=digest, bytes=actual)
        return actual

    def _fetch_batch(self, batch: list[sqlite3.Row],
                     scratch_dir: Path) -> tuple[dict[str, Path], dict[str, str]]:
        """Download a batch, then look at what is actually on disk.

        Returns (landed, errors) keyed by node id. The exit code is never
        trusted on its own: a batch that fails partway still leaves real files
        behind, and throwing those away would mean re-transferring them.

        When the batch call fails, whatever did not land is retried one file
        at a time. That costs an extra pass over the failed batch and buys
        exact attribution -- one unreadable file must not charge an attempt to
        the other forty-nine.
        """
        landed: dict[str, Path] = {}
        errors: dict[str, str] = {}
        batch_error: str | None = None

        try:
            self.backend.download_many([self._node_for(r) for r in batch],
                                       scratch_dir)
        except AuthError as exc:
            self.auth_ok = False
            raise AuthFailure(str(exc)) from exc
        except (ProtonError, OSError) as exc:
            batch_error = str(exc)

        for row in batch:
            candidate = scratch_dir / (row["remote_name"] or "")
            if candidate.is_file():
                landed[row["node_id"]] = candidate

        missing = [r for r in batch if r["node_id"] not in landed]
        if batch_error and missing and len(batch) > 1:
            log.warn("download.batch_failed_retrying_singly",
                     batch=len(batch), missing=len(missing),
                     detail=log.condense(batch_error, 300))
            for row in missing:
                single = (scratch_dir
                          / hashlib.sha1(str(row["node_id"]).encode()).hexdigest()[:12]
                          / (row["remote_name"] or "asset"))
                try:
                    self.backend.download(self._node_for(row), single)
                except AuthError as exc:
                    self.auth_ok = False
                    raise AuthFailure(str(exc)) from exc
                except (ProtonError, OSError) as exc:
                    errors[row["node_id"]] = str(exc)
                    continue
                if single.is_file():
                    landed[row["node_id"]] = single
                else:
                    errors[row["node_id"]] = "download reported success but produced nothing"
        elif batch_error:
            for row in missing:
                errors[row["node_id"]] = batch_error
        else:
            for row in missing:
                errors[row["node_id"]] = (
                    f"not produced by the download "
                    f"(expected {row['remote_name']!r} in the batch folder)")
        return landed, errors

    def download(self, limit: int | None = None, max_bytes: int | None = None,
                 backfill: bool = False) -> Stats:
        default_limit, default_bytes = self._limits(backfill)
        limit = default_limit if limit is None else limit
        max_bytes = default_bytes if max_bytes is None else max_bytes

        floor = self._min_free_bytes()
        free = self._free_bytes()
        if free < floor:
            message = (f"free space {free / 1e9:.1f} GB below floor "
                       f"{floor / 1e9:.1f} GB; skipping downloads")
            log.warn("download.aborted_low_space", free_gb=round(free / 1e9, 1),
                     floor_gb=round(floor / 1e9, 1))
            self.stats.aborted.append(message)
            return self.stats

        rows = state.select_for_download(
            self.conn, self.account, limit=limit, max_bytes=max_bytes,
            max_attempts=int(self.cfg.get("limits.max_attempts", 5)),
            backoff_base_sec=int(self.cfg.get("limits.backoff_base_sec", 300)),
            backoff_cap_sec=int(self.cfg.get("limits.backoff_cap_sec", 86400)),
        )
        if not rows:
            log.info("download.nothing_to_do")
            return self.stats

        incoming = self.cfg.incoming_dir / self.run_id
        if not self.dry_run:
            incoming.mkdir(parents=True, exist_ok=True)

        batch_size = int(self.cfg.get("proton.download_batch_size", 25))
        batches = self._plan_batches(rows, batch_size)
        if batch_size > 1:
            log.info("download.batched", files=len(rows), batches=len(batches),
                     batch_size=batch_size)
        breaker = self._breaker()

        budget = max_bytes
        for index, batch in enumerate(batches):
            # The byte cap is applied at selection time too; this is the
            # backstop for when the real sizes differ from the reported ones.
            if budget is not None and self.stats.downloaded:
                fitted, running = [], 0
                for row in batch:
                    size = row["remote_size"] or 0
                    if running + size > budget:
                        continue
                    fitted.append(row)
                    running += size
                if not fitted:
                    log.info("download.byte_cap_reached",
                             downloaded=self.stats.downloaded)
                    break
                batch = fitted

            batch_bytes = sum(r["remote_size"] or 0 for r in batch)
            if self._free_bytes() - batch_bytes < floor:
                log.warn("download.stopped_low_space",
                         free_gb=round(self._free_bytes() / 1e9, 1))
                self.stats.aborted.append("stopped early: free space floor reached")
                break

            if self.dry_run:
                for row in batch:
                    log.info("download.dry_run", node_id=row["node_id"],
                             path=row["remote_path"])
                    self.stats.downloaded += 1
                continue

            for row in batch:
                state.mark_downloading(self.conn, self.account, row["node_id"])
                log.transition(row["node_id"], row["status"], state.DOWNLOADING,
                               path=row["remote_path"])

            # One scratch folder per batch. The CLI picks the filename and
            # `--conflict-strategy skip` would keep a leftover from an earlier
            # batch, so nothing is ever downloaded into a shared directory.
            scratch_dir = incoming / f"b{index:05d}"
            handled = 0
            try:
                landed, errors = self._fetch_batch(batch, scratch_dir)
                for row in batch:
                    handled += 1
                    node_id = row["node_id"]
                    scratch = landed.get(node_id)
                    if scratch is None:
                        self._fail(node_id, "download",
                                   errors.get(node_id, "download produced nothing"),
                                   state.DOWNLOADING)
                        if breaker.record_failure():
                            break
                        continue
                    try:
                        actual = self._promote(row, scratch)
                    except (ProtonError, OSError) as exc:
                        self._fail(node_id, "download", str(exc), state.DOWNLOADING)
                        if breaker.record_failure():
                            break
                        continue
                    breaker.record_success()
                    self.stats.downloaded += 1
                    self.stats.bytes_downloaded += actual
                    if budget is not None:
                        budget -= actual
            finally:
                # Covers success (files have been moved out), failure and the
                # auth abort alike.
                shutil.rmtree(scratch_dir, ignore_errors=True)

            if breaker.tripped:
                # Rows this batch never got to go back on the queue now, not
                # at the next run's resume(): a pass that stops deliberately
                # should leave no row looking in-flight. No attempt charged --
                # they were never tried.
                state.rewind(self.conn, self.account,
                             [r["node_id"] for r in batch[handled:]])
                self._trip(breaker, "download")
                break

        if not self.dry_run:
            _remove_if_empty(incoming)
        log.info("download.done", downloaded=self.stats.downloaded,
                 failed=self.stats.failed,
                 bytes=self.stats.bytes_downloaded)
        return self.stats

    # -- Phase 3: push -----------------------------------------------------
    def _build_batch(self, rows: Iterable[sqlite3.Row]) -> tuple[Path, dict[str, sqlite3.Row]]:
        """Hardlink the selected files into their own tree.

        Uploading staging/ready wholesale would re-offer everything still inside
        its retention window; a per-run batch keeps a push to exactly the rows
        it selected while preserving relative paths for folder-derived albums.
        """
        batch = self.cfg.batch_dir / self.run_id
        batch.mkdir(parents=True, exist_ok=True)
        mapping: dict[str, sqlite3.Row] = {}
        for row in rows:
            source = Path(row["local_path"])
            try:
                rel = source.relative_to(self.cfg.ready_dir)
            except ValueError:
                rel = Path(source.name)
            dest = batch / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            try:
                os.link(source, dest)
            except OSError:
                shutil.copy2(source, dest)
            mapping[row["node_id"]] = row
        return batch, mapping

    def _relocate(self, row: sqlite3.Row, dry: bool) -> sqlite3.Row | None:
        """Find a staged file whose recorded path went stale.

        Rows staged on bare metal carry /mnt/immich/staging/ready/...; in the
        container the same tree is /staging/<account>/ready/... The part after
        `ready/` is ours (capture month / name), so it is looked for under the
        current ready_dir and accepted only if the sha1 still matches.
        """
        old = row["local_path"]
        if not old or not row["sha1"]:
            return None
        parts = Path(old).parts
        if "ready" not in parts:
            return None
        last_ready = len(parts) - 1 - parts[::-1].index("ready")
        tail = parts[last_ready + 1:]
        if not tail:
            return None
        candidate = self.cfg.ready_dir.joinpath(*tail)
        if str(candidate) == old or not candidate.is_file():
            return None
        try:
            if sha1_file(candidate) != row["sha1"]:
                return None
        except OSError:
            return None
        log.info("push.local_path_relocated", node_id=row["node_id"],
                 old=old, new=str(candidate))
        if dry:
            return None
        state.relocate_local_path(self.conn, self.account, row["node_id"], str(candidate))
        return state.get(self.conn, self.account, row["node_id"])

    def push(self, limit: int | None = None, dry_run: bool | None = None) -> Stats:
        dry = self.dry_run if dry_run is None else dry_run
        rows = state.select_for_upload(
            self.conn, self.account, limit=limit,
            max_attempts=int(self.cfg.get("limits.max_attempts", 5)),
            backoff_base_sec=int(self.cfg.get("limits.backoff_base_sec", 300)),
            backoff_cap_sec=int(self.cfg.get("limits.backoff_cap_sec", 86400)),
        )
        present, missing = [], []
        for row in rows:
            if row["local_path"] and Path(row["local_path"]).exists():
                present.append(row)
                continue
            relocated = self._relocate(row, dry)
            if relocated is not None:
                present.append(relocated)
            else:
                missing.append(row)
        for row in missing:
            # Reaped too early, or removed by hand: send it back for re-download.
            # Failing it on the upload side would only retry the same dead path
            # until the row quarantines.
            error = f"local file missing: {row['local_path']}"
            if dry:
                log.info("push.dry_run_redownload", node_id=row["node_id"],
                         path=row["local_path"])
                continue
            state.send_back_for_download(self.conn, self.account, row["node_id"], error)
            log.warn("push.local_file_missing_redownload", node_id=row["node_id"],
                     path=row["local_path"])
            log.transition(row["node_id"], row["status"], state.FAILED,
                           stage="push", error=error, retry="download")
        rows = present
        if not rows:
            log.info("push.nothing_to_do")
            return self.stats

        try:
            self.immich_ok = self.client.ping()
        except ImmichError:
            self.immich_ok = False

        # Ask the server what it already has before sending anything.
        pending: list[sqlite3.Row] = []
        try:
            known = self.client.bulk_upload_check(
                [(r["node_id"], r["sha1"]) for r in rows])
        except ImmichAuthError as exc:
            raise AuthFailure(f"immich: {exc}") from exc
        except ImmichError as exc:
            log.warn("push.precheck_unavailable", detail=str(exc)[:200])
            known = {}

        # A checksum match is not proof the photo is in the library: Immich
        # dedupes on checksum and answers just as happily for an asset sitting
        # in its trash. Recording that as an upload is what used to fill the
        # delete queue with photos that had never been uploaded at all.
        matched = {row["node_id"]: known[row["node_id"]].asset_id
                   for row in rows
                   if known.get(row["node_id"])
                   and known[row["node_id"]].found
                   and known[row["node_id"]].asset_id}
        trashed, restored = self._resolve_trashed_matches(matched, dry)

        for row in rows:
            hit = known.get(row["node_id"])
            if hit and hit.found and hit.asset_id:
                in_trash = hit.asset_id in trashed
                if in_trash and hit.asset_id not in restored:
                    # Not uploaded, and not restorable. Say so rather than
                    # claim a success -- reconcile will then stage it, which
                    # is the honest outcome when the trash is deliberate.
                    if not dry:
                        self._fail(row["node_id"], "upload",
                                   f"immich already holds this checksum as "
                                   f"asset {hit.asset_id}, but that asset is "
                                   f"in the trash, so the photo is not in the "
                                   f"library. Re-uploading cannot fix it (the "
                                   f"checksum is taken); restore it in Immich, "
                                   f"or set immich.restore_trashed_duplicates",
                                   row["status"])
                    continue
                if dry:
                    log.info("push.dry_run_duplicate", node_id=row["node_id"],
                             restored=in_trash)
                    continue
                state.mark_uploaded(self.conn, self.account, row["node_id"], hit.asset_id, is_duplicate=True)
                log.transition(row["node_id"], row["status"], state.UPLOADED,
                               asset_id=hit.asset_id, duplicate=True,
                               restored=in_trash)
                self.stats.duplicates += 1
                self.stats.uploaded += 1
            else:
                pending.append(row)

        if not pending:
            log.info("push.all_duplicates", duplicates=self.stats.duplicates)
            return self.stats

        if dry:
            log.info("push.dry_run", files=len(pending))
            if self.cfg.get("immich.upload_mode", "cli") == "cli":
                batch, _ = self._build_batch(pending)
                try:
                    self.uploader.upload_dir(batch, dry_run=True)
                finally:
                    shutil.rmtree(batch, ignore_errors=True)
            return self.stats

        for row in pending:
            state.mark_uploading(self.conn, self.account, row["node_id"])
            log.transition(row["node_id"], row["status"], state.UPLOADING)

        mode = self.cfg.get("immich.upload_mode", "cli")
        if mode == "api":
            self._push_via_api(pending)
        else:
            self._push_via_cli(pending)

        log.info("push.done", uploaded=self.stats.uploaded,
                 duplicates=self.stats.duplicates, failed=self.stats.failed)
        return self.stats

    def _resolve_trashed_matches(self, matched: dict[str, str],
                                 dry: bool) -> tuple[set[str], set[str]]:
        """Work out which dedupe matches are in the trash, and revive them.

        Returns (trashed asset ids, ids successfully restored). The trash is
        queried only when there is at least one match to check, so the common
        case -- a run of genuinely new files -- costs nothing.
        """
        if not matched:
            return set(), set()
        try:
            trashed = self.client.trashed_asset_ids()
        except ImmichAuthError as exc:
            raise AuthFailure(f"immich: {exc}") from exc
        except ImmichError as exc:
            # Without the trash list every match looks live, which is the old
            # behaviour. Say so; do not silently resume claiming uploads.
            log.warn("push.trash_check_unavailable",
                     detail=log.condense(str(exc), 300))
            return set(), set()

        hits = {asset for asset in matched.values() if asset in trashed}
        if not hits:
            return trashed, set()

        log.info("push.matched_trashed_assets", assets=len(hits),
                 detail="immich recognises these checksums but the assets are "
                        "in its trash, so the photos are not in the library")
        if dry or not self.cfg.get("immich.restore_trashed_duplicates", True):
            return trashed, set()
        try:
            self.client.restore_from_trash(sorted(hits))
        except ImmichAuthError as exc:
            raise AuthFailure(f"immich: {exc}") from exc
        except ImmichError as exc:
            log.error("push.restore_failed", assets=len(hits),
                      detail=log.condense(str(exc), 300))
            return trashed, set()

        # A 200 is not proof. Ask again: the photo is only in the library if
        # the asset has actually left the trash, and recording an upload that
        # did not happen is what fills the delete queue with photos nobody
        # deleted.
        try:
            trashed = self.client.trashed_asset_ids()
        except ImmichAuthError as exc:
            raise AuthFailure(f"immich: {exc}") from exc
        except ImmichError as exc:
            log.warn("push.restore_unverified", assets=len(hits),
                     detail=log.condense(str(exc), 300))
            return trashed, set()

        restored = {asset for asset in hits if asset not in trashed}
        refused = hits - restored
        if refused:
            log.error(
                "push.restore_ineffective", assets=len(refused),
                detail=("immich accepted the restore and left these in the "
                        "trash. They are failed rather than recorded as "
                        "uploaded; restore them in the Immich UI and requeue"))
        self.stats.restored += len(restored)
        return trashed, restored

    def _push_via_api(self, rows: list[sqlite3.Row]) -> None:
        breaker = self._breaker()
        handled = 0
        for row in rows:
            handled += 1
            try:
                result = self.client.upload_file(
                    row["local_path"], row["sha1"], device_asset_id=row["node_id"])
            except ImmichAuthError as exc:
                raise AuthFailure(f"immich: {exc}") from exc
            except (ImmichError, OSError) as exc:
                self._fail(row["node_id"], "upload", str(exc), state.UPLOADING)
                if breaker.record_failure():
                    break
                continue
            if not result.asset_id:
                self._fail(row["node_id"], "upload", "no asset id returned",
                           state.UPLOADING)
                if breaker.record_failure():
                    break
                continue
            breaker.record_success()
            state.mark_uploaded(self.conn, self.account, row["node_id"], result.asset_id,
                                is_duplicate=result.duplicate)
            log.transition(row["node_id"], state.UPLOADING, state.UPLOADED,
                           asset_id=result.asset_id, duplicate=result.duplicate)
            self.stats.uploaded += 1
            if result.duplicate:
                self.stats.duplicates += 1
        if breaker.tripped:
            # Same as download: put the rows never reached back to
            # `downloaded` now, attempts untouched.
            state.rewind(self.conn, self.account,
                         [r["node_id"] for r in rows[handled:]])
            self._trip(breaker, "push")

    def _push_via_cli(self, rows: list[sqlite3.Row]) -> None:
        batch, mapping = self._build_batch(rows)
        try:
            try:
                run = self.uploader.upload_dir(batch)
                log.debug("push.cli_output", stdout=run.stdout[-1500:],
                          stderr=run.stderr[-500:])
            except ImmichConfigError:
                # Nothing was sent, and no number of retries will fix it.
                # Charging attempts here would quarantine the library over a
                # typo. Rows stay `uploading` and resume() rewinds them.
                raise
            except ImmichAuthError as exc:
                raise AuthFailure(f"immich: {exc}") from exc
            except ImmichError as exc:
                # The batch failed as a unit; charge an attempt to every row.
                for row in rows:
                    self._fail(row["node_id"], "upload", str(exc), state.UPLOADING)
                return

            # The CLI has no stable per-file machine output, so confirm against
            # the server by checksum instead of parsing stdout.
            try:
                landed = self.client.bulk_upload_check(
                    [(r["node_id"], r["sha1"]) for r in rows])
            except ImmichError as exc:
                landed = {}
                log.warn("push.postcheck_unavailable", detail=str(exc)[:200])

            for row in rows:
                hit = landed.get(row["node_id"])
                if not (hit and hit.asset_id):
                    hit = self.client.find_by_checksum(row["sha1"], row["remote_name"])
                if hit and hit.asset_id:
                    state.mark_uploaded(self.conn, self.account, row["node_id"], hit.asset_id,
                                        is_duplicate=False)
                    log.transition(row["node_id"], state.UPLOADING, state.UPLOADED,
                                   asset_id=hit.asset_id)
                    self.stats.uploaded += 1
                else:
                    self._fail(row["node_id"], "upload",
                               "not found on server after upload", state.UPLOADING)
        finally:
            shutil.rmtree(batch, ignore_errors=True)

    # -- Phase 4: verify ---------------------------------------------------
    def verify(self, limit: int | None = None) -> Stats:
        rows = state.select_for_verify(self.conn, self.account, limit=limit)
        if not rows:
            log.info("verify.nothing_to_do")
            return self.stats
        breaker = self._breaker()
        for row in rows:
            node_id = row["node_id"]
            asset_id = row["immich_asset_id"]
            try:
                asset = self.client.get_asset(asset_id) if asset_id else None
                if asset is None:
                    hit = self.client.find_by_checksum(row["sha1"], row["remote_name"])
                    asset = self.client.get_asset(hit.asset_id) if hit.asset_id else None
                    if asset:
                        self.conn.execute(
                            "UPDATE assets SET immich_asset_id=?"
                            " WHERE account=? AND node_id=?",
                            (asset.get("id"), self.account, node_id))
                        self.conn.commit()
            except ImmichAuthError as exc:
                raise AuthFailure(f"immich: {exc}") from exc
            except ImmichError as exc:
                self._fail(node_id, "verify", str(exc), state.UPLOADED)
                if breaker.record_failure():
                    break
                continue

            if not asset:
                self._fail(node_id, "verify", "asset not found server-side",
                           state.UPLOADED)
                if breaker.record_failure():
                    break
                continue
            remote_sum = asset.get("checksum") or (asset.get("exifInfo") or {}).get("checksum")
            if remote_sum and not immich_mod.checksums_match(row["sha1"], remote_sum):
                self._fail(node_id, "verify",
                           f"checksum mismatch (local {row['sha1']}, remote {remote_sum})",
                           state.UPLOADED)
                if breaker.record_failure():
                    break
                continue
            if not remote_sum:
                log.warn("verify.no_remote_checksum", node_id=node_id,
                         asset_id=asset.get("id"))
            breaker.record_success()
            state.mark_verified(self.conn, self.account, node_id,
                                immich_checksum=remote_sum)
            log.transition(node_id, state.UPLOADED, state.VERIFIED,
                           asset_id=asset.get("id"))
            self.stats.verified += 1
        if breaker.tripped:
            self._trip(breaker, "verify")
        log.info("verify.done", verified=self.stats.verified, failed=self.stats.failed)
        return self.stats

    # -- Phase 4b: reap ----------------------------------------------------
    def reap(self, keep_days: int | None = None) -> Stats:
        keep = int(self.cfg.get("reap.keep_days", 7)) if keep_days is None else keep_days
        rows = state.select_for_reap(self.conn, self.account, keep_days=keep)
        for row in rows:
            local = row["local_path"]
            if self.dry_run:
                log.info("reap.dry_run", node_id=row["node_id"], path=local)
                continue
            try:
                if local:
                    Path(local).unlink(missing_ok=True)
            except OSError as exc:
                log.warn("reap.unlink_failed", node_id=row["node_id"], error=str(exc))
                continue
            state.mark_purged(self.conn, self.account, row["node_id"])
            log.transition(row["node_id"], state.VERIFIED, state.PURGED, path=local)
            self.stats.purged += 1

        # Rows matched by claimed digest never had a file; nothing to delete,
        # but they should still reach a terminal state.
        for row in state.select_verified_without_file(self.conn, self.account):
            if self.dry_run:
                continue
            state.mark_purged(self.conn, self.account, row["node_id"])
            log.transition(row["node_id"], state.VERIFIED, state.PURGED,
                           downloaded=False)
            self.stats.purged += 1

        if not self.dry_run:
            _prune_empty_dirs(self.cfg.ready_dir)
            self._clean_scratch()
        log.info("reap.done", purged=self.stats.purged, keep_days=keep)
        return self.stats

    def _clean_scratch(self) -> None:
        """Drop incoming/ and batch/ dirs left behind by killed runs."""
        max_age = timedelta(hours=float(self.cfg.get("reap.scratch_max_age_hours", 48)))
        cutoff = time.time() - max_age.total_seconds()
        for parent in (self.cfg.incoming_dir, self.cfg.batch_dir):
            if not parent.exists():
                continue
            for child in parent.iterdir():
                if not child.is_dir() or child.name == self.run_id:
                    continue
                try:
                    if child.stat().st_mtime < cutoff:
                        shutil.rmtree(child, ignore_errors=True)
                        log.info("reap.scratch_removed", path=str(child))
                except OSError:
                    continue

    # -- Phase 5: reconcile ------------------------------------------------
    def reconcile(self) -> Stats:
        """Stage for deletion anything this account put in Immich and someone
        has since moved to Immich's trash.

        Read-only towards Proton. The only write is a row in `staged_deletes`
        plus the asset's status, and both are idempotent -- a photo sitting in
        Immich's trash for a week is staged once, on the first sync that sees
        it, not once per night.

        Why the list lives here and not in Immich: **Immich empties its trash
        after about 30 days.** A list recomputed from the server on each view
        would silently lose anything not acted on before that purge, while the
        file stayed in Proton with nothing left to say so. Recording on first
        sighting is the whole point of the table.
        """
        if not self.cfg.get("reconcile.enabled", True):
            log.debug("reconcile.disabled")
            return self.stats

        types = list(self.cfg.get("reconcile.types", ["IMAGE", "VIDEO"]) or [])
        try:
            trashed = self.client.search_trashed(
                types=types,
                page_size=int(self.cfg.get("reconcile.page_size", 250)),
                max_pages=int(self.cfg.get("reconcile.max_pages", 400)),
            )
        except ImmichAuthError as exc:
            raise AuthFailure(f"immich: {exc}") from exc
        except ImmichError as exc:
            # Never fail a whole sync over the delete queue: the photos are
            # already safely uploaded, and the next run will scan again.
            log.warn("reconcile.unavailable", detail=log.condense(str(exc), 300))
            self.stats.aborted.append(f"reconcile: {exc}")
            return self.stats

        if not trashed:
            # An empty trash is the strongest possible "nothing should be
            # deleted": every pending row is stale by definition. Returning
            # here without cancelling is what let an emptied trash leave the
            # queue full.
            cancelled = self.cancel_restored(set())
            log.info("reconcile.done", trashed_in_immich=0, staged=0,
                     cancelled=cancelled,
                     total_staged=state.count_staged(self.conn, self.account))
            return self.stats

        by_asset = {str(a.get("id")): a for a in trashed if a.get("id")}
        rows = state.find_by_asset_ids(self.conn, self.account, list(by_asset))
        never_uploaded = 0
        for asset_id, row in rows.items():
            if self.dry_run:
                if row["status"] not in state.DELETE_STATUSES:
                    log.info("reconcile.dry_run_stage", node_id=row["node_id"],
                             path=row["remote_path"])
                    self.stats.staged += 1
                continue
            if state.stage_delete(self.conn, self.account, row):
                self.stats.staged += 1
                if row["is_duplicate"]:
                    never_uploaded += 1
                log.transition(row["node_id"], row["status"],
                               state.STAGED_FOR_DELETE, asset_id=asset_id,
                               path=row["remote_path"], reason="trashed in immich")

        if never_uploaded:
            # The surprising case, and the dangerous one at scale.
            #
            # /assets/bulk-upload-check dedupes on checksum and does not care
            # whether the match is in the trash. So a photo this pipeline has
            # never uploaded can be "recognised" as already present, marked
            # uploaded-as-duplicate, and then staged for deletion by the very
            # next step -- all in one run, on the strength of a trash entry
            # that predates the pipeline entirely.
            #
            # That is a long way from "you deleted it in Immich, so delete it
            # in Proton", which is what the queue is for. Staging still
            # happens, because it may well be what was meant, but never
            # silently.
            log.warn(
                "reconcile.staged_without_uploading",
                account=self.account, assets=never_uploaded,
                detail=("these were matched to assets ALREADY IN IMMICH'S "
                        "TRASH by checksum -- this pipeline never uploaded "
                        "them. Check `sync.py staged` before executing, and "
                        "empty or restore Immich's trash if they should not "
                        "be deleted from Proton"))

        cancelled = self.cancel_restored(set(by_asset))

        log.info("reconcile.done", trashed_in_immich=len(by_asset),
                 matched=len(rows), staged=self.stats.staged,
                 cancelled=cancelled,
                 staged_without_uploading=never_uploaded,
                 total_staged=state.count_staged(self.conn, self.account),
                 dry_run=self.dry_run)
        return self.stats

    def cancel_restored(self, trashed_ids: set[str]) -> int:
        """Withdraw pending deletions for photos that are back in the library.

        The queue used to be append-only -- "reconcile only ever adds" -- which
        made it disagree with the very signal it is built on. Restoring a photo
        in Immich is how a person says "no, keep this one", and yet the Proton
        original stayed queued, so a queue could sit there listing hundreds of
        photos plainly present in Immich and not in its trash. Under
        `delete.action: execute` those originals would then be trashed in
        Proton despite having been explicitly rescued.

        Absence from the trash listing is *not* enough to act on, because two
        opposite situations produce it:

        * restored -- the asset is live again, so withdraw the deletion;
        * purged   -- Immich's 30-day sweep deleted it, so the staged row is
          now the only record that the Proton original should go, and it must
          survive. That is the whole reason this list is stored rather than
          recomputed from the server on each view.

        So a row is withdrawn only on **positive proof of life**: Immich is
        asked about the asset directly and must answer that it holds it and it
        is not trashed. Anything else -- missing, still trashed, an error, no
        asset id to ask about -- leaves the row exactly as it was. Errors
        change nothing.

        Only `staged_for_delete` rows are revalidated. `deleting`,
        `remote_trashed` and `delete_failed` are left alone: their Proton file
        may already be gone, and sending those back would have the puller
        download them all over again.
        """
        if not self.cfg.get("reconcile.cancel_restored", True):
            return 0
        if not getattr(self.client, "trash_scan_complete", True):
            log.warn("reconcile.cancel_skipped", account=self.account,
                     detail=("the trash scan hit reconcile.max_pages, so "
                             "'not in the trash' cannot be told apart from "
                             "'not reached yet' -- nothing was unstaged"))
            return 0

        pending = state.staged_deletes(self.conn, self.account)
        candidates = [r for r in pending
                      if str(r["immich_asset_id"] or "") not in trashed_ids
                      and r["immich_asset_id"]]
        if not candidates:
            return 0

        # One lookup per candidate, and a candidate only exists when something
        # actually left the trash since the last pass -- in the steady state
        # this list is empty and no request is made at all. The cap is for the
        # pathological case where it is not.
        cap = int(self.cfg.get("reconcile.cancel_check_max", 5000))
        if cap and len(candidates) > cap:
            log.warn("reconcile.cancel_check_capped", account=self.account,
                     candidates=len(candidates), cap=cap)
            candidates = candidates[:cap]

        restored: list[int] = []
        purged = unknown = 0
        for row in candidates:
            try:
                where = self.client.asset_state(str(row["immich_asset_id"]))
            except ImmichAuthError as exc:
                raise AuthFailure(f"immich: {exc}") from exc
            except ImmichError:
                where = "unknown"
            if where == "live":
                restored.append(int(row["id"]))
            elif where == "missing":
                purged += 1
            else:
                unknown += 1

        if purged or unknown:
            log.info("reconcile.cancel_kept", account=self.account,
                     purged_by_immich=purged, unverifiable=unknown,
                     detail="left on the queue; only a live asset withdraws one")
        if not restored:
            return 0
        if self.dry_run:
            log.info("reconcile.dry_run_cancel", account=self.account,
                     rows=len(restored))
            return len(restored)

        count = state.unstage(self.conn, self.account, restored)
        self.stats.cancelled += count
        log.info("reconcile.cancelled_restored", account=self.account,
                 rows=count,
                 detail="back in immich's library; proton original kept")
        return count

    # -- Phase 6: execute the staged deletions -----------------------------
    def execute_deletes(self, ids: Iterable[int] | None = None,
                        limit: int | None = None,
                        dry_run: bool | None = None) -> Stats:
        """Trash the named staged rows in Proton. The only destructive path.

        Every rule here exists because a mistake is a lost photo:

        * **Trash, never permanent.** `filesystem trash`, so Proton's trash is
          the undo. `filesystem delete` and `empty-trash` are never called.
        * **Ids, never paths.** What to delete is resolved out of
          `staged_deletes`; a caller supplies row ids and nothing else. A path
          from a request body is never passed to the CLI.
        * **Re-resolve before touching anything.** The node id currently at
          the staged path must equal the node id that was staged. A path since
          reused for a different file is skipped and flagged, not deleted.
        * **Capped.** `delete.batch_cap` bounds every invocation regardless of
          what was asked for.
        * **Verified afterwards.** If the node is still sitting there, the row
          becomes `delete_failed` rather than quietly claiming success.
        * **Audited.** Every attempt appends a row to `deletions`, which is
          never updated or deleted.
        """
        dry = self.dry_run if dry_run is None else dry_run
        action = str(getattr(self.cfg, "delete_action", "mark_only"))
        cap = int(self.cfg.get("delete.batch_cap", 50))
        if limit is not None:
            cap = min(cap, max(int(limit), 0))

        if ids is None:
            rows = state.staged_deletes(self.conn, self.account, limit=cap)
        else:
            rows = [r for r in state.get_staged(self.conn, self.account, list(ids))
                    if r["state"] == state.STAGED][:cap]

        if not rows:
            log.info("delete.nothing_to_do", action=action)
            return self.stats

        log.info("delete.start", count=len(rows), action=action, dry_run=dry,
                 cap=cap)

        for row in rows:
            staged_id = int(row["id"])
            node_id = row["node_id"]
            path = row["remote_path"]

            if action == "mark_only":
                # Nothing is called on Proton. The row records that the
                # operator is doing the deletion themselves, which is the
                # honest state for a Photos-section library the CLI may refuse.
                if dry:
                    log.info("delete.dry_run_mark_only", node_id=node_id, path=path)
                    self.stats.delete_skipped += 1
                    continue
                state.mark_remote_trashed(self.conn, self.account, row,
                                          "mark_only")
                self.stats.trashed += 1
                log.transition(node_id, state.STAGED_FOR_DELETE,
                               state.REMOTE_TRASHED, path=path,
                               action="mark_only")
                continue

            if not path:
                self._flag_delete(row, "no remote path recorded; cannot re-resolve")
                continue

            # --- pre-flight: is the staged node still the node at that path?
            try:
                current = self.backend.resolve(path)
            except AuthError as exc:
                self.auth_ok = False
                raise AuthFailure(str(exc)) from exc
            except ProtonError as exc:
                self._flag_delete(row, f"could not resolve: {log.condense(str(exc), 300)}")
                continue

            if current is None:
                # Already gone from Proton -- someone deleted it by hand, or a
                # previous pass succeeded and we crashed before recording it.
                # Not a failure: the desired end state is the actual one.
                if dry:
                    log.info("delete.dry_run_already_gone", node_id=node_id, path=path)
                    self.stats.delete_skipped += 1
                    continue
                state.mark_remote_trashed(self.conn, self.account, row,
                                          "already_absent")
                self.stats.trashed += 1
                log.transition(node_id, state.STAGED_FOR_DELETE,
                               state.REMOTE_TRASHED, path=path,
                               reason="already absent in proton")
                continue

            if current.node_id != node_id:
                # The path has been reused. Deleting what is there now would
                # destroy a file nobody asked about.
                self._flag_delete(
                    row,
                    f"path now holds a different node ({current.node_id}, "
                    f"staged {node_id}); refusing to trash it")
                continue

            if dry:
                log.info("delete.dry_run", node_id=node_id, path=path,
                         name=row["remote_name"])
                self.stats.delete_skipped += 1
                continue

            # --- execute
            state.mark_deleting(self.conn, self.account, node_id, staged_id)
            log.transition(node_id, state.STAGED_FOR_DELETE, state.DELETING,
                           path=path)
            try:
                self.backend.trash(path)
            except AuthError as exc:
                self.auth_ok = False
                raise AuthFailure(str(exc)) from exc
            except ProtonError as exc:
                self._flag_delete(row, log.condense(str(exc), 500))
                continue

            # --- verify: the node must no longer be at that path
            try:
                after = self.backend.resolve(path)
            except ProtonError as exc:
                log.warn("delete.verify_unavailable", node_id=node_id,
                         detail=str(exc)[:200])
                after = None
            if after is not None and after.node_id == node_id:
                self._flag_delete(row, "trash reported success but the node is "
                                       "still at that path")
                continue

            state.mark_remote_trashed(self.conn, self.account, row, "trashed")
            self.stats.trashed += 1
            log.transition(node_id, state.DELETING, state.REMOTE_TRASHED,
                           path=path)

        log.info("delete.done", trashed=self.stats.trashed,
                 failed=self.stats.delete_failed,
                 skipped=self.stats.delete_skipped, dry_run=dry)
        return self.stats

    def _flag_delete(self, row, error: str) -> None:
        """A staged row that could not be trashed. Never silently dropped: it
        stays visible in the UI with the reason, and the audit table records
        the attempt."""
        state.mark_delete_failed(self.conn, self.account, row, error)
        self.stats.delete_failed += 1
        log.error("delete.failed", node_id=row["node_id"],
                  path=row["remote_path"], error=error[:400])

    # -- Phase 7: run everything ------------------------------------------
    def run(self, backfill: bool = False) -> Stats:
        state.start_run(self.conn, self.account, self.run_id)
        reset = state.resume(self.conn, self.account)
        if reset:
            log.info("run.resumed", **{k: v for k, v in reset.items()})
        self.pull()
        if self.cfg.get("immich.precheck_claimed_digests"):
            self.precheck()
        self.download(backfill=backfill)
        self.push()
        self.verify()
        self.reap()
        # Last, and never destructive: reconcile only adds to the staged list.
        # Deleting is a separate, manual step.
        self.reconcile()
        return self.stats


def _row_get(row: sqlite3.Row, column: str) -> Any:
    """sqlite3.Row raises on unknown columns; tolerate a pre-migration row."""
    try:
        return row[column]
    except (IndexError, KeyError):
        return None


def _remove_if_empty(path: Path) -> None:
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def _prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == str(root):
            continue
        if not dirnames and not filenames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
