import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import state  # noqa: E402

ACCOUNT = "david"
OTHER = "mirjam"


class StateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.conn = state.connect(Path(self.dir.name) / "state.sqlite")
        state.init_schema(self.conn, ACCOUNT)

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def add(self, node_id="n1", size=100, modified="2026-01-01T00:00:00+00:00"):
        return state.upsert_discovered(
            self.conn, ACCOUNT, node_id, f"/Photos/{node_id}.jpg",
            f"{node_id}.jpg", size, modified)


class TestUpsert(StateTest):
    def test_new_then_unchanged(self):
        self.assertEqual(self.add(), "new")
        self.assertEqual(self.add(), "unchanged")

    def test_size_change_resets_the_row(self):
        self.add()
        state.mark_downloaded(self.conn, ACCOUNT, "n1", "/x/n1.jpg", "abc")
        self.assertEqual(self.add(size=200), "changed")
        row = state.get(self.conn, ACCOUNT, "n1")
        self.assertEqual(row["status"], state.DISCOVERED)
        self.assertIsNone(row["sha1"])
        self.assertIsNone(row["local_path"])

    def test_rename_is_metadata_only(self):
        self.add()
        state.mark_downloaded(self.conn, ACCOUNT, "n1", "/x/n1.jpg", "abc")
        result = state.upsert_discovered(
            self.conn, ACCOUNT, "n1", "/Photos/renamed.jpg",
            "renamed.jpg", 100,
            "2026-01-01T00:00:00+00:00")
        self.assertEqual(result, "unchanged")
        row = state.get(self.conn, ACCOUNT, "n1")
        self.assertEqual(row["status"], state.DOWNLOADED)
        self.assertEqual(row["remote_name"], "renamed.jpg")


class TestResume(StateTest):
    def test_in_flight_states_reset(self):
        self.add("a")
        self.add("b")
        state.mark_downloading(self.conn, ACCOUNT, "a")
        state.mark_downloaded(self.conn, ACCOUNT, "b", "/x/b.jpg", "s")
        state.mark_uploading(self.conn, ACCOUNT, "b")
        reset = state.resume(self.conn, ACCOUNT)
        self.assertEqual(reset, {state.DOWNLOADING: 1, state.UPLOADING: 1})
        self.assertEqual(state.get(self.conn, ACCOUNT, "a")["status"], state.DISCOVERED)
        self.assertEqual(state.get(self.conn, ACCOUNT, "b")["status"], state.DOWNLOADED)

    def test_resume_clears_partial_local_path(self):
        self.add("a")
        state.mark_downloaded(self.conn, ACCOUNT, "a", "/x/a.jpg", "s")
        state.mark_downloading(self.conn, ACCOUNT, "a")
        state.resume(self.conn, ACCOUNT)
        self.assertIsNone(state.get(self.conn, ACCOUNT, "a")["local_path"])


class TestFailureHandling(StateTest):
    def test_quarantine_after_max_attempts(self):
        self.add()
        results = [state.mark_failed(self.conn, ACCOUNT, "n1", "download", "boom", 5)
                   for _ in range(5)]
        self.assertEqual(results, [state.FAILED] * 4 + [state.QUARANTINED])
        self.assertEqual(state.get(self.conn, ACCOUNT, "n1")["attempts"], 5)
        self.assertTrue(state.get(self.conn, ACCOUNT, "n1")["last_error"].startswith("download:"))

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
        state.mark_failed(self.conn, ACCOUNT, "a", "download", "boom", 5)
        picked = state.select_for_download(self.conn, ACCOUNT, backoff_base_sec=0)
        self.assertEqual([r["node_id"] for r in picked], ["a"])
        self.assertEqual(state.select_for_upload(self.conn, ACCOUNT, backoff_base_sec=0), [])

    def test_failed_upload_retries_at_upload_stage(self):
        self.add("b")
        state.mark_downloaded(self.conn, ACCOUNT, "b", "/x/b.jpg", "sha")
        state.mark_failed(self.conn, ACCOUNT, "b", "upload", "boom", 5)
        self.assertEqual(state.select_for_download(self.conn, ACCOUNT, backoff_base_sec=0), [])
        picked = state.select_for_upload(self.conn, ACCOUNT, backoff_base_sec=0)
        self.assertEqual([r["node_id"] for r in picked], ["b"])

    def test_quarantined_rows_are_not_selected(self):
        self.add("c")
        for _ in range(5):
            state.mark_failed(self.conn, ACCOUNT, "c", "download", "boom", 5)
        self.assertEqual(state.select_for_download(self.conn, ACCOUNT, backoff_base_sec=0), [])
        self.assertEqual(state.requeue(self.conn, ACCOUNT), 1)
        self.assertEqual(len(state.select_for_download(self.conn, ACCOUNT, backoff_base_sec=0)), 1)


