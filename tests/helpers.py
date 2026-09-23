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
        # One entry per download_many call, so tests can assert on batching.
        self.batch_calls: list[list[str]] = []
        # -- the delete path
        self.trashed: dict[str, bytes] = {}
        self.trash_calls: list[str] = []
        self.trash_fail_paths: set[str] = set()
        self.trash_auth_fail_paths: set[str] = set()
        # Paths where `trash` reports success but leaves the node in place --
        # the case the post-trash verification exists to catch.
        self.trash_noop_paths: set[str] = set()
        self.resolve_fail_paths: set[str] = set()

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

    def download_many(self, nodes, dest_dir: Path) -> None:
        """One call, every file into one folder -- as `filesystem download
        path... localFolder` behaves.

        Crucially it fails the way the real thing does: it raises on the first
        bad file and leaves everything written before it on disk. That partial
        state is what the pipeline has to cope with, so the fake has to
        produce it.
        """
        self.batch_calls.append([node.path for node in nodes])
        dest_dir.mkdir(parents=True, exist_ok=True)
        for node in nodes:
            if node.path in self.auth_fail_paths:
                raise AuthError("session expired")
            if node.path in self.fail_paths:
                raise ProtonError("simulated transfer failure")
            if node.path not in self.files:
                raise ProtonError("no such file")
            self.downloads.append(node.path)
            (dest_dir / node.name).write_bytes(self.files[node.path])

    # -- the delete path ---------------------------------------------------
    def node_at(self, path: str) -> RemoteNode | None:
        if path not in self.files:
            return None
        meta = self.meta.get(path, {})
        return RemoteNode(
            node_id=meta.get("node_id", f"path:{path}"),
            path=path,
            name=os.path.basename(path),
            size=len(self.files[path]),
            modified=meta.get("modified"),
            is_folder=False,
        )

    def resolve(self, path: str) -> RemoteNode | None:
        if path in self.resolve_fail_paths:
            raise ProtonError("simulated info failure")
        return self.node_at(path)

    def trash(self, path: str) -> None:
        """Move to the fake trash: gone from `files`, still recoverable here."""
        if path in self.trash_auth_fail_paths:
            raise AuthError("session expired")
        if path in self.trash_fail_paths:
            raise ProtonError("simulated trash failure")
        self.trash_calls.append(path)
        if path in self.trash_noop_paths:
            return
        if path in self.files:
            self.trashed[path] = self.files.pop(path)


class FakeImmichServer:
    """Holds assets keyed by sha1, the way the real server dedupes."""

    def __init__(self):
        self.by_checksum: dict[str, str] = {}
        self.corrupt: set[str] = set()
        # asset_id -> the metadata /search/metadata would return for it.
        self.trash: dict[str, dict[str, Any]] = {}

    def add(self, sha1_hex: str, asset_id: str | None = None) -> str:
        asset_id = asset_id or str(uuid.uuid4())
        self.by_checksum[sha1_hex] = asset_id
        return asset_id

    def asset_id_for(self, sha1_hex: str) -> str | None:
        return self.by_checksum.get(sha1_hex)

    def trash_asset(self, asset_id: str, filename: str = "x.jpg",
                    asset_type: str = "IMAGE") -> str:
        """What "someone deleted this photo in Immich" looks like."""
        self.trash[str(asset_id)] = {
            "id": str(asset_id), "originalFileName": filename,
            "type": asset_type, "isTrashed": True,
        }
        return str(asset_id)

    def empty_trash(self) -> None:
        """Immich's 30-day auto-purge: the assets are deleted outright.

        Not just dropped from the trash listing -- the server no longer has
        them at all, which is what makes a purge distinguishable from someone
        restoring a photo.
        """
        purged = set(self.trash)
        self.by_checksum = {checksum: asset_id
                            for checksum, asset_id in self.by_checksum.items()
                            if asset_id not in purged}
        self.trash.clear()

    def restore_asset(self, asset_id: str) -> None:
        """What "someone changed their mind in Immich" looks like."""
        self.trash.pop(str(asset_id), None)


class FakeImmichClient:
    def __init__(self, server: FakeImmichServer, fail_precheck: bool = False,
                 fail_trash_search: bool = False, fail_restore: bool = False,
                 fail_asset_lookup: bool = False):
        self.server = server
        self.fail_precheck = fail_precheck
        self.fail_trash_search = fail_trash_search
        self.fail_restore = fail_restore
        self.fail_asset_lookup = fail_asset_lookup
        self.asset_lookups: list[str] = []
        self.uploaded: list[str] = []
        self.trash_queries: list[tuple] = []
        self.restored: list[str] = []
        # Mirrors the real client: False means the returned list is a prefix
        # of the trash, so absence from it proves nothing.
        self.trash_scan_complete = True

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

    def search_trashed(self, types=("IMAGE", "VIDEO"), page_size: int = 250,
                       max_pages: int = 400) -> list[dict[str, Any]]:
        from src.immich import ImmichError
        self.trash_queries.append(tuple(types))
        if self.fail_trash_search:
            raise ImmichError("search/metadata unavailable")
        wanted = {str(t).upper() for t in types}
        return [dict(item) for item in self.server.trash.values()
                if str(item.get("type", "IMAGE")).upper() in wanted]

    def trashed_asset_ids(self, types=("IMAGE", "VIDEO"), page_size: int = 250,
                          max_pages: int = 400) -> set:
        from src.immich import ImmichError
        self.trash_queries.append(tuple(types))
        if self.fail_trash_search:
            raise ImmichError("search/metadata unavailable")
        return set(self.server.trash)

    def asset_state(self, asset_id: str) -> str:
        from src.immich import ImmichError
        asset_id = str(asset_id)
        self.asset_lookups.append(asset_id)
        if self.fail_asset_lookup:
            raise ImmichError("asset lookup unavailable")
        if asset_id in self.server.trash:
            return "trashed"
        if asset_id in set(self.server.by_checksum.values()):
            return "live"
        return "missing"

    def restore_from_trash(self, asset_ids, chunk: int = 200) -> int:
        """Restoring takes the asset back out of the trash, which is exactly
        what makes the photo present in the library again."""
        from src.immich import ImmichError
        if self.fail_restore:
            raise ImmichError("restore unavailable on this Immich version")
        ids = [str(a) for a in asset_ids]
        for asset_id in ids:
            self.server.trash.pop(str(asset_id), None)
        self.restored += ids
        return len(ids)

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
