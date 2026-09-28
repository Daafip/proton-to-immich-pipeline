"""Proton Drive access.

Two backends behind one interface:

* ProtonCliBackend -- the official Bun binary, `--json` on every subcommand.
  Its exact flags and JSON shape are the one genuinely unverified part of the
  build plan (Phase 0), so the argument templates live in config and the JSON
  normaliser accepts every plausible key spelling rather than one fixed schema.
* RcloneBackend -- the documented Phase 0 fallback. `rclone lsjson` has a
  stable, well-known schema, and credentials live in rclone.conf (no keyring),
  which is why it is the headless escape hatch.
"""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import log


class ProtonError(Exception):
    pass


class AuthError(ProtonError):
    """Session missing or expired -- exit code 2, and the case most likely to
    stall the pipeline silently, so it gets its own signal into HA."""


# "You need to login first" is what cli-drive@0.6.0 actually prints, on stdout,
# with exit 1 and no JSON even under --json. Missing it would turn an expired
# session into a generic error instead of the auth signal Home Assistant needs.
_AUTH_HINTS = (
    "need to login", "needs to login", "login first", "log in first",
    "not logged in", "unauthenticated", "unauthorized", "401",
    "no session", "session expired", "please log in", "login required",
    "authentication", "invalid credentials", "secret service",
)

_ID_KEYS = ("node_id", "nodeId", "uid", "linkId", "link_id", "id")
_NAME_KEYS = ("name", "filename", "fileName", "basename")
_PATH_KEYS = ("path", "fullPath", "full_path", "remotePath", "remote_path", "Path")
# claimedSize is the plaintext content size. totalStorageSize/storageSize are
# the ENCRYPTED size and run ~25% larger -- using one of those as "the file
# size" fails every post-download size check.
_SIZE_KEYS = ("claimedSize", "claimed_size", "size", "sizeBytes", "size_bytes",
              "fileSize", "totalSize", "Size")
_STORAGE_SIZE_KEYS = ("totalStorageSize", "storageSize")
_MTIME_KEYS = ("modified", "modifiedAt", "modified_at", "modificationTime",
               "modification_time", "lastModified", "last_modified", "mtime",
               "updatedAt", "updated_at", "captureTime", "capture_time", "ModTime")
_FOLDER_KEYS = ("isFolder", "is_folder", "isDirectory", "is_dir", "IsDir", "folder")
_TYPE_KEYS = ("type", "kind", "nodeType", "node_type", "mimeType")
_LIST_KEYS = ("items", "nodes", "entries", "files", "children", "results",
              "data", "list", "content")


@dataclass
class RemoteNode:
    node_id: str
    path: str
    name: str
    size: int | None
    modified: str | None
    is_folder: bool
    # From activeRevision. `sha1` is the uploader's claimed digest -- the same
    # algorithm Immich dedupes with. `capture_time` is when the photo was
    # taken, which is usually nothing like when it was put into Proton.
    sha1: str | None = None
    capture_time: str | None = None
    media_type: str | None = None

    @property
    def ext(self) -> str:
        return posixpath.splitext(self.name)[1].lower()


def unwrap(value: Any) -> Any:
    """cli-drive wraps decryptable metadata as {"ok": bool, "value": ...}.

    Names, and anything else derived from encrypted attributes, arrive this
    way because decryption can fail. `ok: false` means "this field is not
    available", which is different from "this field is empty".
    """
    if isinstance(value, dict) and ("ok" in value or "value" in value):
        if value.get("ok") is False:
            return None
        if "value" in value:
            return value["value"]
    return value