class TestSelection(StateTest):
    def test_download_respects_file_and_byte_caps(self):
        for i in range(5):
            self.add(f"n{i}", size=1000)
        self.assertEqual(len(state.select_for_download(self.conn, ACCOUNT, limit=2)), 2)
        # 2 x 1000 fits under 2500; a third would exceed it.
        picked = state.select_for_download(self.conn, ACCOUNT, max_bytes=2500)
        self.assertEqual(len(picked), 2)

    def test_byte_cap_always_allows_one_oversized_file(self):
        self.add("big", size=10_000)
        self.assertEqual(len(state.select_for_download(self.conn, ACCOUNT, max_bytes=100)), 1)

    def test_reap_honours_grace_period(self):
        self.add("n1")
        state.mark_downloaded(self.conn, ACCOUNT, "n1", "/x/n1.jpg", "s")
        state.mark_uploaded(self.conn, ACCOUNT, "n1", "asset-1")
        state.mark_verified(self.conn, ACCOUNT, "n1")
        self.assertEqual(state.select_for_reap(self.conn, ACCOUNT, keep_days=7), [])
        self.assertEqual(len(state.select_for_reap(self.conn, ACCOUNT, keep_days=0)), 1)
        future = datetime.now(timezone.utc) + timedelta(days=8)
        self.assertEqual(len(state.select_for_reap(self.conn, ACCOUNT, keep_days=7, now=future)), 1)


class TestMigration(StateTest):
    """A v1/v2 database has to survive the move to (account, node_id)."""

    V2_ASSETS = (
        "CREATE TABLE assets (node_id TEXT PRIMARY KEY, remote_path TEXT NOT NULL,"
        " remote_name TEXT NOT NULL, remote_size INTEGER, remote_modified TEXT,"
        " local_path TEXT, sha1 TEXT, status TEXT NOT NULL, immich_asset_id TEXT,"
        " is_duplicate INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,"
        " first_seen TEXT NOT NULL, last_attempt TEXT, last_error TEXT,"
        " claimed_sha1 TEXT, capture_time TEXT)"
    )
    V1_ASSETS = (
        "CREATE TABLE assets (node_id TEXT PRIMARY KEY, remote_path TEXT NOT NULL,"
        " remote_name TEXT NOT NULL, remote_size INTEGER, remote_modified TEXT,"
        " local_path TEXT, sha1 TEXT, status TEXT NOT NULL, immich_asset_id TEXT,"
        " is_duplicate INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,"
        " first_seen TEXT NOT NULL, last_attempt TEXT, last_error TEXT)"
    )

    def make_old_db(self, ddl: str, rows: int = 3) -> None:
        """Replace the v3 tables with the pre-v3 shapes and seed them."""
        self.conn.executescript(
            "DROP INDEX IF EXISTS idx_assets_status;"
            "DROP INDEX IF EXISTS idx_assets_sha1;"
            "DROP INDEX IF EXISTS idx_assets_immich;"
            "DROP INDEX IF EXISTS idx_runs_started;"
            "DROP TABLE assets; DROP TABLE runs;")
        self.conn.execute(ddl)
        self.conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT,"
            " finished_at TEXT, discovered INTEGER, downloaded INTEGER,"
            " uploaded INTEGER, failed INTEGER, exit_code INTEGER)")
        for i in range(rows):
            self.conn.execute(
                "INSERT INTO assets (node_id, remote_path, remote_name,"
                " remote_size, status, first_seen) VALUES (?,?,?,?,?,?)",
                (f"n{i}", f"/Photos/n{i}.jpg", f"n{i}.jpg", 100 + i,
                 state.VERIFIED, "2026-01-01T00:00:00+00:00"))
        self.conn.execute(
            "INSERT INTO runs (run_id, started_at, exit_code) VALUES (?,?,0)",
            ("20260101T000000", "2026-01-01T00:00:00+00:00"))
        self.conn.commit()

    def test_v2_database_is_rebuilt_with_every_row_preserved(self):
        self.make_old_db(self.V2_ASSETS)
        notes = state.init_schema(self.conn, ACCOUNT)
        self.assertIn("assets", notes)
        self.assertIn("runs", notes)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(assets)")}
        self.assertIn("account", cols)
        self.assertIn("immich_checksum", cols)
        rows = self.conn.execute("SELECT * FROM assets ORDER BY node_id").fetchall()
        self.assertEqual([r["node_id"] for r in rows], ["n0", "n1", "n2"])
        self.assertEqual({r["account"] for r in rows}, {ACCOUNT})
        self.assertEqual(rows[1]["remote_size"], 101)
        self.assertEqual(rows[0]["status"], state.VERIFIED)
        run = state.last_run(self.conn, ACCOUNT)
        self.assertEqual(run["run_id"], "20260101T000000")

    def test_v1_database_skips_straight_to_v3(self):
        """No claimed_sha1/capture_time columns at all: they arrive NULL."""
        self.make_old_db(self.V1_ASSETS)
        state.init_schema(self.conn, ACCOUNT)
        row = state.get(self.conn, ACCOUNT, "n0")
        self.assertIsNone(row["claimed_sha1"])
        self.assertIsNone(row["capture_time"])
        self.assertEqual(state.counts(self.conn, ACCOUNT)["total"], 3)

    def test_the_old_table_is_kept_as_the_rollback(self):
        self.make_old_db(self.V2_ASSETS)
        state.init_schema(self.conn, ACCOUNT)
        kept = self.conn.execute("SELECT COUNT(*) c FROM assets_v2").fetchone()["c"]
        self.assertEqual(kept, 3)

    def test_a_backup_file_is_written_first(self):
        self.make_old_db(self.V2_ASSETS)
        notes = state.init_schema(self.conn, ACCOUNT)
        backup = [n for n in notes if n.startswith("backup=")]
        self.assertTrue(backup, notes)
        path = Path(self.dir.name) / backup[0].split("=", 1)[1]
        self.assertTrue(path.exists())
        # The backup is a readable database holding the pre-migration rows.
        import sqlite3
        with sqlite3.connect(str(path)) as old:
            self.assertEqual(
                old.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 3)

    def test_migration_refuses_without_an_account_to_assign(self):
        self.make_old_db(self.V2_ASSETS)
        with self.assertRaises(state.MigrationError):
            state.migrate(self.conn, None)

    def test_migration_is_idempotent(self):
        self.assertEqual(state.migrate(self.conn, ACCOUNT), [])
        self.make_old_db(self.V2_ASSETS)
        state.init_schema(self.conn, ACCOUNT)
        self.assertEqual(state.init_schema(self.conn, ACCOUNT), [])

    def test_revision_fields_round_trip(self):
        state.upsert_discovered(
            self.conn, ACCOUNT, "n1", "/p/a.jpg", "a.jpg", 100,
            "2026-02-15T16:00:00Z",
            claimed_sha1="a" * 40, capture_time="2017-12-27T18:55:15.000Z")
        row = state.get(self.conn, ACCOUNT, "n1")
        self.assertEqual(row["claimed_sha1"], "a" * 40)
        self.assertEqual(row["capture_time"], "2017-12-27T18:55:15.000Z")


