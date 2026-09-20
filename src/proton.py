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
from typing import Any, Iterator

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
_SIZE_KEYS = ("size", "sizeBytes", "size_bytes", "fileSize", "totalSize", "Size")
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

    return RemoteNode(
        node_id=str(node_id),
        path=path,
        name=str(name),
        size=_coerce_int(_first(obj, _SIZE_KEYS)),
        modified=_coerce_ts(_first(obj, _MTIME_KEYS)),
        is_folder=is_folder_entry(obj),
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


_UNSAFE_FILENAME = re.compile(r"[/\\\x00]")


def safe_filename(name: str, fallback: str = "asset") -> str:
    """A remote name is untrusted input for the local filesystem.

    Proton names may contain / (the CLI escapes it with a backslash), and an
    undecryptable name falls back to a uid, which is base64 and can contain /.
    """
    cleaned = _UNSAFE_FILENAME.sub("_", str(name or "")).strip().strip(".")
    cleaned = cleaned[:200]
    return cleaned or fallback


def should_include(node: RemoteNode, extensions: list[str], exclude_globs: list[str]) -> bool:
    if node.is_folder:
        return False
    if extensions and node.ext not in {e.lower() for e in extensions}:
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


class ProtonCliBackend(Backend):
    name = "proton-cli"

    def __init__(self, cfg):
        self.binary = cfg.get("proton.binary", "proton-drive")
        self.cache_dir = cfg.proton_cache_dir
        self.timeout = int(cfg.get("proton.timeout_sec", 900))
        self.max_depth = int(cfg.get("proton.max_depth", 25))
        self.cmd = cfg.get("proton.cmd", {})
        self.credentials_store = cfg.get("proton.credentials_store")
        self.auth_probe_path = cfg.get("proton.auth_probe_path", "/")

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
        return env

    def _template(self, key: str, default: list[str], **subs: str) -> list[str]:
        template = self.cmd.get(key) or default
        return [str(part).format(**subs) for part in template]

    def _run(self, args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
        argv = [self.binary, *args]
        log.debug("proton.exec", argv=" ".join(argv))
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=timeout or self.timeout, env=self._env(), check=False,
            )
        except FileNotFoundError as exc:
            raise ProtonError(f"{self.binary} not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProtonError(f"{self.binary} timed out after {timeout or self.timeout}s") from exc
        if proc.returncode != 0:
            blob = f"{proc.stderr}\n{proc.stdout}"
            if looks_like_auth_failure(blob):
                raise AuthError(blob.strip()[:500])
            raise ProtonError(
                f"{' '.join(argv)} exited {proc.returncode}: {blob.strip()[:500]}")
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

    def walk(self, root: str) -> Iterator[RemoteNode]:
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

    def download(self, node: RemoteNode, dest: Path) -> None:
        """`filesystem download path... localFolder` -- the destination is a
        FOLDER, so the CLI decides the filename. The caller still names the
        file it wants; we rename afterwards.

        -c skip matters: without a conflict strategy the CLI prompts, which
        would hang an unattended run forever.
        """
        dest_dir = dest.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        args = self._template(
            "download",
            ["filesystem", "download", "-c", "skip", "{path}", "{dest_dir}"],
            path=node.path, dest=str(dest), dest_dir=str(dest_dir),
        )
        self._run(args)
        produced = dest_dir / node.name
        if not dest.exists() and produced.exists():
            produced.replace(dest)
        if not dest.exists():
            listing = ", ".join(sorted(p.name for p in dest_dir.iterdir())) or "nothing"
            raise ProtonError(
                f"download reported success but {dest.name} is missing "
                f"(folder holds: {listing})")


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
                raise AuthError(blob.strip()[:500])
            raise ProtonError(f"rclone exited {proc.returncode}: {blob.strip()[:500]}")
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


def get_backend(cfg) -> Backend:
    name = cfg.get("proton.backend", "proton-cli")
    if name == "rclone":
        return RcloneBackend(cfg)
    if name == "proton-cli":
        return ProtonCliBackend(cfg)
    raise ProtonError(f"unknown proton.backend: {name!r}")
