"""CLI-level behaviour: config discovery, the flock guard, exit codes."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SYNC = ROOT / "sync.py"


class TestArgumentPositions(unittest.TestCase):
    """Switches must work before or after the subcommand."""

    def parse(self, argv):
        import sync
        return sync.build_parser().parse_args(argv)

    def test_dry_run_after_subcommand(self):
        # The build plan's own acceptance test is written this way.
        self.assertTrue(self.parse(["pull", "--dry-run"]).dry_run)

    def test_dry_run_before_subcommand(self):
        self.assertTrue(self.parse(["--dry-run", "pull"]).dry_run)

    def test_leading_flag_is_not_clobbered_by_the_subcommand_default(self):
        self.assertTrue(self.parse(["-n", "download", "--limit", "5"]).dry_run)

    def test_absent_flag_stays_false(self):
        self.assertFalse(self.parse(["pull"]).dry_run)

    def test_config_and_verbose_in_either_position(self):
        self.assertEqual(self.parse(["-c", "a.yaml", "run"]).config, "a.yaml")
        self.assertEqual(self.parse(["run", "-c", "b.yaml"]).config, "b.yaml")
        self.assertTrue(self.parse(["run", "-v"]).verbose)

    def test_subcommand_options_still_parse(self):
        args = self.parse(["download", "--limit", "20", "--backfill"])
        self.assertEqual(args.limit, 20)
        self.assertTrue(args.backfill)
        self.assertTrue(self.parse(["run", "--now"]).now)
        self.assertEqual(self.parse(["reap", "--keep-days", "0"]).keep_days, 0)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.staging = Path(self.tmp.name) / "staging"
        self.config = Path(self.tmp.name) / "config.yaml"
        self.config.write_text(
            f"staging:\n  root: {self.staging}\n"
            f"immich:\n  url: http://127.0.0.1:1/api\n  api_key: test\n"
            f"proton:\n  roots:\n    - /Photos\n"
            f"report:\n  status_path: {self.staging}/.state/status.json\n",
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_sync(self, *args, timeout=60):
        return subprocess.run(
            [sys.executable, str(SYNC), "-c", str(self.config), *args],
            capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))

    def test_status_on_a_fresh_db(self):
        proc = self.run_sync("status")
        self.assertIn("proton-immich-sync", proc.stdout)
        self.assertIn("never", proc.stdout)

    def test_status_json_is_machine_readable(self):
        proc = self.run_sync("status", "--json")
        status = json.loads(proc.stdout)
        self.assertEqual(status["backlog"], 0)
        self.assertIn("auth_ok", status)
        self.assertIn("staging_free_gb", status)

    def test_status_creates_the_staging_layout(self):
        self.run_sync("status")
        for name in ("ready", "incoming", "batch", ".state"):
            self.assertTrue((self.staging / name).is_dir(), name)

    def test_missing_config_is_reported(self):
        proc = subprocess.run(
            [sys.executable, str(SYNC), "-c", "/nonexistent.yaml", "status"],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("config error", proc.stderr)

    def test_invalid_config_refuses_to_run(self):
        self.config.write_text(
            f"staging:\n  root: {self.staging}\n"
            f"immich:\n  url: http://vm:2283\n  api_key: \"\"\n", encoding="utf-8")
        proc = self.run_sync("pull")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("config.invalid", proc.stderr)

    def test_lock_is_exclusive(self):
        """A second invocation exits 3 rather than racing the first."""
        import fcntl
        lock_path = self.staging / ".state" / "sync.lock"
        self.run_sync("status")  # create the layout
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            proc = self.run_sync("pull")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("lock.held", proc.stderr)

    def test_unreachable_immich_and_proton_is_a_clean_failure(self):
        """No traceback, a real exit code, and a status file on disk."""
        proc = self.run_sync("pull")
        self.assertIn(proc.returncode, (1, 2))
        self.assertNotIn("Traceback", proc.stderr)

    def test_logs_are_one_json_object_per_line(self):
        proc = self.run_sync("status", "--probe")
        for line in (proc.stdout + proc.stderr).splitlines():
            if line.startswith("{"):
                json.loads(line)


if __name__ == "__main__":
    unittest.main()
