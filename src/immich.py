"""Immich access.

Uploads go through the immich-cli container (the build plan's route) or, if
configured, straight over REST. Either way the *bookkeeping* -- asset ids and
duplicate detection -- comes from the REST API rather than from parsing CLI
stdout, which has no stable machine-readable form.

The pivot is /assets/bulk-upload-check, the same endpoint the CLI itself uses
for dedupe: ask it before uploading and it tells us what the server already
has (true duplicates, with their asset ids); ask it afterwards and it confirms
what landed. Checksums are sent as sha1; immich accepts hex or base64 and this
client works out which one this server wants and then remembers it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import log


class ImmichError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        # The HTTP status when there was one. Reconcile needs 404 ("Immich has
        # no such asset") told apart from 500 ("ask again later"), and matching
        # on message text for that is how a server reword becomes a deleted
        # photo.
        self.status = status


class ImmichConfigError(ImmichError):
    """The run cannot work until a human edits the config. Never charged to an
    asset's attempt budget -- nothing was attempted."""


class ImmichAuthError(ImmichError):
    pass


def sha1_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_bytes(value: str | None) -> bytes | None:
    """Immich reports checksums as base64; we store hex. Normalise both."""
    if not value:
        return None
    text = value.strip()
    if len(text) == 40:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    try:
        decoded = base64.b64decode(text + "=" * (-len(text) % 4), validate=False)
        if len(decoded) == 20:
            return decoded
    except Exception:  # noqa: BLE001 - malformed input is just "no match"
        pass
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def checksums_match(local_sha1_hex: str | None, remote: str | None) -> bool:
    a, b = checksum_bytes(local_sha1_hex), checksum_bytes(remote)
    return bool(a and b and a == b)


def _api_key(cfg) -> str:
    """The key belonging to *this* account.

    Config.immich_api_key() reads immich.api_key_file when one is set, which
    is how two accounts hold two keys without either being written into the
    config. Read here rather than cached at load time so a key never sits in
    the config data that gets logged or serialised.
    """
    reader = getattr(cfg, "immich_api_key", None)
    if callable(reader):
        return str(reader())
    return str(cfg.get("immich.api_key", ""))


@dataclass
class UploadResult:
    asset_id: str | None = None
    duplicate: bool = False
    found: bool = False
    error: str | None = None


@dataclass
class CliRun:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    argv: list[str] = field(default_factory=list)


