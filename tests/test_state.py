import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import state  # noqa: E402


class StateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.conn = state.connect(Path(self.dir.name) / "state.sqlite")
        state.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def add(self, node_id="n1", size=100, modified="2026-01-01T00:00:00+00:00"):
        return state.upsert_discovered(
            self.conn, node_id, f"/Photos/{node_id}.jpg", f"{node_id}.jpg",
            size, modified)


class TestUpsert(StateTest):
    def test_new_then_unchanged(self):
        self.assertEqual(self.add(), "new")
        self.assertEqual(self.add(), "unchanged")

    def test_size_change_resets_the_row(self):
        self.add()
        state.mark_downloaded(self.conn, "n1", "/x/n1.jpg", "abc")
        self.assertEqual(self.add(size=200), "changed")
        row = state.get(self.conn, "n1")
        self.assertEqual(row["status"], state.DISCOVERED)
        self.assertIsNone(row["sha1"])
        self.assertIsNone(row["local_path"])

    def test_rename_is_metadata_only(self):
        self.add()
        state.mark_downloaded(self.conn, "n1", "/x/n1.jpg", "abc")
        result = state.upsert_discovered(
            self.conn, "n1", "/Photos/renamed.jpg", "renamed.jpg", 100,
            "2026-01-01T00:00:00+00:00")
        self.assertEqual(result, "unchanged")
        row = state.get(self.conn, "n1")
        self.assertEqual(row["status"], state.DOWNLOADED)
        self.assertEqual(row["remote_name"], "renamed.jpg")


class TestResume(StateTest):
    def test_in_flight_states_reset(self):
        self.add("a")
        self.add("b")
        state.mark_downloading(self.conn, "a")
        state.mark_downloaded(self.conn, "b", "/x/b.jpg", "s")
        state.mark_uploading(self.conn, "b")
        reset = state.resume(self.conn)
        self.assertEqual(reset, {state.DOWNLOADING: 1, state.UPLOADING: 1})
        self.assertEqual(state.get(self.conn, "a")["status"], state.DISCOVERED)
        self.assertEqual(state.get(self.conn, "b")["status"], state.DOWNLOADED)

    def test_resume_clears_partial_local_path(self):
        self.add("a")
        state.mark_downloaded(self.conn, "a", "/x/a.jpg", "s")
        state.mark_downloading(self.conn, "a")
        state.resume(self.conn)
        self.assertIsNone(state.get(self.conn, "a")["local_path"])


class TestFailureHandling(StateTest):
    def test_quarantine_after_max_attempts(self):
        self.add()
        results = [state.mark_failed(self.conn, "n1", "download", "boom", 5)
                   for _ in range(5)]
        self.assertEqual(results, [state.FAILED] * 4 + [state.QUARANTINED])
        self.assertEqual(state.get(self.conn, "n1")["attempts"], 5)
        self.assertTrue(state.get(self.conn, "n1")["last_error"].startswith("download:"))

    def test_backoff_window(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(seconds=60)).isoformat()
        self.assertFalse(state.backoff_ready(1, recent, 300, 86400, now))
        self.assertTrue(state.backoff_ready(1, (now - timedelta(seconds=400)).isoformat(),
                                            300, 86400, now))
        # attempt 3 waits 300 * 2^2 = 1200s
        self.assertFalse(state.backoff_ready(3, (now - timedelta(seconds=1000)).isoformat(),
                                             300, 86400, now))
        self.assertTrue(state.backoff_ready(3, (now - timedelta(seconds=1300)).isoformat(),
                                            300, 86400, now))

    def test_backoff_is_capped(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        stale = (now - timedelta(days=2)).isoformat()
        self.assertTrue(state.backoff_ready(20, stale, 300, 86400, now))

    def test_failed_download_retries_at_download_stage(self):
        self.add("a")
        state.mark_failed(self.conn, "a", "download", "boom", 5)
        picked = state.select_for_download(self.conn, backoff_base_sec=0)
        self.assertEqual([r["node_id"] for r in picked], ["a"])
        self.assertEqual(state.select_for_upload(self.conn, backoff_base_sec=0), [])

    def test_failed_upload_retries_at_upload_stage(self):
        self.add("b")
        state.mark_downloaded(self.conn, "b", "/x/b.jpg", "sha")
        state.mark_failed(self.conn, "b", "upload", "boom", 5)
        self.assertEqual(state.select_for_download(self.conn, backoff_base_sec=0), [])
        picked = state.select_for_upload(self.conn, backoff_base_sec=0)
        self.assertEqual([r["node_id"] for r in picked], ["b"])

    def test_quarantined_rows_are_not_selected(self):
        self.add("c")
        for _ in range(5):
            state.mark_failed(self.conn, "c", "download", "boom", 5)
        self.assertEqual(state.select_for_download(self.conn, backoff_base_sec=0), [])
        self.assertEqual(state.requeue(self.conn), 1)
        self.assertEqual(len(state.select_for_download(self.conn, backoff_base_sec=0)), 1)


class TestSelection(StateTest):
    def test_download_respects_file_and_byte_caps(self):
        for i in range(5):
            self.add(f"n{i}", size=1000)
        self.assertEqual(len(state.select_for_download(self.conn, limit=2)), 2)
        # 2 x 1000 fits under 2500; a third would exceed it.
        picked = state.select_for_download(self.conn, max_bytes=2500)
        self.assertEqual(len(picked), 2)

    def test_byte_cap_always_allows_one_oversized_file(self):
        self.add("big", size=10_000)
        self.assertEqual(len(state.select_for_download(self.conn, max_bytes=100)), 1)

    def test_reap_honours_grace_period(self):
        self.add("n1")
        state.mark_downloaded(self.conn, "n1", "/x/n1.jpg", "s")
        state.mark_uploaded(self.conn, "n1", "asset-1")
        state.mark_verified(self.conn, "n1")
        self.assertEqual(state.select_for_reap(self.conn, keep_days=7), [])
        self.assertEqual(len(state.select_for_reap(self.conn, keep_days=0)), 1)
        future = datetime.now(timezone.utc) + timedelta(days=8)
        self.assertEqual(len(state.select_for_reap(self.conn, keep_days=7, now=future)), 1)


class TestCounts(StateTest):
    def test_backlog_excludes_finished_states(self):
        self.add("a")
        self.add("b")
        state.mark_downloaded(self.conn, "b", "/x/b.jpg", "s")
        state.mark_uploaded(self.conn, "b", "id")
        state.mark_verified(self.conn, "b")
        self.assertEqual(state.backlog(self.conn), 1)
        self.assertEqual(state.counts(self.conn)["total"], 2)


if __name__ == "__main__":
    unittest.main()
