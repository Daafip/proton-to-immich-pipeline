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
        # Files per `filesystem download` invocation. The CLI costs ~1.2 s of
        # Bun and SDK startup per call whatever it does, so a 25k-file
        # backfill spends most of a working day just starting processes; one
        # call per batch is the only lever on that. Batches are grouped by
        # source folder and never contain two files of the same name, because
        # they all land in one destination folder. Set to 1 to go back to one
        # invocation per file.
        "download_batch_size": 25,
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
        # Argument templates, verified against cli-drive@0.6.0 and 0.8.0.
        # Overridable so a flag change needs a config edit, not a code change.
        # {path} = remote path, {dest_dir} = local destination FOLDER.
        # Long-form --conflict-strategy deliberately: 0.6.0 takes either
        # spelling, 0.8.0 dropped the -c alias and errors on it.
        "cmd": {
            "list": ["filesystem", "list", "{path}", "--json"],
            "download": ["filesystem", "download", "--conflict-strategy", "skip",
                         "{path}", "{dest_dir}"],
            # v2, for the delete path. `trash` is reversible; `delete` and
            # `empty-trash` are not and are never invoked.
            "info": ["filesystem", "info", "{path}", "--json"],
            "trash": ["filesystem", "trash", "{path}"],
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
        # docker-level flags, inserted after `docker run --rm`. Left empty,
        # a loopback immich.url gets `--network host` added automatically so
        # the container can reach Immich on the host.
        "docker_args": [],
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
    # v2: one account for now; `accounts:` turns this into a list. Everything
    # that belongs to a Proton identity -- session, staging subtree, Immich key
    # -- is resolved through an Account object, never through a global.
    "account": {
        "name": "default",
    },
    "accounts": None,
    "state": {
        # Where the databases live: one <account>.sqlite per pipeline, all in
        # this one directory. Defaults to <staging.root>/.state, and every
        # account is pinned back to the same directory so the UI can find them
        # all with a single read-only mount.
        #
        # Each ingesting process writes only its own file. That is what lets
        # two pipelines run as two containers without sharing a write lock.
        "dir": None,
    },
    "reconcile": {
        # Runs as the last step of every `run`: ask Immich what is in its trash
        # and stage the matching Proton nodes for deletion. Never mutates
        # Proton -- it only ever adds rows to the staged list.
        "enabled": True,
        # /search/metadata takes one type per request.
        "types": ["IMAGE", "VIDEO"],
        "page_size": 250,
        # Stop after this many pages per type; a runaway pagination bug would
        # otherwise loop against the server all night.
        "max_pages": 400,
    },
    "delete": {
        # "mark_only" records that you deleted it in Proton yourself.
        # "execute" calls `proton-drive filesystem trash`.
        #
        # Proton's support docs say items in the Photos section cannot be
        # deleted from desktop apps. That is unverified from the CLI, so the
        # safe default is mark_only; /my-files/... roots are the execute case.
        "action": "mark_only",
        # Hard cap per invocation, whatever the UI asks for.
        "batch_cap": 50,
    },
    # The loop a pipeline container runs: it owns both the schedule and the
    # job queue, so a container needs no cron and the UI needs no way to
    # execute anything. Unused by the systemd layout, where a timer starts
    # `sync.py run` instead.
    "agent": {
        # Daily run time, local to the container (set TZ). Empty = no
        # schedule, jobs only.
        "at": "03:15",
        # Spread two pipelines sharing one Proton fair-use budget.
        "jitter_sec": 1800,
        # How often to look for work queued by the UI.
        "poll_sec": 5,
        "job_timeout_sec": 28800,
        # Run once at startup rather than waiting for the first `at`. Handy
        # for a first backfill; noisy as a permanent setting, because every
        # container restart triggers a run.
        "run_on_start": False,
    },
    "web": {
        "bind": "127.0.0.1",
        "port": 8080,
        # How a "Sync now" click reaches a pipeline:
        #   subprocess -- a worker thread here spawns sync.py. Bare metal.
        #   systemd    -- systemctl start <unit>, via a narrow sudoers entry.
        #   queue      -- write the job row and stop. The pipeline's own agent
        #                 picks it up. The only one that works across a
        #                 container boundary, and the one the compose file
        #                 uses: no docker socket, no sudo, no cross-container
        #                 exec.
        "job_runner": "subprocess",
        "systemd_unit": "proton-to-immich-pipeline@{account}.service",
        "systemctl": "systemctl",
        "sudo": "sudo",
        # scrypt hash from `sync.py web-password`. Set PIS_WEB_PASSWORD to
        # supply a plaintext password instead; it is hashed at load time and
        # never written anywhere.
        "password_hash": "",
        "session_hours": 168,
        # Cookie signing key. Generated and kept in the state dir when unset,
        # so sessions survive a restart.
        "secret_file": None,
        "poll_active_sec": 3,
        "poll_idle_sec": 30,
    },
    "limits": {
        "max_files": 500,
        "max_bytes": 20_000_000_000,
        "max_attempts": 5,
        "backoff_base_sec": 300,
        "backoff_cap_sec": 86400,
        # Global circuit breaker: stop a pass after this many consecutive
        # failures. Per-asset backoff does not help when Proton starts
        # rate-limiting mid-backfill -- every file fails in quick succession
        # and each burns an attempt, so one bad night quarantines hundreds of
        # files that were never broken. Tripping leaves the rows not reached
        # with their attempts intact. 0 disables it.
        "consecutive_failures": 25,
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

# The account a single-account install owns. Its lock and status.json keep
# their v1 filenames so an existing deployment's paths and HA sensors survive
# the upgrade.
DEFAULT_ACCOUNT = "default"

# A root reaches the CLI as a literal path. Neither the shell (it comes from
# YAML) nor the CLI expands these, so one in a root is always a mistake.
GLOB_CHARS = ("*", "?", "[")

DELETE_ACTIONS = ("mark_only", "execute")
JOB_RUNNERS = ("subprocess", "systemd", "queue")

# Shorthand keys accepted in an `accounts:` entry, mapped onto the dotted
# config paths they stand for. The plan writes accounts this way; the long
# nested form works too and both merge into the same thing.
# Anything an account's settings can be written in terms of its own name.
# Expanded once, when the Account is built, so the shared config can say the
# per-account paths a single time:
#
#   staging:  {root: /staging/{account}}
#   immich:   {api_key_file: /secrets/{account}.key}
#   accounts: [{name: david}, {name: mirjam}, {name: alice}]
#
# Adding a person is then one line, and the three things that must differ per
# account cannot be copy-pasted wrong.
ACCOUNT_TOKEN = "{account}"

ACCOUNT_SHORTHAND = {
    "proton_cache_dir": "proton.cache_dir",
    "proton_secrets": "proton.secrets_file",
    "proton_root": "proton.roots",
    "proton_roots": "proton.roots",
    "staging_dir": "staging.root",
    "immich_api_key_file": "immich.api_key_file",
    "immich_api_key": "immich.api_key",
    "immich_url": "immich.url",
    "album_name": "immich.album_name",
    "delete_action": "delete.action",
    "credentials_store": "proton.credentials_store",
}


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
        """Where state.sqlite lives. Shared by every account.

        Defaults under staging, but stays explicit for an Account so that
        giving one its own staging subtree cannot fork the database.
        """
        configured = self.get("state.dir")
        return Path(configured) if configured else self.staging / ".state"

    @property
    def db_path(self) -> Path:
        """This account's own database.

        One file per pipeline, all in the shared `state.dir`. Each ingesting
        process writes only its own file, so two pipelines -- two containers,
        two systemd units, whatever -- never contend for a write lock and a
        bug in one cannot reach the other's rows. The UI mounts the directory
        read-only and combines them.

        The `account` column stays inside each file even though it is now
        redundant there: it keeps a file self-describing, and it is what makes
        splitting and merging databases possible in either direction.
        """
        return self.state_dir / f"{self.account_name}.sqlite"

    @property
    def legacy_db_path(self) -> Path:
        """The single shared database used before the per-pipeline split.

        Only the migration looks at this. Its presence alongside missing
        per-account files is what `sync.py migrate` detects.
        """
        return self.state_dir / "state.sqlite"

    def db_paths(self) -> dict[str, Path]:
        """account name -> its database, for every configured account."""
        return {a.account_name: a.db_path for a in self.accounts}

    @property
    def lock_path(self) -> Path:
        """One lock per account -- accounts run sequentially by timer, but a
        forced run for one must not be refused because another is working."""
        name = self.account_name
        stem = "sync" if name == DEFAULT_ACCOUNT else f"sync-{name}"
        return self.state_dir / f"{stem}.lock"

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
        if configured:
            return Path(configured)
        name = self.account_name
        stem = "status" if name == DEFAULT_ACCOUNT else f"status-{name}"
        return self.state_dir / f"{stem}.json"

    # -- account identity --------------------------------------------------
    @property
    def account_name(self) -> str:
        return str(self.get("account.name") or DEFAULT_ACCOUNT)

    @property
    def delete_action(self) -> str:
        return str(self.get("delete.action") or "mark_only")

    def immich_api_key(self) -> str:
        """The key for this account, read from a file when one is named.

        A file rather than a literal is how two accounts keep two keys without
        either appearing in the config on the SSD. Read on demand, never
        cached into the config data, so it cannot be logged with it.
        """
        path = self.get("immich.api_key_file")
        if path:
            try:
                return Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(
                    f"cannot read immich.api_key_file {path}: {exc}") from exc
        return str(self.get("immich.api_key", ""))

    # -- accounts ----------------------------------------------------------
    @property
    def accounts(self) -> list["Account"]:
        """Every account this config describes, in config order.

        There is always at least one. A bare config yields a single account
        named by `account.name`; an `accounts:` list yields one per entry,
        each a full Config in its own right whose Proton session, staging
        subtree and Immich key are the same object -- which is the whole point.
        """
        return build_accounts(self)

    def account(self, name: str | None) -> "Account":
        found = self.accounts
        if name is None:
            if len(found) > 1:
                raise ConfigError(
                    f"this config has {len(found)} accounts "
                    f"({', '.join(a.account_name for a in found)}); "
                    f"name one with --account")
            return found[0]
        for candidate in found:
            if candidate.account_name == name:
                return candidate
        raise ConfigError(
            f"unknown account {name!r}; configured: "
            f"{', '.join(a.account_name for a in found)}")

    def validate(self, scope: str = "pipeline") -> list[str]:
        """Config problems, in the voice of something a human has to fix.

        With an `accounts:` list the per-identity checks run against each
        account's merged view, not the shared base -- an Immich key that lives
        only in an account entry is not a missing key. The cross-account
        checks then run once over the whole set.

        `scope="ui"` drops the credential checks. The web UI reads databases
        and writes job rows; it never calls Proton or Immich, so it has no
        business holding anyone's API key -- and in the container layout it
        deliberately does not, because the keys belong to the pipelines. A
        missing key is not a reason to refuse to show a status page.
        """
        entries = self.get("accounts")
        if not entries:
            return self._validate_one(scope)
        try:
            accounts = self.accounts
        except ConfigError as exc:
            return [str(exc)]
        problems: list[str] = []
        for account in accounts:
            problems += [f"accounts[{account.account_name}]: {p}"
                         for p in account._validate_one(scope)]
        return problems + self._validate_cross_account(accounts, scope)

    def _validate_one(self, scope: str = "pipeline") -> list[str]:
        """Checks that make sense for exactly one account's settings."""
        problems = []
        if scope != "pipeline":
            # Structural only: these are the settings the UI itself uses.
            if self.delete_action not in DELETE_ACTIONS:
                problems.append(
                    f"delete.action must be one of {', '.join(DELETE_ACTIONS)} "
                    f"(got {self.delete_action!r})")
            runner = self.get("web.job_runner")
            if runner and runner not in JOB_RUNNERS:
                problems.append(
                    f"web.job_runner must be one of {', '.join(JOB_RUNNERS)} "
                    f"(got {runner!r})")
            return problems
        url = str(self.get("immich.url", ""))
        if not url:
            problems.append("immich.url is empty")
        elif not url.rstrip("/").endswith("/api"):
            problems.append(
                f"immich.url must include the /api suffix (got {url!r})"
            )
        if not (self.get("immich.api_key") or self.get("immich.api_key_file")):
            problems.append(
                "immich.api_key is empty (set it in the config, point "
                "immich.api_key_file at a file, or export IMMICH_API_KEY)"
            )
        roots = self.get("proton.roots") or []
        if not roots:
            problems.append("proton.roots is empty -- nothing to walk")
        for root in roots:
            hit = next((c for c in GLOB_CHARS if c in str(root)), None)
            if hit:
                # The CLI takes a path, not a pattern: it looks for a folder
                # literally named `*` and reports `Node not found: *`, which
                # is a long way from "globs are not a thing here".
                problems.append(
                    f"proton.roots entry {root!r} contains {hit!r}: roots are "
                    f"paths, not patterns, and nothing expands them. Name the "
                    f"parent folder -- every root is walked recursively -- or "
                    f"list the subfolders individually")
        if self.get("proton.backend") not in ("proton-cli", "rclone"):
            problems.append("proton.backend must be 'proton-cli' or 'rclone'")
        if self.get("immich.upload_mode") not in ("cli", "api"):
            problems.append("immich.upload_mode must be 'cli' or 'api'")
        store = self.get("proton.credentials_store")
        if store and store not in CREDENTIALS_STORES:
            problems.append(
                f"proton.credentials_store must be one of {', '.join(CREDENTIALS_STORES)}")
        if self.delete_action not in DELETE_ACTIONS:
            problems.append(
                f"delete.action must be one of {', '.join(DELETE_ACTIONS)} "
                f"(got {self.delete_action!r})")
        runner = self.get("web.job_runner")
        if runner and runner not in JOB_RUNNERS:
            problems.append(
                f"web.job_runner must be one of {', '.join(JOB_RUNNERS)} "
                f"(got {runner!r})")
        return problems

    def _validate_cross_account(self, found: list["Account"],
                                scope: str = "pipeline") -> list[str]:
        """The hard rule, enforced: no two accounts may share a staging subtree
        or an Immich key. Either mistake uploads one person's photos into the
        other's library, which is tedious to unpick after the fact."""
        problems: list[str] = []
        if len(found) < 2:
            return problems
        for field, label in (("staging", "staging.root"),
                             ("cache", "proton.cache_dir")):
            seen: dict[str, str] = {}
            for account in found:
                key = str(account.staging if field == "staging"
                          else account.proton_cache_dir)
                if key in seen:
                    problems.append(
                        f"accounts {seen[key]!r} and {account.account_name!r} "
                        f"share {label} {key!r}; each account needs its own")
                seen[key] = account.account_name
        if scope != "pipeline":
            # The UI holds no keys, so it cannot check them -- and two
            # accounts sharing a staging dir is still worth saying.
            return problems
        keys: dict[str, str] = {}
        for account in found:
            literal = str(account.get("immich.api_key") or "")
            key_file = str(account.get("immich.api_key_file") or "")
            token = key_file or literal
            if not token:
                continue
            if token in keys:
                problems.append(
                    f"accounts {keys[token]!r} and {account.account_name!r} "
                    f"share one Immich API key; each person needs their own user")
            keys[token] = account.account_name
        return problems


class Account(Config):
    """One Proton identity and everything that belongs to it.

    The v2 plan's hard rule: *the Proton session, the staging dir and the
    Immich API key travel as one object, never as separate globals.* This is
    that object. It is a full Config, so every `cfg.get("proton.…")` call site
    in the pipeline keeps working unchanged -- it just resolves against this
    account's merged view instead of a shared one.

    The failure mode being designed out is uploading one person's photos into
    the other's library. No code path should ever take a staging path from one
    Account and a key from another, and because both come off the same object
    there is nothing to mismatch.
    """

    def __init__(self, data: dict[str, Any], name: str, path: Path | None = None,
                 explicit: bool = False):
        super().__init__(data, path)
        self.set("account.name", name)
        # True when this account came from an `accounts:` list rather than
        # being the implicit single account. Decides whether its status.json
        # keeps the v1 filename.
        self.explicit = explicit

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Account {self.account_name} staging={self.staging}>"


def expand_account_token(value: Any, name: str) -> Any:
    """Replace the literal `{account}` in every string, recursively.

    A plain replace rather than str.format: `proton.cmd` carries `{path}` and
    `{dest_dir}` templates that format() would raise on, and a filesystem path
    is not a format string.
    """
    if isinstance(value, str):
        return value.replace(ACCOUNT_TOKEN, name)
    if isinstance(value, dict):
        return {k: expand_account_token(v, name) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_account_token(v, name) for v in value]
    return value


def _expand_shorthand(entry: dict[str, Any]) -> dict[str, Any]:
    """Turn an `accounts:` entry into an ordinary nested config overlay."""
    overlay: dict[str, Any] = {}
    for key, value in entry.items():
        if key == "name":
            continue
        dotted = ACCOUNT_SHORTHAND.get(key)
        if dotted is None:
            # Already a nested section (proton:, immich:, staging:, …).
            overlay[key] = value
            continue
        if dotted == "proton.roots" and not isinstance(value, list):
            value = [value]
        node = overlay
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return overlay


def build_accounts(cfg: Config) -> list[Account]:
    """Resolve a config into one Account per identity.

    A bare config is a single account: the same data, named by `account.name`.
    An `accounts:` list merges each entry over the shared config, so global
    `proton:` and `immich:` settings stay in one place and an entry only names
    what differs.

    Every account is pinned to the shared `state.dir`, whatever it does with
    staging. One database, rows scoped by account -- splitting it per account
    would make the UI's cross-account view impossible and lose the guarantee
    that a node id is only ever interpreted against its own volume.
    """
    shared_state_dir = str(cfg.get("state.dir") or (Path(
        cfg.get("staging.root", "/mnt/immich/staging")) / ".state"))

    entries = cfg.get("accounts")
    if not entries:
        name = str(cfg.get("account.name") or DEFAULT_ACCOUNT)
        data = copy.deepcopy(cfg.data)
        data.pop("accounts", None)
        data = expand_account_token(data, name)
        # After expansion: the state directory is shared, so a `{account}` in
        # it would split the database the UI is meant to read as one set.
        data.setdefault("state", {})["dir"] = shared_state_dir
        return [Account(data, name, cfg.path)]

    if not isinstance(entries, list):
        raise ConfigError("accounts: must be a list of account entries")

    base = copy.deepcopy(cfg.data)
    base.pop("accounts", None)
    out: list[Account] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"accounts[{index}] must be a map with at least a name "
                f"(got {type(entry).__name__}); the built-in YAML parser "
                f"cannot read maps inside lists -- install PyYAML")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ConfigError(f"accounts[{index}] has no name")
        if name in seen:
            raise ConfigError(f"duplicate account name {name!r}")
        seen.add(name)
        data = expand_account_token(deep_merge(base, _expand_shorthand(entry)),
                                    name)
        data.setdefault("state", {})["dir"] = shared_state_dir
        # A per-account status.json, unless the entry named one itself.
        out.append(Account(data, name, cfg.path, explicit=True))
    return out


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
    if os.environ.get("PIS_ACCOUNT"):
        cfg.set("account.name", os.environ["PIS_ACCOUNT"])


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