def _first(obj: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in obj:
            value = unwrap(obj[k])
            if value not in (None, ""):
                return value
    return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_ts(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Heuristic: milliseconds vs seconds since epoch.
        from datetime import datetime, timezone
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")
    return str(value)


def is_folder_entry(obj: dict[str, Any]) -> bool:
    flag = _first(obj, _FOLDER_KEYS)
    if isinstance(flag, str):
        flag = flag.lower() in ("true", "yes", "folder", "1")
    if isinstance(flag, bool):
        return flag
    type_value = _first(obj, _TYPE_KEYS)
    if isinstance(type_value, str):
        low = type_value.lower()
        if low in ("folder", "dir", "directory", "album"):
            return True
        if low in ("file", "document", "photo", "video") or "/" in low:
            return False
    return bool(obj.get("children"))


def normalize_entry(obj: dict[str, Any], parent_path: str) -> RemoteNode | None:
    """Map one JSON object to a RemoteNode, whatever the CLI calls its fields."""
    if not isinstance(obj, dict):
        return None
    name = _first(obj, _NAME_KEYS)
    path = _first(obj, _PATH_KEYS)
    node_id_raw = _first(obj, _ID_KEYS)
    if not name and not path and not node_id_raw:
        return None
    if not name and path:
        name = posixpath.basename(str(path).rstrip("/"))
    if not name:
        # "When name cannot be decrypted or conflicts with other node(s),
        # node UIDs can be used instead" -- proton-drive filesystem help.
        name = str(node_id_raw)
    if not path:
        # The CLI requires / inside a node name to be backslash-escaped.
        path = posixpath.join(parent_path or "/", str(name).replace("/", "\\/"))
    path = "/" + str(path).lstrip("/")

    node_id = node_id_raw
    if not node_id:
        # No stable id available: the path is the next best primary key.
        node_id = f"path:{path}"

    revision = unwrap(obj.get("activeRevision")) or {}
    if not isinstance(revision, dict):
        revision = {}
    digests = unwrap(revision.get("claimedDigests")) or {}
    if not isinstance(digests, dict):
        digests = {}
    extra = unwrap(revision.get("claimedAdditionalMetadata")) or {}
    camera = (extra.get("Camera") or {}) if isinstance(extra, dict) else {}

    size = _coerce_int(_first(revision, _SIZE_KEYS))
    if size is None:
        size = _coerce_int(_first(obj, _SIZE_KEYS))
    if size is None:
        # Last resort only: this is the encrypted size, not the content size.
        size = _coerce_int(_first(obj, _STORAGE_SIZE_KEYS))
        if size is not None:
            log.debug("proton.storage_size_fallback", name=str(name))

    capture = _coerce_ts(camera.get("CaptureTime") if isinstance(camera, dict) else None)
    if not capture:
        capture = _coerce_ts(_first(revision, ("claimedModificationTime",)))

    return RemoteNode(
        node_id=str(node_id),
        path=path,
        name=str(name),
        size=size,
        modified=_coerce_ts(_first(obj, _MTIME_KEYS)),
        is_folder=is_folder_entry(obj),
        sha1=(str(digests.get("sha1")).lower() if digests.get("sha1") else None),
        capture_time=capture,
        media_type=_first(obj, ("mediaType", "media_type", "mimeType")),
    )


def extract_entries(payload: Any) -> list[dict[str, Any]]:
    """Dig the list of entries out of whatever the CLI returned."""
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return [e for e in value if isinstance(e, dict)]
            if isinstance(value, dict):
                nested = extract_entries(value)
                if nested:
                    return nested
        # A single entry returned bare.
        if any(k in payload for k in _NAME_KEYS + _PATH_KEYS):
            return [payload]
    return []


def parse_json_output(text: str) -> Any:
    """Accept a JSON document, or NDJSON, or JSON preceded by log noise."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] not in "[{":
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if records:
        return records

    # Last resort: the first balanced JSON value in the stream.
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ProtonError(f"could not parse JSON output: {text[:400]!r}")


def looks_like_auth_failure(text: str) -> bool:
    low = (text or "").lower()
    return any(hint in low for hint in _AUTH_HINTS)


# A path that resolves to nothing is not an error for the delete path -- it is
# the answer ("the node is gone"). Distinguishing it from a real failure is
# what stops a transport hiccup being read as "already deleted".
_MISSING_HINTS = (
    "not found", "no such", "does not exist", "doesn't exist", "enoent",
    "404", "cannot find", "could not find", "no node", "invalid path",
)


def looks_like_missing_node(text: str) -> bool:
    low = (text or "").lower()
    if looks_like_auth_failure(low):
        return False
    return any(hint in low for hint in _MISSING_HINTS)


_TRASHED_KEYS = ("isTrashed", "is_trashed", "trashed", "trashedAt", "trashTime",
                 "deletedAt", "state", "nodeState")


def is_trashed_entry(obj: dict[str, Any]) -> bool:
    """Whether a node the CLI just described is in Proton's trash.

    Best-effort: the key is not in any captured `filesystem info` sample, so
    the delete path treats "no longer resolvable at that path" as the primary
    signal and this as a bonus. Returning False must therefore never be read
    as "definitely still live" on its own.
    """
    for key in _TRASHED_KEYS:
        if key not in obj:
            continue
        value = unwrap(obj[key])
        if isinstance(value, bool):
            if value:
                return True
        elif isinstance(value, str):
            if value.strip().lower() in ("trashed", "trash", "deleted", "true"):
                return True
        elif value not in (None, ""):
            # A timestamp under trashedAt/deletedAt means it happened.
            if key in ("trashedAt", "trashTime", "deletedAt"):
                return True
    return False


_UNSAFE_FILENAME = re.compile(r"[/\\\x00]")


def safe_filename(name: str, fallback: str = "asset") -> str:
    """A remote name is untrusted input for the local filesystem.

    Proton names may contain / (the CLI escapes it with a backslash), and an
    undecryptable name falls back to a uid, which is base64 and can contain /.
    """
    cleaned = _UNSAFE_FILENAME.sub("_", str(name or "")).strip().strip(".")
    cleaned = cleaned[:200]
    return cleaned or fallback


# The Photos section: the timeline the phone apps back up into. It is not a
# folder under /my-files and the `filesystem` verbs refuse it ("Path type
# photos is not supported"); cli-drive reaches it through `photo timeline` and
# `photo download` instead. Both /photos/<name> and /photos/<full uid> download.
PHOTOS_ROOT = "/photos"


def is_photos_path(path: str | None) -> bool:
    path = str(path or "")
    return path.rstrip("/") == PHOTOS_ROOT or path.startswith(PHOTOS_ROOT + "/")


def should_include(node: RemoteNode, extensions: list[str], exclude_globs: list[str],
                   media_prefixes: list[str] | None = None) -> bool:
    if node.is_folder:
        return False
    # mediaType is authoritative when present; extensions are the fallback for
    # entries that do not carry one.
    if node.media_type and media_prefixes:
        if not any(str(node.media_type).lower().startswith(p.lower())
                   for p in media_prefixes):
            return False
    elif extensions and node.ext not in {e.lower() for e in extensions}:
        return False
    for pattern in exclude_globs or []:
        if fnmatch.fnmatch(node.name, pattern) or fnmatch.fnmatch(node.path, pattern):
            return False
    return True


# --------------------------------------------------------------------------


class Backend:
    name = "base"

    def auth_ok(self) -> bool:
        raise NotImplementedError

    def walk(self, root: str) -> Iterator[RemoteNode]:
        raise NotImplementedError

    def download(self, node: RemoteNode, dest: Path) -> None:
        raise NotImplementedError

    def download_many(self, nodes: Sequence[RemoteNode], dest_dir: Path) -> None:
        """Fetch several nodes into one folder.

        The default is one call per node, which is exactly what v1 did.
        ProtonCliBackend overrides it with a real multi-path invocation --
        that is where the saving is, because the cost being avoided is process
        startup, not transfer.

        Every caller must treat partial success as normal: some files may have
        landed before a failure, so the folder is inspected afterwards rather
        than trusted to the exit code.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        for node in nodes:
            self.download(node, dest_dir / safe_filename(node.name))

    # -- v2: the delete path ----------------------------------------------
    def resolve(self, path: str) -> RemoteNode | None:
        """The node currently at `path`, or None if nothing is there."""
        raise NotImplementedError

    def trash(self, path: str) -> None:
        """Move one node to Proton's trash. Never a permanent delete."""
        raise NotImplementedError


# `filesystem download` conflict flags are not stable across CLI builds:
# 0.6.0 documents `-c/--conflict-strategy`, 0.8.0 rejects `-c` outright. The
# backend tries these in order and remembers the first the binary accepts.
CONFLICT_FALLBACKS: list[list[str]] = [
    ["--conflict-strategy", "skip"],
    [],
]

_CONFLICT_FLAGS = {
    "-c", "--conflict-strategy",
    "-f", "--file-conflict-strategy",
    "-d", "--folder-conflict-strategy",
}


def looks_like_unknown_option(blob: str) -> bool:
    low = blob.lower()
    return ("unknown option" in low or "unrecognized option" in low
            or "unknown flag" in low)


def _set_conflict_args(args: list[str], variant: list[str]) -> list[str]:
    """Strip whatever conflict flag the template carried and splice in
    `variant` after the `download` verb. Returns args unchanged if the
    template is custom enough that `download` is not in it."""
    if "download" not in args:
        return args
    out: list[str] = []
    drop_value = False
    for tok in args:
        if drop_value:
            drop_value = False
            continue
        if tok in _CONFLICT_FLAGS:
            drop_value = True
            continue
        if any(tok.startswith(f + "=") for f in _CONFLICT_FLAGS):
            continue
        out.append(tok)
    at = out.index("download") + 1
    return out[:at] + list(variant) + out[at:]


def _refuse_photos(path: str, verb: str) -> None:
    """`filesystem info` and `filesystem trash` reject Photos paths, and
    cli-drive has no photo equivalent. Say so plainly rather than pass on
    "Path type photos is not supported"."""
    if is_photos_path(path):
        raise ProtonError(
            f"cannot {verb} {path}: cli-drive has no way to {verb} a photo in "
            f"the Photos timeline. Use delete.action: mark_only for this "
            f"account and delete it in the Proton app")


class ProtonCliBackend(Backend):
    name = "proton-cli"

    def __init__(self, cfg):
        self.binary = cfg.get("proton.binary", "proton-drive")
        self.cache_dir = cfg.proton_cache_dir
        self.timeout = int(cfg.get("proton.timeout_sec", 900))
        self.timeline_timeout = int(cfg.get("proton.timeline_timeout_sec", 3600))
        self.max_depth = int(cfg.get("proton.max_depth", 25))
        self.cmd = cfg.get("proton.cmd", {})
        self.credentials_store = cfg.get("proton.credentials_store")
        self.log_level = cfg.get("proton.cli_log_level")
        self.auth_probe_path = cfg.get("proton.auth_probe_path", "/")
        # None = the configured template is still believed to work.
        self._conflict_args: list[str] | None = None

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.cache_dir:
            try:
                path = Path(self.cache_dir)
                path.mkdir(parents=True, exist_ok=True)
                # Cache, app data and logs all land here, and with
                # credentials_store=unsafe_file so does the session token.
                path.chmod(0o700)
            except OSError as exc:
                raise ProtonError(
                    f"cannot prepare PROTON_DRIVE_CACHE_DIR {self.cache_dir}: {exc}"
                ) from exc
            env["PROTON_DRIVE_CACHE_DIR"] = str(self.cache_dir)
        if self.credentials_store:
            env["PROTON_DRIVE_CREDENTIALS_STORE"] = str(self.credentials_store)
        if self.log_level:
            # The CLI defaults to DEBUG and writes into the cache dir, which is
            # on the same SSD as Immich, with no rotation of its own.
            env["PROTON_DRIVE_LOG_LEVEL"] = str(self.log_level).upper()
        return env

    def _template(self, key: str, default: list[str], **subs: str) -> list[str]:
        template = self.cmd.get(key) or default
        return [str(part).format(**subs) for part in template]

    def _template_multi(self, key: str, default: list[str],
                        paths: Sequence[str], **subs: str) -> list[str]:
        """Like _template, but `{path}` expands to several arguments.

        `filesystem download path... localFolder` takes any number of source
        paths, so the single `{path}` slot in the configured template becomes
        the whole list. Nothing goes through a shell, so a path with spaces
        stays one argument.
        """
        template = self.cmd.get(key) or default
        out: list[str] = []
        for part in template:
            part = str(part)
            if part == "{path}":
                out.extend(paths)
            else:
                out.append(part.format(path="", **subs))
        return out

    def _run(self, args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
        argv = [self.binary, *args]
        log.debug("proton.exec", argv=" ".join(argv))
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                timeout=timeout or self.timeout, env=self._env(), check=False,
            )
        except FileNotFoundError as exc:
            raise ProtonError(f"{self.binary} not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProtonError(f"{self.binary} timed out after {timeout or self.timeout}s") from exc
        if proc.returncode != 0:
            blob = f"{proc.stderr}\n{proc.stdout}"
            if looks_like_auth_failure(blob):
                raise AuthError(log.condense(blob))
            raise ProtonError(
                f"{' '.join(argv)} exited {proc.returncode}: {log.condense(blob)}")
        return proc

    def auth_ok(self) -> bool:
        """cli-drive has only `auth login` / `auth logout`, no status command,
        so the session probe is a listing of the top-level sections."""
        try:
            self.list_dir(self.auth_probe_path)
            return True
        except AuthError:
            return False
        except ProtonError as exc:
            log.warn("proton.auth_probe_failed", detail=str(exc)[:200])
            return False

    def start_login(self, timeout: int = 30) -> tuple[str, subprocess.Popen]:
        """Start `auth login --json` and return (signInUrl, live process).

        cli-drive@0.6.0 prints exactly one JSON line, {"signInUrl": "..."},
        then blocks until the sign-in completes. The URL carries its payload in
        the fragment, so it is a desktop-pairing flow: the CLI polls Proton's
        API and the browser never calls back to this machine. That means any
        device can complete it -- there is no loopback port to forward.
        """
        argv = [self.binary, "auth", "login", "--json"]
        log.debug("proton.login_exec", argv=" ".join(argv))
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=self._env(), bufsize=1,
            )
        except FileNotFoundError as exc:
            raise ProtonError(f"{self.binary} not found on PATH") from exc

        # A reader thread rather than select(): select watches the OS pipe,
        # but readline() fills a Python-level buffer, so a noise line arriving
        # in the same chunk as the JSON would leave the URL sitting in that
        # buffer with select reporting nothing more to read.
        lines: queue.Queue[str | None] = queue.Queue()

        def reader() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    lines.put(line)
            finally:
                lines.put(None)

        threading.Thread(target=reader, daemon=True).start()

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                line = lines.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            line = line.strip()
            if not line.startswith("{"):
                log.debug("proton.login_noise", line=line[:200])
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = payload.get("signInUrl") or payload.get("url")
            if url:
                return str(url), proc

        proc.kill()
        stderr = (proc.stderr.read() or "").strip() if proc.stderr else ""
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()
        proc.wait(timeout=5)
        raise ProtonError(
            f"no sign-in URL from `{self.binary} auth login --json` "
            f"within {timeout}s: {stderr[:300]}")

    def list_dir(self, path: str) -> list[RemoteNode]:
        args = self._template("list", ["filesystem", "list", "{path}", "--json"], path=path)
        proc = self._run(args)
        payload = parse_json_output(proc.stdout)
        nodes = []
        for entry in extract_entries(payload):
            node = normalize_entry(entry, path)
            if node:
                nodes.append(node)
        return nodes

    def walk_timeline(self) -> Iterator[RemoteNode]:
        """`photo timeline -d --json` -- the whole Photos section in one call.

        The timeline is flat: every photo shares one parent, so paths are
        /photos/<name>. With `-d` each entry has the same shape as a
        `filesystem list` file entry, only with `"type": "photo"` and an extra
        `photo` block, so it goes through the same normalisation.
        """
        args = self._template("timeline", ["photo", "timeline", "-d", "--json"])
        proc = self._run(args, timeout=self.timeline_timeout)
        entries = extract_entries(parse_json_output(proc.stdout))
        log.debug("proton.listed", path=PHOTOS_ROOT, entries=len(entries))
        for entry in entries:
            node = normalize_entry(entry, PHOTOS_ROOT)
            if node and not node.is_folder:
                yield node

    def walk(self, root: str) -> Iterator[RemoteNode]:
        if is_photos_path(root):
            if root.rstrip("/") != PHOTOS_ROOT:
                raise ProtonError(
                    f"root {root!r}: the Photos timeline is flat and has no "
                    f"subfolders; use {PHOTOS_ROOT!r} on its own")
            yield from self.walk_timeline()
            return
        queue: list[tuple[str, int]] = [(root, 0)]
        seen: set[str] = set()
        while queue:
            path, depth = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            nodes = self.list_dir(path)
            log.debug("proton.listed", path=path, entries=len(nodes))
            for node in nodes:
                if node.is_folder:
                    if depth + 1 <= self.max_depth:
                        queue.append((node.path, depth + 1))
                else:
                    yield node

    def _run_download(self, args: list[str], timeout: int | None = None) -> None:
        """Run the download, negotiating the conflict flag if this build
        rejects the configured one -- the same remember-what-worked trick the
        Immich client uses for its checksum encoding.

        Dropping the flag entirely is the last resort and is safe here: the
        pipeline hands every node its own empty scratch folder, so there is
        nothing to conflict with, and stdin is /dev/null so a prompt cannot
        hang the run.
        """
        if self._conflict_args is not None:
            args = _set_conflict_args(args, self._conflict_args)
        try:
            self._run(args, timeout=timeout)
            return
        except ProtonError as exc:
            if self._conflict_args is not None or not looks_like_unknown_option(str(exc)):
                raise
            first_error = exc

        for variant in CONFLICT_FALLBACKS:
            candidate = _set_conflict_args(args, variant)
            if candidate == args:
                continue
            try:
                self._run(candidate, timeout=timeout)
            except ProtonError as retry_exc:
                if looks_like_unknown_option(str(retry_exc)):
                    continue
                raise
            self._conflict_args = variant
            log.warn("proton.conflict_flag_fallback",
                     using=" ".join(variant) or "(no conflict flag)",
                     detail="pin proton.cmd.download in config to skip this probe")
            return
        raise first_error

    def download(self, node: RemoteNode, dest: Path) -> None:
        """`filesystem download path... localFolder` -- the destination is a
        FOLDER, so the CLI decides the filename. The caller still names the
        file it wants; we rename afterwards.

        A conflict strategy is passed where the build accepts one: without it
        a CLI that hits a conflict prompts, and an unattended run must never
        wait on a prompt. Which flag spells it depends on the build, so
        _run_download negotiates.
        """
        dest_dir = dest.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        if is_photos_path(node.path):
            self._run_photo_download([node], dest_dir)
        else:
            args = self._template(
                "download",
                ["filesystem", "download", "--conflict-strategy", "skip",
                 "{path}", "{dest_dir}"],
                path=node.path, dest=str(dest), dest_dir=str(dest_dir),
            )
            self._run_download(args)
        produced = dest_dir / node.name
        if not dest.exists() and produced.exists():
            produced.replace(dest)
        if not dest.exists():
            listing = ", ".join(sorted(p.name for p in dest_dir.iterdir())) or "nothing"
            raise ProtonError(
                f"download reported success but {dest.name} is missing "
                f"(folder holds: {listing})")

    def download_many(self, nodes: Sequence[RemoteNode], dest_dir: Path) -> None:
        """One `filesystem download` call for the whole batch.

        The CLI costs ~1.2 s of Bun and SDK startup per invocation whatever it
        does, so for a 25k-file backfill the per-file loop spends most of a
        working day starting processes. Batching is the only lever that
        touches that.

        The caller guarantees what makes this safe: every node comes from the
        same source folder and no two share a filename, because they all land
        in one destination folder and `--conflict-strategy skip` would
        silently keep the wrong file on a collision.
        """
        if not nodes:
            return
        if len(nodes) == 1:
            self.download(nodes[0], dest_dir / nodes[0].name)
            return
        dest_dir.mkdir(parents=True, exist_ok=True)
        photos = [n for n in nodes if is_photos_path(n.path)]
        if photos:
            # Batches are grouped by parent folder, so a mix is not expected;
            # anything else in the batch still takes the filesystem route.
            self._run_photo_download(photos, dest_dir)
            nodes = [n for n in nodes if not is_photos_path(n.path)]
            if not nodes:
                return
        args = self._template_multi(
            "download",
            ["filesystem", "download", "--conflict-strategy", "skip",
             "{path}", "{dest_dir}"],
            [node.path for node in nodes],
            dest_dir=str(dest_dir), dest=str(dest_dir),
        )
        # proton.timeout_sec is a per-invocation budget sized for one file,
        # and this invocation does the work of len(nodes) of them, so it
        # scales with the batch. Without this a batch of 25 would have to
        # finish in the time one file was allowed.
        #
        # No post-check here: the pipeline inspects the folder itself, because
        # a batch that fails halfway still leaves real files behind and those
        # should not be thrown away.
        self._run_download(args, timeout=self.timeout * len(nodes))

    def _run_photo_download(self, nodes: Sequence[RemoteNode], dest_dir: Path) -> None:
        """`photo download path... localFolder`, addressing each photo by its
        full uid. Names repeat in the timeline and may not decrypt; the uid
        never does either. The file still lands under the photo's name.

        No conflict-flag negotiation: that fallback is for `filesystem
        download`, and this verb still accepts `-c` on 0.8.0.
        """
        args = self._template_multi(
            "photo_download",
            ["photo", "download", "-c", "rename", "{path}", "{dest_dir}"],
            [f"{PHOTOS_ROOT}/{node.node_id}" for node in nodes],
            dest_dir=str(dest_dir), dest=str(dest_dir),
        )
        self._run(args, timeout=self.timeout * len(nodes))

    # -- the delete path ---------------------------------------------------
    def resolve(self, path: str) -> RemoteNode | None:
        """`filesystem info path --json` -- what is at that path right now.

        Returns None when nothing is. This is the pre-flight check the delete
        path is built on: a staged row is only trashed when the node id at its
        recorded path still matches the one that was staged, so a path since
        reused for a different file is skipped rather than deleted.
        """
        _refuse_photos(path, "look up")
        args = self._template("info", ["filesystem", "info", "{path}", "--json"],
                              path=path)
        try:
            proc = self._run(args)
        except AuthError:
            raise
        except ProtonError as exc:
            if looks_like_missing_node(str(exc)):
                return None
            raise
        payload = parse_json_output(proc.stdout)
        # `info` describes one node, so a bare object is the node itself --
        # not a container to dig a list out of.
        if isinstance(payload, dict) and any(
                k in payload for k in _ID_KEYS + _NAME_KEYS + _TYPE_KEYS):
            entries: list[dict[str, Any]] = [payload]
        else:
            entries = extract_entries(payload)
        if not entries:
            return None
        parent = posixpath.dirname(path.rstrip("/")) or "/"
        node = normalize_entry(entries[0], parent)
        if node is not None and is_trashed_entry(entries[0]):
            log.debug("proton.resolved_trashed", path=path, node_id=node.node_id)
            return None
        return node

    def trash(self, path: str) -> None:
        """`filesystem trash path` -- reversible; Proton's trash is the undo.

        Deliberately never `filesystem delete` or `empty-trash`, which are
        permanent, and deliberately one path per invocation even though the
        CLI accepts several. A batch shares one exit code, so a partial
        failure would leave us unable to say which node it was -- and this is
        the only destructive call in the pipeline, so an unambiguous result
        per node is worth the ~1.2 s of CLI startup.
        """
        _refuse_photos(path, "trash")
        args = self._template("trash", ["filesystem", "trash", "{path}"], path=path)
        self._run(args)


class RcloneBackend(Backend):
    name = "rclone"

    def __init__(self, cfg):
        self.binary = cfg.get("proton.rclone.binary", "rclone")
        self.remote = cfg.get("proton.rclone.remote", "protondrive:")
        self.extra = list(cfg.get("proton.rclone.extra_args", []) or [])
        self.timeout = int(cfg.get("proton.timeout_sec", 900))

    def _remote_path(self, path: str) -> str:
        return f"{self.remote.rstrip(':')}:{str(path).lstrip('/')}"

    def _run(self, args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
        argv = [self.binary, *args, *self.extra]
        log.debug("rclone.exec", argv=" ".join(argv))
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout or self.timeout, check=False)
        except FileNotFoundError as exc:
            raise ProtonError(f"{self.binary} not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProtonError(f"rclone timed out after {timeout or self.timeout}s") from exc
        if proc.returncode != 0:
            blob = f"{proc.stderr}\n{proc.stdout}"
            if looks_like_auth_failure(blob):
                raise AuthError(log.condense(blob))
            raise ProtonError(
                f"rclone exited {proc.returncode}: {log.condense(blob)}")
        return proc

    def auth_ok(self) -> bool:
        try:
            self._run(["lsjson", "--max-depth", "1", self._remote_path("/")], timeout=120)
            return True
        except ProtonError:
            return False

    def walk(self, root: str) -> Iterator[RemoteNode]:
        proc = self._run(["lsjson", "--recursive", "--files-only", self._remote_path(root)])
        for entry in parse_json_output(proc.stdout) or []:
            rel = entry.get("Path") or entry.get("Name")
            if not rel:
                continue
            full = posixpath.join("/" + root.strip("/"), rel) if root.strip("/") else "/" + rel
            yield RemoteNode(
                node_id=str(entry.get("ID") or f"path:{full}"),
                path=full,
                name=entry.get("Name") or posixpath.basename(rel),
                size=_coerce_int(entry.get("Size")),
                modified=_coerce_ts(entry.get("ModTime")),
                is_folder=bool(entry.get("IsDir")),
            )

    def download(self, node: RemoteNode, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run(["copyto", self._remote_path(node.path), str(dest)])
        if not dest.exists():
            raise ProtonError(f"rclone copyto finished but {dest} is missing")

    def resolve(self, path: str) -> RemoteNode | None:
        proc = self._run(["lsjson", "--stat", self._remote_path(path)], timeout=120)
        try:
            entry = parse_json_output(proc.stdout)
        except ProtonError:
            return None
        if isinstance(entry, list):
            entry = entry[0] if entry else None
        if not isinstance(entry, dict) or not entry.get("Name"):
            return None
        return RemoteNode(
            node_id=str(entry.get("ID") or f"path:{path}"),
            path="/" + str(path).lstrip("/"),
            name=str(entry.get("Name")),
            size=_coerce_int(entry.get("Size")),
            modified=_coerce_ts(entry.get("ModTime")),
            is_folder=bool(entry.get("IsDir")),
        )

    def trash(self, path: str) -> None:
        """Not supported, on purpose.

        rclone's protondrive backend deletes rather than trashing, and the one
        inviolable rule of this path is "trash, never permanent" -- Proton's
        trash is the undo. There is no safe way to honour that here, so an
        rclone install stays `delete_action: mark_only`.
        """
        raise ProtonError(
            "the rclone backend cannot move a file to Proton's trash, only "
            "delete it permanently; use proton.backend: proton-cli for "
            "delete.action: execute, or keep delete.action: mark_only")


def get_backend(cfg) -> Backend:
    name = cfg.get("proton.backend", "proton-cli")
    if name == "rclone":
        return RcloneBackend(cfg)
    if name == "proton-cli":
        return ProtonCliBackend(cfg)
    raise ProtonError(f"unknown proton.backend: {name!r}")
