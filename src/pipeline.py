"""Phase orchestration: pull, download, push, verify, reap.

Backends are injected so the whole pipeline can be exercised against fakes
without touching the network -- see tests/.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import immich as immich_mod
from . import log, proton, state
from .immich import ImmichAuthError, ImmichClient, ImmichCliUploader, ImmichError, sha1_file
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
    purged: int = 0
    failed: int = 0
    quarantined: int = 0
    bytes_downloaded: int = 0
    aborted: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuthFailure(Exception):
    """Raised to surface exit code 2 to the CLI layer."""


class Pipeline:
    def __init__(self, cfg, conn: sqlite3.Connection, backend=None, client=None,
                 uploader=None, run_id: str | None = None, dry_run: bool = False):
        self.cfg = cfg
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

    def _fail(self, node_id: str, stage: str, error: str, previous: str) -> None:
        status = state.mark_failed(
            self.conn, node_id, stage, error,
            max_attempts=int(self.cfg.get("limits.max_attempts", 5)))
        self.stats.failed += 1
        if status == state.QUARANTINED:
            self.stats.quarantined += 1
        log.transition(node_id, previous, status, stage=stage, error=error[:300])

    # -- Phase 1: pull -----------------------------------------------------
    def pull(self, dry_run: bool | None = None) -> Stats:
        dry = self.dry_run if dry_run is None else dry_run
        extensions = list(self.cfg.get("proton.extensions", []) or [])
        excludes = list(self.cfg.get("proton.exclude_globs", []) or [])
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
                    if not proton.should_include(node, extensions, excludes):
                        self.stats.skipped += 1
                        continue
                    if dry:
                        row = state.get(self.conn, node.node_id)
                        if row is None:
                            self.stats.discovered += 1
                        elif ((row["remote_size"] or 0) != (node.size or 0)
                              or (row["remote_modified"] or "") != (node.modified or "")):
                            self.stats.changed += 1
                        else:
                            self.stats.unchanged += 1
                        continue

                    result = state.upsert_discovered(
                        self.conn, node.node_id, node.path, node.name,
                        node.size, node.modified)
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

    # -- Phase 2: download -------------------------------------------------
    def _ready_path(self, row: sqlite3.Row, sha1: str) -> Path:
        ts = state.parse_ts(row["remote_modified"]) or datetime.now(timezone.utc)
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
            self.conn, limit=limit, max_bytes=max_bytes,
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

        budget = max_bytes
        for row in rows:
            node_id = row["node_id"]
            size = row["remote_size"] or 0
            if budget is not None and size > budget and self.stats.downloaded:
                log.info("download.byte_cap_reached", downloaded=self.stats.downloaded)
                break
            if self._free_bytes() - size < floor:
                log.warn("download.stopped_low_space", free_gb=round(self._free_bytes() / 1e9, 1))
                self.stats.aborted.append("stopped early: free space floor reached")
                break

            if self.dry_run:
                log.info("download.dry_run", node_id=node_id, path=row["remote_path"])
                self.stats.downloaded += 1
                continue

            previous = row["status"]
            state.mark_downloading(self.conn, node_id)
            log.transition(node_id, previous, state.DOWNLOADING, path=row["remote_path"])

            # One scratch folder per node: the Proton CLI downloads into a
            # folder and picks the filename, and with "-c skip" a leftover file
            # from another node could otherwise be promoted as this one.
            scratch_dir = incoming / hashlib.sha1(str(node_id).encode()).hexdigest()[:12]
            scratch = scratch_dir / row["remote_name"]
            node = RemoteNode(node_id=node_id, path=row["remote_path"],
                              name=row["remote_name"], size=row["remote_size"],
                              modified=row["remote_modified"], is_folder=False)
            try:
                self.backend.download(node, scratch)
                actual = scratch.stat().st_size
                if size and actual != size:
                    raise ProtonError(
                        f"size mismatch: remote {size} bytes, got {actual}")
                if actual == 0:
                    raise ProtonError("downloaded file is empty")
                digest = sha1_file(scratch)
                target = self._ready_path(row, digest)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(scratch, target)  # same filesystem: atomic
            except AuthError as exc:
                # Leave the row in `downloading`; resume() resets it next run.
                self.auth_ok = False
                raise AuthFailure(str(exc)) from exc
            except (ProtonError, OSError) as exc:
                self._fail(node_id, "download", str(exc), state.DOWNLOADING)
                continue
            finally:
                # Covers success (the file has been moved out), failure and
                # the auth abort alike.
                shutil.rmtree(scratch_dir, ignore_errors=True)

            state.mark_downloaded(self.conn, node_id, str(target), digest)
            log.transition(node_id, state.DOWNLOADING, state.DOWNLOADED,
                           local_path=str(target), sha1=digest, bytes=actual)
            self.stats.downloaded += 1
            self.stats.bytes_downloaded += actual
            if budget is not None:
                budget -= actual

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

    def push(self, limit: int | None = None, dry_run: bool | None = None) -> Stats:
        dry = self.dry_run if dry_run is None else dry_run
        rows = state.select_for_upload(
            self.conn, limit=limit,
            max_attempts=int(self.cfg.get("limits.max_attempts", 5)),
            backoff_base_sec=int(self.cfg.get("limits.backoff_base_sec", 300)),
            backoff_cap_sec=int(self.cfg.get("limits.backoff_cap_sec", 86400)),
        )
        present, missing = [], []
        for row in rows:
            target = present if (row["local_path"] and Path(row["local_path"]).exists()) else missing
            target.append(row)
        for row in missing:
            # Reaped too early, or removed by hand: send it back for re-download.
            self._fail(row["node_id"], "push",
                       f"local file missing: {row['local_path']}", row["status"])
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

        for row in rows:
            hit = known.get(row["node_id"])
            if hit and hit.found and hit.asset_id:
                if dry:
                    log.info("push.dry_run_duplicate", node_id=row["node_id"])
                    continue
                state.mark_uploaded(self.conn, row["node_id"], hit.asset_id, is_duplicate=True)
                log.transition(row["node_id"], row["status"], state.UPLOADED,
                               asset_id=hit.asset_id, duplicate=True)
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
            state.mark_uploading(self.conn, row["node_id"])
            log.transition(row["node_id"], row["status"], state.UPLOADING)

        mode = self.cfg.get("immich.upload_mode", "cli")
        if mode == "api":
            self._push_via_api(pending)
        else:
            self._push_via_cli(pending)

        log.info("push.done", uploaded=self.stats.uploaded,
                 duplicates=self.stats.duplicates, failed=self.stats.failed)
        return self.stats

    def _push_via_api(self, rows: list[sqlite3.Row]) -> None:
        for row in rows:
            try:
                result = self.client.upload_file(
                    row["local_path"], row["sha1"], device_asset_id=row["node_id"])
            except ImmichAuthError as exc:
                raise AuthFailure(f"immich: {exc}") from exc
            except (ImmichError, OSError) as exc:
                self._fail(row["node_id"], "upload", str(exc), state.UPLOADING)
                continue
            if not result.asset_id:
                self._fail(row["node_id"], "upload", "no asset id returned",
                           state.UPLOADING)
                continue
            state.mark_uploaded(self.conn, row["node_id"], result.asset_id,
                                is_duplicate=result.duplicate)
            log.transition(row["node_id"], state.UPLOADING, state.UPLOADED,
                           asset_id=result.asset_id, duplicate=result.duplicate)
            self.stats.uploaded += 1
            if result.duplicate:
                self.stats.duplicates += 1

    def _push_via_cli(self, rows: list[sqlite3.Row]) -> None:
        batch, mapping = self._build_batch(rows)
        try:
            try:
                run = self.uploader.upload_dir(batch)
                log.debug("push.cli_output", stdout=run.stdout[-1500:],
                          stderr=run.stderr[-500:])
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
                    state.mark_uploaded(self.conn, row["node_id"], hit.asset_id,
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
        rows = state.select_for_verify(self.conn, limit=limit)
        if not rows:
            log.info("verify.nothing_to_do")
            return self.stats
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
                            "UPDATE assets SET immich_asset_id=? WHERE node_id=?",
                            (asset.get("id"), node_id))
                        self.conn.commit()
            except ImmichAuthError as exc:
                raise AuthFailure(f"immich: {exc}") from exc
            except ImmichError as exc:
                self._fail(node_id, "verify", str(exc), state.UPLOADED)
                continue

            if not asset:
                self._fail(node_id, "verify", "asset not found server-side",
                           state.UPLOADED)
                continue
            remote_sum = asset.get("checksum") or (asset.get("exifInfo") or {}).get("checksum")
            if remote_sum and not immich_mod.checksums_match(row["sha1"], remote_sum):
                self._fail(node_id, "verify",
                           f"checksum mismatch (local {row['sha1']}, remote {remote_sum})",
                           state.UPLOADED)
                continue
            if not remote_sum:
                log.warn("verify.no_remote_checksum", node_id=node_id,
                         asset_id=asset.get("id"))
            state.mark_verified(self.conn, node_id)
            log.transition(node_id, state.UPLOADED, state.VERIFIED,
                           asset_id=asset.get("id"))
            self.stats.verified += 1
        log.info("verify.done", verified=self.stats.verified, failed=self.stats.failed)
        return self.stats

    # -- Phase 4b: reap ----------------------------------------------------
    def reap(self, keep_days: int | None = None) -> Stats:
        keep = int(self.cfg.get("reap.keep_days", 7)) if keep_days is None else keep_days
        rows = state.select_for_reap(self.conn, keep_days=keep)
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
            state.mark_purged(self.conn, row["node_id"])
            log.transition(row["node_id"], state.VERIFIED, state.PURGED, path=local)
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

    # -- Phase 5: run everything ------------------------------------------
    def run(self, backfill: bool = False) -> Stats:
        state.start_run(self.conn, self.run_id)
        reset = state.resume(self.conn)
        if reset:
            log.info("run.resumed", **{k: v for k, v in reset.items()})
        self.pull()
        self.download(backfill=backfill)
        self.push()
        self.verify()
        self.reap()
        return self.stats


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