class TestAccountIsolation(StateTest):
    """(account, node_id) is the primary key, so the same node id in two
    volumes is two rows -- and neither account can see the other's."""

    def seed_both(self):
        for account in (ACCOUNT, OTHER):
            state.upsert_discovered(
                self.conn, account, "shared-id", f"/{account}/a.jpg", "a.jpg",
                100, "2026-01-01T00:00:00+00:00")

    def test_the_same_node_id_in_two_accounts_is_two_rows(self):
        self.seed_both()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"], 2)
        self.assertEqual(state.get(self.conn, ACCOUNT, "shared-id")["remote_path"],
                         f"/{ACCOUNT}/a.jpg")
        self.assertEqual(state.get(self.conn, OTHER, "shared-id")["remote_path"],
                         f"/{OTHER}/a.jpg")

    def test_selection_never_crosses_accounts(self):
        self.seed_both()
        for picker in (state.select_for_download,):
            self.assertEqual(len(picker(self.conn, ACCOUNT)), 1)
        state.mark_downloaded(self.conn, ACCOUNT, "shared-id", "/x/a.jpg", "s")
        self.assertEqual(len(state.select_for_upload(self.conn, ACCOUNT)), 1)
        self.assertEqual(len(state.select_for_upload(self.conn, OTHER)), 0)

    def test_counts_and_requeue_are_scoped(self):
        self.seed_both()
        state.mark_failed(self.conn, ACCOUNT, "shared-id", "download", "boom", 9)
        self.assertEqual(state.counts(self.conn, ACCOUNT)[state.FAILED], 1)
        self.assertEqual(state.counts(self.conn, OTHER)[state.FAILED], 0)
        self.assertEqual(state.requeue(self.conn, OTHER), 0)
        self.assertEqual(state.requeue(self.conn, ACCOUNT), 1)

    def test_accounts_lists_what_the_db_has_seen(self):
        self.seed_both()
        self.assertEqual(state.accounts(self.conn), sorted([ACCOUNT, OTHER]))


class TestCounts(StateTest):
    def test_backlog_excludes_finished_states(self):
        self.add("a")
        self.add("b")
        state.mark_downloaded(self.conn, ACCOUNT, "b", "/x/b.jpg", "s")
        state.mark_uploaded(self.conn, ACCOUNT, "b", "id")
        state.mark_verified(self.conn, ACCOUNT, "b")
        self.assertEqual(state.backlog(self.conn, ACCOUNT), 1)
        self.assertEqual(state.counts(self.conn, ACCOUNT)["total"], 2)


if __name__ == "__main__":
    unittest.main()
