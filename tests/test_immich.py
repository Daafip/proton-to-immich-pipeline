import base64
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import immich  # noqa: E402
from src.config import load  # noqa: E402


def cfg_with(**overrides):
    cfg = load(None)
    cfg.set("immich.api_key", "test-key")
    cfg.set("immich.url", "http://vm:2283/api")
    for key, value in overrides.items():
        cfg.set(key, value)
    return cfg


class TestChecksums(unittest.TestCase):
    def setUp(self):
        self.hex = hashlib.sha1(b"payload").hexdigest()
        self.b64 = base64.b64encode(bytes.fromhex(self.hex)).decode()

    def test_hex_and_base64_are_equivalent(self):
        self.assertTrue(immich.checksums_match(self.hex, self.b64))
        self.assertTrue(immich.checksums_match(self.hex, self.hex))

    def test_mismatch_detected(self):
        other = hashlib.sha1(b"different").hexdigest()
        self.assertFalse(immich.checksums_match(self.hex, other))

    def test_missing_values_never_match(self):
        self.assertFalse(immich.checksums_match(self.hex, None))
        self.assertFalse(immich.checksums_match(None, self.b64))
        self.assertFalse(immich.checksums_match(self.hex, "garbage"))

    def test_sha1_file(self):
        import tempfile
        path = Path(tempfile.mkdtemp()) / "f.bin"
        path.write_bytes(b"payload")
        self.assertEqual(immich.sha1_file(path), self.hex)


class TestCliArgv(unittest.TestCase):
    def test_api_suffix_is_enforced(self):
        uploader = immich.ImmichCliUploader(cfg_with(**{"immich.url": "http://vm:2283"}))
        argv = uploader.build_argv(Path("/mnt/batch"))
        self.assertIn("IMMICH_INSTANCE_URL=http://vm:2283/api", argv)

    def test_flat_album_strategy(self):
        argv = immich.ImmichCliUploader(cfg_with()).build_argv(Path("/b"))
        self.assertIn("--album-name", argv)
        self.assertIn("Proton Import", argv)
        self.assertIn("--recursive", argv)

    def test_folder_album_strategy(self):
        uploader = immich.ImmichCliUploader(cfg_with(**{"immich.album_strategy": "folder"}))
        argv = uploader.build_argv(Path("/b"))
        self.assertIn("--album", argv)
        self.assertNotIn("--album-name", argv)

    def test_no_album(self):
        uploader = immich.ImmichCliUploader(cfg_with(**{"immich.album_strategy": "none"}))
        argv = uploader.build_argv(Path("/b"))
        self.assertNotIn("--album", argv)
        self.assertNotIn("--album-name", argv)

    def test_delete_flag_is_stripped(self):
        uploader = immich.ImmichCliUploader(
            cfg_with(**{"immich.extra_args": ["--delete", "--ignore", "*.txt"]}))
        argv = uploader.build_argv(Path("/b"))
        self.assertNotIn("--delete", argv)
        self.assertIn("--ignore", argv)

    def test_read_only_mount_and_dry_run(self):
        argv = immich.ImmichCliUploader(cfg_with()).build_argv(Path("/b"), dry_run=True)
        self.assertIn("/b:/import:ro", argv)
        self.assertIn("--dry-run", argv)


class TestBulkUploadCheck(unittest.TestCase):
    def build(self, responder):
        client = immich.ImmichClient(cfg_with())
        client._request = responder
        return client

    def test_hex_format_used_first(self):
        seen = {}

        def responder(method, path, body=None, **kw):
            seen["checksum"] = body["assets"][0]["checksum"]
            return {"results": [{"id": "a", "action": "reject",
                                 "reason": "duplicate", "assetId": "asset-1"}]}

        client = self.build(responder)
        digest = hashlib.sha1(b"x").hexdigest()
        result = client.bulk_upload_check([("a", digest)])
        self.assertEqual(seen["checksum"], digest)
        self.assertTrue(result["a"].duplicate)
        self.assertEqual(result["a"].asset_id, "asset-1")

    def test_falls_back_to_base64_when_hex_rejected(self):
        attempts = []

        def responder(method, path, body=None, **kw):
            checksum = body["assets"][0]["checksum"]
            attempts.append(checksum)
            if len(checksum) == 40:
                raise immich.ImmichError("POST -> 400: checksum must be base64")
            return {"results": [{"id": "a", "action": "accept"}]}

        client = self.build(responder)
        digest = hashlib.sha1(b"x").hexdigest()
        result = client.bulk_upload_check([("a", digest)])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1], base64.b64encode(bytes.fromhex(digest)).decode())
        self.assertFalse(result["a"].found)
        self.assertEqual(client._checksum_format, "base64")

    def test_format_is_remembered(self):
        calls = []

        def responder(method, path, body=None, **kw):
            calls.append(body["assets"][0]["checksum"])
            return {"results": [{"id": "a", "action": "accept"}]}

        client = self.build(responder)
        digest = hashlib.sha1(b"x").hexdigest()
        client.bulk_upload_check([("a", digest)])
        client.bulk_upload_check([("a", digest)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(client._checksum_format, "hex")

    def test_auth_error_propagates(self):
        def responder(*a, **kw):
            raise immich.ImmichAuthError("401")

        with self.assertRaises(immich.ImmichAuthError):
            self.build(responder).bulk_upload_check([("a", "0" * 40)])

    def test_empty_input_short_circuits(self):
        client = self.build(lambda *a, **kw: self.fail("should not request"))
        self.assertEqual(client.bulk_upload_check([]), {})

    def test_find_by_checksum_falls_back_to_filename_search(self):
        digest = hashlib.sha1(b"x").hexdigest()

        def responder(method, path, body=None, **kw):
            if "bulk-upload-check" in path:
                raise immich.ImmichError("unavailable")
            return {"assets": {"items": [
                {"id": "asset-9",
                 "checksum": base64.b64encode(bytes.fromhex(digest)).decode()}]}}

        result = self.build(responder).find_by_checksum(digest, "IMG.jpg")
        self.assertEqual(result.asset_id, "asset-9")


if __name__ == "__main__":
    unittest.main()
