"""Config loading.

YAML via PyYAML when it is installed; otherwise a small subset parser that
handles exactly the shapes used by config.example.yaml (nested maps, scalars,
lists of scalars). Keeping this dependency-free matters: this runs unattended
on a box that is awkward to debug.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "staging": {
        "root": "/mnt/immich/staging",
        # Abort a download pass when free space on the staging filesystem would
        # drop below this. The SSD is shared with Immich's own data.
        "min_free_gb": 20,
    },
    "proton": {
        # "proton-cli" (the official Bun binary) or "rclone" (Phase 0 fallback).
        "backend": "proton-cli",
        "binary": "proton-drive",
        # Defaults to <staging.root>/.proton when left unset.
        "cache_dir": None,
        # PROTON_DRIVE_CREDENTIALS_STORE. Verified values (cli-drive@0.6.0):
        #   keychain    -- libsecret/Secret Service (default, needs a keyring)
        #   unsafe_file -- plaintext session file in the cache dir (headless)
        #   pass        -- the Unix `pass` store (GPG-backed, headless)
        "credentials_store": "keychain",
        # Each entry is walked recursively. Name one parent folder to take
        # everything under it, or list individual folders to sync a subset
        # (handy for spreading a backfill over several nights).
        # `proton-drive filesystem list /` prints the top-level sections.
        "roots": ["/my-files/Photos"],
        "timeout_sec": 900,
        "max_depth": 25,
        # Preferred filter: the CLI reports mediaType (image/jpeg, video/mp4).
        # Extensions below are the fallback when no mediaType is present.
        "media_type_prefixes": ["image/", "video/"],
        # Skip anything not in this list. Empty list = accept everything.
        "extensions": [
            ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".avif",
            ".dng", ".raw", ".cr2", ".cr3", ".nef", ".arw", ".rw2", ".orf",
            ".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".webm",
        ],
        "exclude_globs": [".*", "*/.trash/*", "*/.trashed/*"],
        # Session probe: cli-drive has no `auth status`, so listing the
        # top-level sections stands in for one.
        "auth_probe_path": "/",
        # PROTON_DRIVE_LOG_LEVEL: DEBUG | INFO | WARNING | ERROR. The CLI
        # defaults to DEBUG and writes proton-drive.log into cache_dir with no
        # rotation, which is not what you want on a nightly unattended run.
        "cli_log_level": "WARNING",
        # `sync.py login` serves the sign-in URL as a redirect on this port so
        # a phone can reach it. Deliberately not Immich's 2283.
        "login_redirect_port": 8399,
        "login_timeout_sec": 300,
        # Argument templates, verified against cli-drive@0.6.0. Overridable so
        # a future flag change needs a config edit, not a code change.
        # {path} = remote path, {dest_dir} = local destination FOLDER.
        "cmd": {
            "list": ["filesystem", "list", "{path}", "--json"],
            "download": ["filesystem", "download", "-c", "skip",
                         "{path}", "{dest_dir}"],
        },
        "rclone": {
            "binary": "rclone",
            "remote": "protondrive:",
            "extra_args": [],
        },
    },
    "immich": {
        # Must include the /api suffix.
        "url": "http://127.0.0.1:2283/api",
        "api_key": "",
        # "cli" = docker run immich-cli (as per the build plan),
        # "api" = direct multipart upload over REST (no docker needed).
        "upload_mode": "cli",
        "docker_binary": "docker",
        "image": "ghcr.io/immich-app/immich-cli:latest",
        "concurrency": 4,
        # Open decision 1: flat album, folder-derived albums, or neither.
        # "flat" uses album_name, "folder" uses the CLI's folder-derived albums.
        "album_strategy": "flat",
        "album_name": "Proton Import",
        "extra_args": [],
        "timeout_sec": 3600,
        "http_timeout_sec": 120,
        # Hardlink each batch into its own dir so a push only touches the rows
        # it selected. "ready" mounts staging/ready wholesale instead.
        "batch_mode": "hardlink",
        # Ask Immich whether it already holds a file, using the sha1 Proton
        # reports at discovery, and skip downloading it if so. Saves an entire
        # transfer per already-imported file; off by default because Proton
        # reports the digest as unverified (sha1Verified: false).
        "precheck_claimed_digests": False,
    },
    "limits": {
        "max_files": 500,
        "max_bytes": 20_000_000_000,
        "max_attempts": 5,
        "backoff_base_sec": 300,
        "backoff_cap_sec": 86400,
    },
    "backfill": {
        "max_files": 5000,
        "max_bytes": 60_000_000_000,
    },
    "reap": {
        # Keep verified originals in staging this long before deleting.
        # Set to 0 once the pipeline has earned trust.
        "keep_days": 7,
        # Discard abandoned incoming/ and batch/ dirs after this many hours.
        "scratch_max_age_hours": 48,
    },
    "report": {
        # Defaults to <staging.root>/.state/status.json when left unset.
        "status_path": None,
        "stale_success_hours": 48,
    },
    "mqtt": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 1883,
        "username": "",
        "password": "",
        "discovery_prefix": "homeassistant",
        "node_id": "proton_immich_sync",
        "state_topic": "proton_immich_sync/state",
        "mosquitto_pub": "mosquitto_pub",
        "retain": True,
    },
    "logging": {
        "json": True,
        "verbose": False,
    },
}


# Enforced by the CLI itself; it exits with
# "Invalid PROTON_DRIVE_CREDENTIALS_STORE: ... Expected one of: ..."
CREDENTIALS_STORES = ("keychain", "unsafe_file", "pass")


class ConfigError(Exception):
    pass


def _parse_scalar(token: str) -> Any:
    t = token.strip()
    if t == "":
        return None
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~"):
        return None
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(p) for p in inner.split(",")]
    try:
        return int(t.replace("_", ""))
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _strip_comment(line: str) -> str:
    if line.lstrip().startswith("#"):
        return ""
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and i > 0 and line[i - 1] in " \t":
            break
        out.append(ch)
    return "".join(out)


def _mini_yaml(text: str) -> dict[str, Any]:
    """Parse the subset of YAML used by config.example.yaml.

    Supports nested maps, scalars, and lists of scalars (indented or not).
    A map nested inside a list item is not supported -- nothing here needs it.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    pending: tuple[int, dict[str, Any], str] | None = None

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        is_item = body.startswith("- ") or body == "-"

        if pending is not None:
            p_indent, p_container, p_key = pending
            if is_item and indent >= p_indent:
                child: Any = []
                p_container[p_key] = child
                stack.append((indent, child))
            elif indent > p_indent:
                child = {}
                p_container[p_key] = child
                stack.append((indent, child))
            else:
                p_container[p_key] = None
            pending = None

        while len(stack) > 1:
            frame_indent, frame = stack[-1]
            if indent < frame_indent:
                stack.pop()
                continue
            if isinstance(frame, list) and not is_item and indent <= frame_indent:
                stack.pop()
                continue
            break

        container = stack[-1][1]

        if is_item:
            if not isinstance(container, list):
                raise ConfigError(f"line {lineno}: list item outside a list")
            value = body[2:] if len(body) > 1 else ""
            if ":" in value and not value.strip().startswith(("\"", "'")):
                raise ConfigError(
                    f"line {lineno}: maps inside lists are not supported "
                    f"by the built-in parser -- install PyYAML"
                )
            container.append(_parse_scalar(value))
            continue

        if ":" not in body:
            raise ConfigError(f"line {lineno}: expected 'key: value', got {body!r}")
        if not isinstance(container, dict):
            raise ConfigError(f"line {lineno}: mapping where a list was expected")

        key, _, rest = body.partition(":")
        key, rest = key.strip(), rest.strip()
        if rest == "":
            pending = (indent, container, key)
        else:
            container[key] = _parse_scalar(rest)

    if pending is not None:
        p_indent, p_container, p_key = pending
        p_container[p_key] = None

    return root