class ImmichClient:
    def __init__(self, cfg):
        url = str(cfg.get("immich.url", "")).rstrip("/")
        if url and not url.endswith("/api"):
            # The plan is emphatic about this; fix it rather than fail at runtime.
            log.warn("immich.url_missing_api_suffix", url=url)
            url = url + "/api"
        self.base_url = url
        self.api_key = _api_key(cfg)
        self.timeout = int(cfg.get("immich.http_timeout_sec", 120))
        self.device_id = str(cfg.get("immich.device_id", "proton-to-immich-pipeline"))
        self._checksum_format: str | None = None
        # Set by every search_trashed(): False when pagination hit max_pages,
        # so the list returned is a prefix of the trash rather than all of it.
        # Callers that reason about *absence* from the trash must check this.
        self.trash_scan_complete = True

    # -- transport ---------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        raw: bool = False,
        timeout: int | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        hdrs = {"x-api-key": self.api_key, "Accept": "application/json"}
        data: bytes | Any = None
        if body is not None and not raw:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif raw:
            data = body
        hdrs.update(headers or {})

        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code in (401, 403):
                raise ImmichAuthError(f"{exc.code} {detail}") from exc
            raise ImmichError(f"{method} {url} -> {exc.code}: {detail}",
                              status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise ImmichError(f"{method} {url} failed: {exc.reason}") from exc
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload.decode("utf-8", "replace")

    # -- health ------------------------------------------------------------
    def ping(self) -> bool:
        try:
            return bool(self._request("GET", "/server/ping", timeout=30))
        except ImmichError:
            return False

    def auth_ok(self) -> bool:
        try:
            self._request("GET", "/users/me", timeout=30)
            return True
        except ImmichError:
            return False

    # -- dedupe / lookup ---------------------------------------------------
    def _bulk_check_once(self, pairs: Sequence[tuple[str, str]], fmt: str) -> dict[str, UploadResult]:
        assets = []
        for key, sha1_hex in pairs:
            checksum = sha1_hex
            if fmt == "base64":
                raw = checksum_bytes(sha1_hex)
                checksum = base64.b64encode(raw).decode() if raw else sha1_hex
            assets.append({"id": key, "checksum": checksum})
        payload = self._request("POST", "/assets/bulk-upload-check", {"assets": assets})

        out: dict[str, UploadResult] = {}
        for item in (payload or {}).get("results", []):
            key = str(item.get("id"))
            action = str(item.get("action", "")).lower()
            reason = str(item.get("reason", "")).lower()
            asset_id = item.get("assetId") or item.get("asset_id")
            is_dupe = action == "reject" and reason in ("duplicate", "")
            out[key] = UploadResult(
                asset_id=str(asset_id) if asset_id else None,
                duplicate=is_dupe,
                found=bool(asset_id) or is_dupe,
                error=None if action in ("accept", "reject") else action,
            )
        return out

    def bulk_upload_check(self, pairs: Sequence[tuple[str, str]]) -> dict[str, UploadResult]:
        """key -> what the server already knows about that checksum."""
        if not pairs:
            return {}
        formats = [self._checksum_format] if self._checksum_format else ["hex", "base64"]
        last_error: Exception | None = None
        for fmt in formats:
            try:
                result = self._bulk_check_once(pairs, fmt)
            except ImmichAuthError:
                raise
            except ImmichError as exc:
                last_error = exc
                log.debug("immich.bulk_check_format_rejected", fmt=fmt, detail=str(exc)[:200])
                continue
            if result:
                self._checksum_format = fmt
                return result
            last_error = ImmichError("bulk-upload-check returned no results")
        if last_error:
            raise last_error
        return {}

    def find_by_checksum(self, sha1_hex: str, filename: str | None = None) -> UploadResult:
        """Single-asset lookup, with a filename search as a second opinion."""
        try:
            result = self.bulk_upload_check([("probe", sha1_hex)])
            hit = result.get("probe")
            if hit and hit.found:
                return hit
        except ImmichError as exc:
            log.debug("immich.bulk_check_failed", detail=str(exc)[:200])

        if filename:
            try:
                payload = self._request(
                    "POST", "/search/metadata", {"originalFileName": filename, "size": 10})
                items = ((payload or {}).get("assets") or {}).get("items") or []
                for item in items:
                    if checksums_match(sha1_hex, item.get("checksum")):
                        return UploadResult(asset_id=str(item.get("id")), found=True)
            except ImmichError as exc:
                log.debug("immich.search_failed", detail=str(exc)[:200])
        return UploadResult()

    def search_trashed(
        self,
        types: Sequence[str] = ("IMAGE", "VIDEO"),
        page_size: int = 250,
        max_pages: int = 400,
    ) -> list[dict[str, Any]]:
        """Every asset currently sitting in Immich's trash.

        /search/metadata takes one `type` per request, so each is queried
        separately and the results deduped by asset id.

        Pagination is by `page`, and the server reports `nextPage` as a string
        or null. max_pages is a stop so a server that always returns a
        nextPage cannot loop against the API all night.
        """
        seen: dict[str, dict[str, Any]] = {}
        self.trash_scan_complete = True
        for asset_type in types:
            page: Any = 1
            for _ in range(max_pages):
                body = {
                    "isTrashed": True,
                    "type": asset_type,
                    "size": page_size,
                    "page": int(page),
                    # withDeleted is what makes the server include assets it
                    # considers soft-deleted; isTrashed alone filters an
                    # already-narrowed set on some versions.
                    "withDeleted": True,
                }
                payload = self._request("POST", "/search/metadata", body)
                assets = (payload or {}).get("assets") or {}
                items = assets.get("items") or []
                for item in items:
                    asset_id = item.get("id")
                    if asset_id:
                        seen[str(asset_id)] = item
                next_page = assets.get("nextPage")
                if not next_page or not items:
                    break
                page = next_page
            else:
                # Ran out of pages with the server still offering more.
                self.trash_scan_complete = False
                log.warn("immich.trash_pagination_capped", type=asset_type,
                         pages=max_pages)
        log.debug("immich.trash_scanned", assets=len(seen))
        return list(seen.values())

    def trashed_asset_ids(self, types: Sequence[str] = ("IMAGE", "VIDEO"),
                          page_size: int = 250,
                          max_pages: int = 400) -> set[str]:
        """Just the ids of everything in the trash."""
        return {str(a["id"]) for a in self.search_trashed(
            types=types, page_size=page_size, max_pages=max_pages)
            if a.get("id")}

    def asset_state(self, asset_id: str) -> str:
        """Where one asset stands: "live", "trashed", "missing" or "unknown".

        Reconcile needs to tell two situations apart that look identical from
        the trash listing alone, because in both the asset is simply absent
        from it:

        * someone **restored** the photo -- it is back in the library, and the
          Proton original must not be deleted after all;
        * Immich's 30-day purge **deleted** it -- it is gone from the server
          entirely, and the staged row is now the only surviving record that
          the Proton original was meant to go.

        "unknown" is returned for any error that is not a 404, and the caller
        treats it as "changed nothing".
        """
        try:
            payload = self._request("GET", f"/assets/{asset_id}", timeout=30)
        except ImmichAuthError:
            raise
        except ImmichError as exc:
            if exc.status == 404:
                return "missing"
            log.debug("immich.asset_lookup_failed", asset_id=str(asset_id),
                      detail=log.condense(str(exc), 200))
            return "unknown"
        if not isinstance(payload, dict) or not payload.get("id"):
            return "unknown"
        return "trashed" if payload.get("isTrashed") else "live"

    def restore_from_trash(self, asset_ids: Sequence[str],
                           chunk: int = 200) -> int:
        """Bring assets back out of Immich's trash. Returns how many were sent.

        Why this exists: Immich dedupes on checksum and a trashed asset still
        owns its checksum, so re-uploading the same bytes is rejected as a
        duplicate rather than putting the photo back. Restoring is the only
        way to get it into the library again.

        The endpoint is `POST /trash/restore/assets`, which has moved between
        Immich versions. A failure here is reported, never swallowed: the
        caller marks the asset failed rather than claiming an upload that did
        not happen.
        """
        ids = [str(a) for a in asset_ids if a]
        if not ids:
            return 0
        for start in range(0, len(ids), chunk):
            batch = ids[start:start + chunk]
            self._request("POST", "/trash/restore/assets", {"ids": batch})
        log.info("immich.restored_from_trash", assets=len(ids))
        return len(ids)

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"/assets/{asset_id}")
        except ImmichAuthError:
            raise
        except ImmichError as exc:
            if "-> 404" in str(exc):
                return None
            raise

    # -- upload over REST (upload_mode: api) -------------------------------
    def upload_file(self, path: str | Path, sha1_hex: str, device_asset_id: str | None = None) -> UploadResult:
        path = Path(path)
        stat = path.stat()
        boundary = uuid.uuid4().hex
        fields = {
            "deviceAssetId": device_asset_id or f"{path.name}-{stat.st_size}",
            "deviceId": self.device_id,
            "fileCreatedAt": _iso(stat.st_mtime),
            "fileModifiedAt": _iso(stat.st_mtime),
            "isFavorite": "false",
            "duration": "0",
        }
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        for key, value in fields.items():
            spool.write(f"--{boundary}\r\n".encode())
            spool.write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            spool.write(f"{value}\r\n".encode())
        spool.write(f"--{boundary}\r\n".encode())
        spool.write(
            f'Content-Disposition: form-data; name="assetData"; filename="{path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode()
        )
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                spool.write(chunk)
        spool.write(f"\r\n--{boundary}--\r\n".encode())
        length = spool.tell()
        spool.seek(0)

        try:
            payload = self._request(
                "POST", "/assets", body=spool, raw=True,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(length),
                    "x-immich-checksum": sha1_hex,
                },
                timeout=max(self.timeout, 600),
            )
        finally:
            spool.close()

        status = str((payload or {}).get("status", "")).lower()
        asset_id = (payload or {}).get("id")
        return UploadResult(
            asset_id=str(asset_id) if asset_id else None,
            duplicate=status == "duplicate",
            found=bool(asset_id),
        )


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


