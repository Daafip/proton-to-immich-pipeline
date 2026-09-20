import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import proton  # noqa: E402
from src.config import load  # noqa: E402
from tests.helpers import FIXTURES  # noqa: E402


def nodes_from(fixture: str, parent: str = "/Photos"):
    payload = proton.parse_json_output((FIXTURES / fixture).read_text())
    return [proton.normalize_entry(e, parent) for e in proton.extract_entries(payload)]


class TestNormalisation(unittest.TestCase):
    def test_camel_case_shape(self):
        nodes = nodes_from("proton_list_camelcase.json")
        self.assertEqual(len(nodes), 4)
        first = nodes[0]
        self.assertEqual(first.node_id, "n-001")
        self.assertEqual(first.path, "/Photos/IMG_0001.jpg")
        self.assertEqual(first.size, 2048576)
        self.assertFalse(first.is_folder)
        self.assertTrue(nodes[2].is_folder)

    def test_snake_case_shape_with_epoch_and_string_size(self):
        nodes = nodes_from("proton_list_snake.json")
        self.assertEqual(nodes[0].size, 10485760)
        self.assertTrue(nodes[0].modified.startswith("2026-01-01"))
        self.assertEqual(nodes[0].name, "VID_0001.mp4")

    def test_nested_payload(self):
        nodes = nodes_from("proton_list_nested.json")
        self.assertEqual([n.node_id for n in nodes], ["u-1", "u-2"])
        self.assertTrue(nodes[1].is_folder)

    def test_missing_id_falls_back_to_path(self):
        node = proton.normalize_entry({"name": "a.jpg", "size": 1}, "/Photos")
        self.assertEqual(node.node_id, "path:/Photos/a.jpg")

    def test_ndjson_and_log_noise(self):
        payload = proton.parse_json_output(
            'starting up\n{"name":"a.jpg","size":1}\n{"name":"b.jpg","size":2}\n')
        self.assertEqual(len(proton.extract_entries(payload)), 2)

    def test_unparseable_output_raises_with_context(self):
        with self.assertRaises(proton.ProtonError):
            proton.parse_json_output("totally not json")


class TestFiltering(unittest.TestCase):
    def node(self, name, folder=False):
        return proton.RemoteNode("id", f"/Photos/{name}", name, 1, None, folder)

    def test_extension_allowlist(self):
        exts = [".jpg", ".mp4"]
        self.assertTrue(proton.should_include(self.node("a.JPG"), exts, []))
        self.assertFalse(proton.should_include(self.node("notes.txt"), exts, []))

    def test_folders_never_included(self):
        self.assertFalse(proton.should_include(self.node("dir", folder=True), [], []))

    def test_exclude_globs(self):
        self.assertFalse(proton.should_include(self.node(".hidden.jpg"), [], [".*"]))
        node = proton.RemoteNode("id", "/Photos/.trash/x.jpg", "x.jpg", 1, None, False)
        self.assertFalse(proton.should_include(node, [], ["*/.trash/*"]))

    def test_empty_allowlist_accepts_everything(self):
        self.assertTrue(proton.should_include(self.node("weird.xyz"), [], []))


class TestAuthDetection(unittest.TestCase):
    def test_the_message_cli_drive_actually_prints(self):
        # cli-drive@0.6.0: stdout "You need to login first", exit 1, no JSON.
        self.assertTrue(proton.looks_like_auth_failure("You need to login first"))

    def test_auth_hints(self):
        self.assertTrue(proton.looks_like_auth_failure("Error: not logged in"))
        self.assertTrue(proton.looks_like_auth_failure("HTTP 401 Unauthorized"))
        self.assertTrue(proton.looks_like_auth_failure("Secret Service unavailable"))
        self.assertFalse(proton.looks_like_auth_failure("connection reset by peer"))


class TestCliBackend(unittest.TestCase):
    def make(self, outputs):
        cfg = load(None)
        backend = proton.ProtonCliBackend(cfg)
        calls = []

        class Result:
            def __init__(self, stdout):
                self.stdout, self.stderr, self.returncode = stdout, "", 0

        def fake_run(args, timeout=None):
            calls.append(args)
            path = args[args.index("list") + 1] if "list" in args else "/"
            return Result(outputs.get(path, "[]"))

        backend._run = fake_run
        return backend, calls

    def test_walk_recurses_into_folders(self):
        outputs = {
            "/Photos": json.dumps([
                {"nodeId": "d1", "name": "2026-08", "type": "folder"},
                {"nodeId": "f1", "name": "top.jpg", "size": 1, "type": "file"},
            ]),
            "/Photos/2026-08": json.dumps([
                {"nodeId": "f2", "name": "inner.jpg", "size": 2, "type": "file"},
            ]),
        }
        backend, calls = self.make(outputs)
        found = list(backend.walk("/Photos"))
        self.assertEqual(sorted(n.node_id for n in found), ["f1", "f2"])
        self.assertEqual(len(calls), 2)

    def test_walk_respects_max_depth(self):
        outputs = {
            "/Photos": json.dumps([{"nodeId": "d1", "name": "a", "type": "folder"}]),
            "/Photos/a": json.dumps([{"nodeId": "f1", "name": "x.jpg", "size": 1,
                                      "type": "file"}]),
        }
        backend, calls = self.make(outputs)
        backend.max_depth = 0
        self.assertEqual(list(backend.walk("/Photos")), [])

    def test_command_templates_are_substituted(self):
        cfg = load(None)
        backend = proton.ProtonCliBackend(cfg)
        args = backend._template("list", ["filesystem", "list", "{path}", "--json"],
                                 path="/Photos")
        self.assertEqual(args, ["filesystem", "list", "/Photos", "--json"])


