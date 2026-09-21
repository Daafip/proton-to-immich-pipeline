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
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import log


class ImmichError(Exception):
    pass


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
        self.api_key = str(cfg.get("immich.api_key", ""))
        self.timeout = int(cfg.get("immich.http_timeout_sec", 120))
        self.device_id = str(cfg.get("immich.device_id", "proton-to-immich-pipeline"))
        self._checksum_format: str | None = None

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
            raise ImmichError(f"{method} {url} -> {exc.code}: {detail}") from exc
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


class ImmichCliUploader:
    """`docker run --rm ghcr.io/immich-app/immich-cli upload --recursive /import`."""

    def __init__(self, cfg):
        self.docker = cfg.get("immich.docker_binary", "docker")
        self.image = cfg.get("immich.image", "ghcr.io/immich-app/immich-cli:latest")
        self.url = str(cfg.get("immich.url", "")).rstrip("/")
        if self.url and not self.url.endswith("/api"):
            self.url += "/api"
        self.api_key = str(cfg.get("immich.api_key", ""))
        self.concurrency = int(cfg.get("immich.concurrency", 4))
        self.album_strategy = cfg.get("immich.album_strategy", "flat")
        self.album_name = cfg.get("immich.album_name", "Proton Import")
        self.extra_args = list(cfg.get("immich.extra_args", []) or [])
        self.timeout = int(cfg.get("immich.timeout_sec", 3600))

    def build_argv(self, import_dir: Path, dry_run: bool = False) -> list[str]:
        argv = [
            self.docker, "run", "--rm",
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

    def upload_dir(self, import_dir: Path, dry_run: bool = False) -> CliRun:
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
