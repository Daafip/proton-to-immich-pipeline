"""Fakes: a Proton backend and an Immich server, both in-process."""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.immich import CliRun, UploadResult  # noqa: E402
from src.proton import AuthError, ProtonError, RemoteNode  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def silence_logs() -> None:
    """Keep unittest output readable; the log format itself is tested elsewhere."""
    from src import log
    log._emit = lambda *a, **kw: None


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class FakeProtonBackend:
    name = "fake"

    def __init__(self, files: dict[str, bytes] | None = None, authed: bool = True):
        # path -> content
        self.files: dict[str, bytes] = files or {}
        self.meta: dict[str, dict[str, Any]] = {}
        self.authed = authed
        self.fail_paths: set[str] = set()
        self.auth_fail_paths: set[str] = set()
        self.downloads: list[str] = []

    def add(self, path: str, content: bytes, node_id: str | None = None,
            modified: str = "2026-08-01T10:00:00+00:00") -> str:
        # Re-adding a known path keeps its node id: Proton node ids are stable
        # across edits and renames, and the pipeline relies on that.
        existing = self.meta.get(path, {}).get("node_id")
        self.files[path] = content
        node_id = node_id or existing or f"node-{len(self.files)}"
        self.meta[path] = {"node_id": node_id, "modified": modified}
        return node_id

    def auth_ok(self) -> bool:
        return self.authed

    def walk(self, root: str) -> Iterator[RemoteNode]:
        if not self.authed:
            raise AuthError("not logged in")
        for path, content in sorted(self.files.items()):
            if not path.startswith(root.rstrip("/") + "/") and root != "/":
                continue
            meta = self.meta.get(path, {})
            yield RemoteNode(
                node_id=meta.get("node_id", f"path:{path}"),
                path=path,
                name=os.path.basename(path),
                size=len(content),
                modified=meta.get("modified"),
                is_folder=False,
            )

    def download(self, node: RemoteNode, dest: Path) -> None:
        if node.path in self.auth_fail_paths:
            raise AuthError("session expired")
        if node.path in self.fail_paths:
            raise ProtonError("simulated transfer failure")
        if node.path not in self.files:
            raise ProtonError("no such file")
        self.downloads.append(node.path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.files[node.path])


class FakeImmichServer:
    """Holds assets keyed by sha1, the way the real server dedupes."""

    def __init__(self):
        self.by_checksum: dict[str, str] = {}
        self.corrupt: set[str] = set()

    def add(self, sha1_hex: str, asset_id: str | None = None) -> str:
        asset_id = asset_id or str(uuid.uuid4())
        self.by_checksum[sha1_hex] = asset_id
        return asset_id

    def asset_id_for(self, sha1_hex: str) -> str | None:
        return self.by_checksum.get(sha1_hex)


class FakeImmichClient:
    def __init__(self, server: FakeImmichServer, fail_precheck: bool = False):
        self.server = server
        self.fail_precheck = fail_precheck
        self.uploaded: list[str] = []

    def ping(self) -> bool:
        return True

    def auth_ok(self) -> bool:
        return True

    def bulk_upload_check(self, pairs) -> dict[str, UploadResult]:
        from src.immich import ImmichError
        if self.fail_precheck:
            raise ImmichError("bulk-upload-check unavailable")
        out = {}
        for key, sha1_hex in pairs:
            asset_id = self.server.asset_id_for(sha1_hex)
            out[str(key)] = UploadResult(
                asset_id=asset_id, duplicate=bool(asset_id), found=bool(asset_id))
        return out

    def find_by_checksum(self, sha1_hex: str, filename: str | None = None) -> UploadResult:
        asset_id = self.server.asset_id_for(sha1_hex)
        return UploadResult(asset_id=asset_id, found=bool(asset_id))

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        for checksum, aid in self.server.by_checksum.items():
            if aid == asset_id:
                stored = "0" * 40 if aid in self.server.corrupt else checksum
                return {"id": aid,
                        "checksum": base64.b64encode(bytes.fromhex(stored)).decode()}
        return None

    def upload_file(self, path, sha1_hex: str, device_asset_id=None) -> UploadResult:
        existing = self.server.asset_id_for(sha1_hex)
        if existing:
            return UploadResult(asset_id=existing, duplicate=True, found=True)
        asset_id = self.server.add(sha1_hex)
        self.uploaded.append(str(path))
        return UploadResult(asset_id=asset_id, duplicate=False, found=True)


class FakeUploader:
    """Stands in for `docker run immich-cli upload`."""

    def __init__(self, server: FakeImmichServer, fail: bool = False):
        self.server = server
        self.fail = fail
        self.calls: list[tuple[str, bool]] = []

    def upload_dir(self, import_dir: Path, dry_run: bool = False) -> CliRun:
        from src.immich import ImmichError
        self.calls.append((str(import_dir), dry_run))
        if self.fail:
            raise ImmichError("simulated immich-cli failure")
        count = 0
        for path in sorted(Path(import_dir).rglob("*")):
            if path.is_file():
                count += 1
                if not dry_run:
                    self.server.add(sha1_bytes(path.read_bytes()))
        return CliRun(0, f"uploaded {count} files", "", ["fake"])
