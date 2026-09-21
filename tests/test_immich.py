import base64
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import immich, log  # noqa: E402
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


class TestContainerNetworking(unittest.TestCase):
    """Immich publishes 2283 on the host; the container must share that
    namespace for a loopback url to mean the same machine in both modes."""

    def uploader(self, url, docker_args=None):
        cfg = load(None)
        cfg.set("immich.url", url)
        if docker_args is not None:
            cfg.set("immich.docker_args", docker_args)
        return immich.ImmichCliUploader(cfg)

    def test_loopback_urls_get_host_networking(self):
        for url in ("http://127.0.0.1:2283/api", "http://localhost:2283/api",
                    "http://[::1]:2283/api", "http://0.0.0.0:2283/api"):
            with self.subTest(url=url):
                argv = self.uploader(url).build_argv(Path("/batch"))
                self.assertIn("--network", argv)
                self.assertEqual(argv[argv.index("--network") + 1], "host")
                self.assertLess(argv.index("--network"), argv.index(self.uploader(url).image),
                                "docker flags must precede the image name")

    def test_a_routable_host_is_left_on_the_default_bridge(self):
        argv = self.uploader("http://192.168.68.52:2283/api").build_argv(Path("/batch"))
        self.assertNotIn("--network", argv)

    def test_an_explicit_network_is_never_overridden(self):
        up = self.uploader("http://127.0.0.1:2283/api",
                           docker_args=["--network", "immich_default"])
        self.assertEqual(up.configured_network(), "immich_default")
        self.assertNotIn("host", up.docker_run_args())

    def test_equals_spelling_is_understood(self):
        up = self.uploader("http://127.0.0.1:2283/api",
                           docker_args=["--network=immich_default"])
        self.assertEqual(up.configured_network(), "immich_default")

    def test_docker_args_are_passed_through(self):
        argv = self.uploader("http://vm:2283/api",
                             docker_args=["--dns", "10.0.0.1"]).build_argv(Path("/batch"))
        self.assertEqual(argv[:5], ["docker", "run", "--rm", "--dns", "10.0.0.1"])

    def test_only_an_unfixable_combination_still_raises(self):
        """Loopback plus a hand-pinned foreign network cannot be saved."""
        up = self.uploader("http://127.0.0.1:2283/api",
                           docker_args=["--network", "immich_default"])
        trap = up.unreachable_from_container()
        self.assertIsNotNone(trap)
        self.assertIn("immich_default", trap)
        with self.assertRaises(immich.ImmichConfigError):
            up.upload_dir(Path("/batch"))

    def test_the_shipped_default_needs_no_edit(self):
        self.assertIsNone(immich.ImmichCliUploader(load(None)).unreachable_from_container())


class TestErrorCondensing(unittest.TestCase):
    """A failing `docker run` buries the cause under pull progress."""

    PULL_NOISE = """Unable to find image 'ghcr.io/immich-app/immich-cli:latest' locally
latest: Pulling from immich-app/immich-cli
9392944252ce: Pulling fs layer
3953cba099bd: Pulling fs layer
1a92ea7b0383: Downloading  12.4MB/58.2MB
a8e022530465: Pull complete
Digest: sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
Status: Downloaded newer image for ghcr.io/immich-app/immich-cli:latest
Error: connect ECONNREFUSED 10.0.0.9:2283"""

    def test_the_real_error_survives_the_pull_progress(self):
        out = log.condense(self.PULL_NOISE)
        self.assertIn("ECONNREFUSED", out)
        self.assertNotIn("Pulling fs layer", out)
        self.assertNotIn("Digest:", out)

    def test_both_ends_are_kept_when_still_too_long(self):
        out = log.condense("HEAD-MARKER " + ("x" * 4000) + " TAIL-MARKER", limit=120)
        self.assertIn("HEAD-MARKER", out)
        self.assertIn("TAIL-MARKER", out)
        self.assertLessEqual(len(out), 120)

    def test_short_output_is_returned_intact(self):
        self.assertEqual(log.condense("Unknown option '-c'."), "Unknown option '-c'.")

    def test_progress_only_output_does_not_vanish(self):
        out = log.condense("latest: Pulling from immich-app/immich-cli")
        self.assertTrue(out.strip(), "an empty error message explains nothing")


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