class TestCliDownload(unittest.TestCase):
    """`filesystem download path... localFolder` -- the CLI names the file."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load(None)
        self.backend = proton.ProtonCliBackend(self.cfg)
        self.node = proton.RemoteNode("n1", "/my-files/IMG_1.jpg", "IMG_1.jpg",
                                      4, None, False)

    def tearDown(self):
        self.tmp.cleanup()

    def arm(self, writes_name=None, content=b"data"):
        """Simulate the CLI dropping <folder>/<name>."""
        self.calls = []

        class Result:
            stdout = stderr = ""
            returncode = 0

        def fake_run(args, timeout=None):
            self.calls.append(args)
            folder = Path(args[-1])
            folder.mkdir(parents=True, exist_ok=True)
            if writes_name:
                (folder / writes_name).write_bytes(content)
            return Result()

        self.backend._run = fake_run

    def test_file_is_renamed_to_the_requested_path(self):
        self.arm(writes_name="IMG_1.jpg")
        dest = Path(self.tmp.name) / "scratch" / "wanted-name.jpg"
        self.backend.download(self.node, dest)
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"data")

    def test_destination_folder_is_passed_not_the_file(self):
        self.arm(writes_name="IMG_1.jpg")
        dest = Path(self.tmp.name) / "scratch" / "IMG_1.jpg"
        self.backend.download(self.node, dest)
        self.assertEqual(self.calls[0][-1], str(dest.parent))
        self.assertIn("-c", self.calls[0])
        self.assertIn("skip", self.calls[0], "unattended runs must not prompt")

    def test_missing_output_raises_with_folder_contents(self):
        self.arm(writes_name="something-else.jpg")
        dest = Path(self.tmp.name) / "scratch" / "IMG_1.jpg"
        with self.assertRaises(proton.ProtonError) as ctx:
            self.backend.download(self.node, dest)
        self.assertIn("something-else.jpg", str(ctx.exception))

    def test_auth_probe_lists_top_level_sections(self):
        probes = []

        class Result:
            stdout, stderr, returncode = "[]", "", 0

        def fake_run(args, timeout=None):
            probes.append(args)
            return Result()

        self.backend._run = fake_run
        self.assertTrue(self.backend.auth_ok())
        self.assertEqual(probes[0], ["filesystem", "list", "/", "--json"])

    def test_auth_probe_reports_false_on_login_error(self):
        def fake_run(args, timeout=None):
            raise proton.AuthError("You need to login first")

        self.backend._run = fake_run
        self.assertFalse(self.backend.auth_ok())


class TestEnvironment(unittest.TestCase):
    """PROTON_DRIVE_* wiring, verified against cli-drive@0.6.0."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load(None)
        self.cfg.set("staging.root", self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cache_dir_and_credentials_store_are_exported(self):
        self.cfg.set("proton.credentials_store", "unsafe_file")
        env = proton.ProtonCliBackend(self.cfg)._env()
        self.assertEqual(env["PROTON_DRIVE_CACHE_DIR"],
                         str(Path(self.tmp.name) / ".proton"))
        self.assertEqual(env["PROTON_DRIVE_CREDENTIALS_STORE"], "unsafe_file")

    def test_cache_dir_is_private(self):
        """With unsafe_file the session token lives here in plaintext."""
        env = proton.ProtonCliBackend(self.cfg)._env()
        mode = Path(env["PROTON_DRIVE_CACHE_DIR"]).stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_unusable_cache_dir_raises_a_clear_error(self):
        self.cfg.set("proton.cache_dir", "/proc/nonexistent/cache")
        with self.assertRaises(proton.ProtonError) as ctx:
            proton.ProtonCliBackend(self.cfg)._env()
        self.assertIn("PROTON_DRIVE_CACHE_DIR", str(ctx.exception))

    def test_default_store_is_not_forced(self):
        self.cfg.set("proton.credentials_store", None)
        self.assertNotIn("PROTON_DRIVE_CREDENTIALS_STORE",
                         proton.ProtonCliBackend(self.cfg)._env())


class TestPathEscaping(unittest.TestCase):
    def test_slash_in_node_name_is_escaped(self):
        node = proton.normalize_entry({"name": "foo/bar.jpg", "size": 1}, "/my-files")
        self.assertEqual(node.path, "/my-files/foo\\/bar.jpg")


class TestRcloneBackend(unittest.TestCase):
    def test_lsjson_parsing(self):
        cfg = load(None)
        cfg.set("proton.backend", "rclone")
        backend = proton.get_backend(cfg)
        self.assertIsInstance(backend, proton.RcloneBackend)

        class Result:
            stdout = (FIXTURES / "rclone_lsjson.json").read_text()
            stderr, returncode = "", 0

        backend._run = lambda args, timeout=None: Result()
        nodes = list(backend.walk("/Photos"))
        self.assertEqual([n.node_id for n in nodes], ["r-1", "r-2"])
        self.assertEqual(nodes[0].path, "/Photos/2026-08/IMG_0001.jpg")
        self.assertEqual(nodes[0].size, 2048576)

    def test_remote_path_join(self):
        cfg = load(None)
        cfg.set("proton.backend", "rclone")
        backend = proton.get_backend(cfg)
        self.assertEqual(backend._remote_path("/Photos/a.jpg"), "protondrive:Photos/a.jpg")


if __name__ == "__main__":
    unittest.main()