# Inside a container these all mean the container, never the host.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


class ImmichCliUploader:
    """`docker run --rm ghcr.io/immich-app/immich-cli upload --recursive /import`.

    The container gets its own network namespace, so a loopback immich.url --
    correct for upload_mode: api, and what the example config ships -- would
    resolve to the container itself. `--network host` is added in that case so
    the same URL means the same machine in both modes.
    """

    def __init__(self, cfg):
        self.docker = cfg.get("immich.docker_binary", "docker")
        self.image = cfg.get("immich.image", "ghcr.io/immich-app/immich-cli:latest")
        self.url = str(cfg.get("immich.url", "")).rstrip("/")
        if self.url and not self.url.endswith("/api"):
            self.url += "/api"
        self.api_key = _api_key(cfg)
        self.concurrency = int(cfg.get("immich.concurrency", 4))
        self.album_strategy = cfg.get("immich.album_strategy", "flat")
        self.album_name = cfg.get("immich.album_name", "Proton Import")
        self.extra_args = list(cfg.get("immich.extra_args", []) or [])
        self.docker_args = list(cfg.get("immich.docker_args", []) or [])
        self.timeout = int(cfg.get("immich.timeout_sec", 3600))

    def loopback_host(self) -> str | None:
        host = urllib.parse.urlsplit(self.url).hostname or ""
        return host if host.strip("[]").lower() in LOOPBACK_HOSTS else None

    def configured_network(self) -> str | None:
        """--network from immich.docker_args, in either spelling."""
        for i, arg in enumerate(self.docker_args):
            if arg == "--network" and i + 1 < len(self.docker_args):
                return self.docker_args[i + 1]
            if arg.startswith("--network="):
                return arg.split("=", 1)[1]
        return None

    def docker_run_args(self) -> list[str]:
        """Host networking when, and only when, the URL needs it. Immich
        publishes 2283 on the host, so sharing that namespace makes a loopback
        URL mean the host -- no LAN IP to hardcode, nothing to break on a new
        DHCP lease."""
        if self.loopback_host() and self.configured_network() is None:
            return [*self.docker_args, "--network", "host"]
        return list(self.docker_args)

    def build_argv(self, import_dir: Path, dry_run: bool = False) -> list[str]:
        argv = [
            self.docker, "run", "--rm",
            *self.docker_run_args(),
            "-v", f"{import_dir}:/import:ro",
            "-e", f"IMMICH_INSTANCE_URL={self.url}",
            "-e", f"IMMICH_API_KEY={self.api_key}",
            self.image,
            "upload", "--recursive", "/import",
            "--concurrency", str(self.concurrency),
        ]
        if self.album_strategy == "flat" and self.album_name:
            argv += ["--album-name", str(self.album_name)]
        elif self.album_strategy == "folder":
            argv += ["--album"]
        if dry_run:
            argv += ["--dry-run"]
        # Never --delete: the reaper owns deletion.
        argv += [a for a in self.extra_args if a != "--delete"]
        return argv

    def unreachable_from_container(self) -> str | None:
        """Only reachable is the configuration host networking cannot save:
        a loopback URL with some other --network pinned by hand."""
        host = self.loopback_host()
        network = self.configured_network()
        if host and network not in (None, "host"):
            return (
                f"immich.url is {self.url} but immich.docker_args pins "
                f"--network {network}; in that namespace {host} is the "
                "container itself -- use --network host, give immich.url the "
                "host's address, or set immich.upload_mode: api")
        return None

    def upload_dir(self, import_dir: Path, dry_run: bool = False) -> CliRun:
        trap = self.unreachable_from_container()
        if trap:
            raise ImmichConfigError(trap)
        argv = self.build_argv(import_dir, dry_run=dry_run)
        printable = [("IMMICH_API_KEY=***" if a.startswith("IMMICH_API_KEY=") else a)
                     for a in argv]
        log.debug("immich.cli_exec", argv=" ".join(printable))
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=self.timeout, check=False)
        except FileNotFoundError as exc:
            raise ImmichError(f"{self.docker} not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ImmichError(f"immich-cli timed out after {self.timeout}s") from exc
        run = CliRun(proc.returncode, proc.stdout or "", proc.stderr or "", printable)
        if run.returncode != 0:
            blob = f"{run.stderr}\n{run.stdout}".strip()
            detail = log.condense(blob)
            if "401" in blob or "403" in blob or "unauthorized" in blob.lower():
                raise ImmichAuthError(detail)
            raise ImmichError(f"immich-cli exited {run.returncode}: {detail}")
        return run