def parse_yaml(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        return _mini_yaml(text)
    data = yaml.safe_load(text)
    return data or {}


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        elif v is not None or k not in out:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self.data = data
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    # -- derived paths -----------------------------------------------------
    @property
    def staging(self) -> Path:
        return Path(self.get("staging.root"))

    @property
    def state_dir(self) -> Path:
        return self.staging / ".state"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.sqlite"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "sync.lock"

    @property
    def incoming_dir(self) -> Path:
        return self.staging / "incoming"

    @property
    def ready_dir(self) -> Path:
        return self.staging / "ready"

    @property
    def batch_dir(self) -> Path:
        return self.staging / "batch"

    @property
    def proton_cache_dir(self) -> Path:
        configured = self.get("proton.cache_dir")
        return Path(configured) if configured else self.staging / ".proton"

    @property
    def status_path(self) -> Path:
        configured = self.get("report.status_path")
        return Path(configured) if configured else self.state_dir / "status.json"

    def validate(self) -> list[str]:
        problems = []
        url = str(self.get("immich.url", ""))
        if not url:
            problems.append("immich.url is empty")
        elif not url.rstrip("/").endswith("/api"):
            problems.append(
                f"immich.url must include the /api suffix (got {url!r})"
            )
        if not self.get("immich.api_key"):
            problems.append(
                "immich.api_key is empty (set it in the config or via IMMICH_API_KEY)"
            )
        if not self.get("proton.roots"):
            problems.append("proton.roots is empty -- nothing to walk")
        if self.get("proton.backend") not in ("proton-cli", "rclone"):
            problems.append("proton.backend must be 'proton-cli' or 'rclone'")
        if self.get("immich.upload_mode") not in ("cli", "api"):
            problems.append("immich.upload_mode must be 'cli' or 'api'")
        store = self.get("proton.credentials_store")
        if store and store not in CREDENTIALS_STORES:
            problems.append(
                f"proton.credentials_store must be one of {', '.join(CREDENTIALS_STORES)}")
        return problems


def apply_env(cfg: Config) -> None:
    """Secrets belong in the environment, not in a file on the SSD."""
    if os.environ.get("IMMICH_API_KEY"):
        cfg.set("immich.api_key", os.environ["IMMICH_API_KEY"])
    if os.environ.get("IMMICH_INSTANCE_URL"):
        cfg.set("immich.url", os.environ["IMMICH_INSTANCE_URL"])
    if os.environ.get("PROTON_DRIVE_CACHE_DIR"):
        cfg.set("proton.cache_dir", os.environ["PROTON_DRIVE_CACHE_DIR"])
    if os.environ.get("PIS_STAGING_ROOT"):
        cfg.set("staging.root", os.environ["PIS_STAGING_ROOT"])


def load(path: str | Path | None) -> Config:
    data = copy.deepcopy(DEFAULTS)
    resolved: Path | None = None
    if path:
        resolved = Path(path).expanduser()
        if not resolved.exists():
            raise ConfigError(f"config file not found: {resolved}")
        data = deep_merge(data, parse_yaml(resolved.read_text(encoding="utf-8")))
    cfg = Config(data, resolved)
    apply_env(cfg)
    return cfg
