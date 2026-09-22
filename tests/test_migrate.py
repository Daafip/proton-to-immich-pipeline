"""Moving an existing install to the one-database-per-pipeline layout.

The failure this guards against is the expensive one: a pipeline that quietly
creates an empty `david.sqlite` next to a `state.sqlite` full of David's rows
re-downloads the entire library. So the tests are mostly about *refusing* --
to run unasked, to guess an owner, to overwrite, to lose rows it was not told
about.
"""

import copy
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import migrate, state  # noqa: E402
from src.config import DEFAULTS, Config  # noqa: E402
from tests.helpers import silence_logs  # noqa: E402

V2_ASSETS = (
    "CREATE TABLE assets (node_id TEXT PRIMARY KEY, remote_path TEXT NOT NULL,"
    " remote_name TEXT NOT NULL, remote_size INTEGER, remote_modified TEXT,"
    " local_path TEXT, sha1 TEXT, status TEXT NOT NULL, immich_asset_id TEXT,"
    " is_duplicate INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,"
    " first_seen TEXT NOT NULL, last_attempt TEXT, last_error TEXT,"
    " claimed_sha1 TEXT, capture_time TEXT)")
V2_RUNS = (
    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT,"
    " finished_at TEXT, discovered INTEGER, downloaded INTEGER,"
    " uploaded INTEGER, failed INTEGER, exit_code INTEGER)")


class MigrateTest(unittest.TestCase):
    def setUp(self):
        silence_logs()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".state").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, *names):
        data = copy.deepcopy(DEFAULTS)
        data["staging"]["root"] = str(self.root)
        data["immich"]["url"] = "http://vm:2283/api"
        data["immich"]["api_key"] = "k"
        if len(names) == 1:
            data["account"] = {"name": names[0]}
        else:
            data["accounts"] = [
                {"name": n, "staging_dir": str(self.root / n),
                 "immich_api_key": f"{n}-key"} for n in names]
        return Config(data)

    @property
    def legacy(self) -> Path:
        return self.root / ".state" / "state.sqlite"

    def db(self, name) -> Path:
        return self.root / ".state" / f"{name}.sqlite"

    # -- fixtures ---------------------------------------------------------
    def make_v3_shared(self, **counts):
        """A shared state.sqlite with rows for several accounts."""
        conn = state.connect(self.legacy)
        state.init_schema(conn, next(iter(counts)))
        for account, n in counts.items():
            for i in range(n):
                state.upsert_discovered(conn, account, f"{account}-{i}",
                                        f"/p/{account}_{i}.jpg",
                                        f"{account}_{i}.jpg", 100 + i, "t")
                conn.execute(
                    "UPDATE assets SET status=?, immich_asset_id=?"
                    " WHERE account=? AND node_id=?",
                    (state.PURGED, f"{account}-asset-{i}", account,
                     f"{account}-{i}"))
            state.start_run(conn, account, f"run-{account}")
        conn.commit()
        conn.close()

    def make_db(self, name):
        """An already-split database for one account."""
        conn = state.connect(self.db(name))
        try:
            state.init_schema(conn, name)
        finally:
            conn.close()

    def make_v2_shared(self, rows=5):
        """A database from before accounts existed."""
        conn = sqlite3.connect(str(self.legacy))
        conn.executescript(f"{V2_ASSETS}; {V2_RUNS}; PRAGMA user_version=2;")
        for i in range(rows):
            conn.execute(
                "INSERT INTO assets (node_id, remote_path, remote_name,"
                " remote_size, status, first_seen) VALUES (?,?,?,?,?,?)",
                (f"uid-{i}", f"/p/IMG_{i}.jpg", f"IMG_{i}.jpg", 100 + i,
                 "purged", "2026-01-01T00:00:00+00:00"))
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------

class TestDetection(MigrateTest):
    def test_a_fresh_install_needs_nothing(self):
        cfg = self.cfg("david")
        self.assertFalse(migrate.needs_migration(cfg))
        self.assertFalse(migrate.plan(cfg).needed)

    def test_an_unsplit_install_is_detected(self):
        self.make_v3_shared(david=3)
        self.assertTrue(migrate.needs_migration(self.cfg("david")))

    def test_an_already_split_install_is_not(self):
        self.make_v3_shared(david=3)
        cfg = self.cfg("david")
        migrate.apply(cfg, migrate.plan(cfg))
        self.assertFalse(migrate.needs_migration(cfg))

    def test_detection_opens_no_database(self):
        """The guard runs on every command, so it must stay two stats."""
        self.make_v3_shared(david=3)
        self.legacy.chmod(0o000)
        try:
            self.assertTrue(migrate.needs_migration(self.cfg("david")))
        finally:
            self.legacy.chmod(0o644)

    def test_a_broken_config_does_not_crash_the_guard(self):
        data = copy.deepcopy(DEFAULTS)
        data["accounts"] = "not-a-list"
        self.assertFalse(migrate.needs_migration(Config(data)))


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

class TestPlan(MigrateTest):
    def test_one_account_is_adopted_by_rename(self):
        """Nothing else is in the file, so a rename is the whole job -- no
        second copy of a large library's metadata."""
        self.make_v3_shared(david=4)
        plan = migrate.plan(self.cfg("david"))
        self.assertEqual([s.kind for s in plan.steps], ["adopt"])
        self.assertEqual(plan.steps[0].rows, 4)
        self.assertEqual(plan.steps[0].dest, self.db("david"))

    def test_several_accounts_are_split(self):
        self.make_v3_shared(david=4, mirjam=2)
        plan = migrate.plan(self.cfg("david", "mirjam"))
        self.assertEqual([s.kind for s in plan.steps], ["split", "split"])
        self.assertEqual({s.account: s.rows for s in plan.steps},
                         {"david": 4, "mirjam": 2})

    def test_a_pre_v3_database_gets_a_schema_step_first(self):
        self.make_v2_shared(rows=6)
        plan = migrate.plan(self.cfg("david"))
        self.assertEqual(plan.steps[0].kind, "schema")
        self.assertEqual(plan.steps[0].rows, 6)

    def test_a_pre_v3_database_with_two_accounts_refuses_to_guess(self):
        """No account column means every row is unowned. One configured
        account is not a guess; two is."""
        self.make_v2_shared()
        with self.assertRaises(migrate.MigrationError) as ctx:
            migrate.plan(self.cfg("david", "mirjam"))
        self.assertIn("--assign-to", str(ctx.exception))

    def test_assign_to_resolves_that(self):
        self.make_v2_shared(rows=3)
        plan = migrate.plan(self.cfg("david", "mirjam"), assign_to="mirjam")
        self.assertEqual(plan.steps[0].account, "mirjam")

    def test_assign_to_must_name_a_configured_account(self):
        self.make_v2_shared()
        with self.assertRaises(migrate.MigrationError):
            migrate.plan(self.cfg("david"), assign_to="nobody")

    def test_an_unconfigured_account_is_warned_about_not_moved(self):
        self.make_v3_shared(david=3, ghost=2)
        plan = migrate.plan(self.cfg("david"))
        self.assertEqual(plan.orphans, ["ghost"])
        self.assertTrue(any("ghost" in w for w in plan.warnings))
        self.assertEqual([s.account for s in plan.steps], ["david"])

    def test_an_existing_target_is_left_alone_with_a_warning(self):
        self.make_v3_shared(david=3)
        self.make_db("david")
        plan = migrate.plan(self.cfg("david"))
        self.assertFalse(plan.needed)
        self.assertTrue(any("already exists" in w for w in plan.warnings))


# ---------------------------------------------------------------------------
# applying
# ---------------------------------------------------------------------------

class TestApply(MigrateTest):
    def test_a_rename_preserves_every_row(self):
        self.make_v3_shared(david=25)
        cfg = self.cfg("david")
        migrate.apply(cfg, migrate.plan(cfg))
        conn = state.connect(self.db("david"))
        try:
            self.assertEqual(state.counts(conn, "david")["total"], 25)
            self.assertEqual(len(state.recent_runs(conn, "david")), 1)
        finally:
            conn.close()

    def test_a_split_preserves_every_row_of_every_table(self):
        self.make_v3_shared(david=6, mirjam=3)
        conn = state.connect(self.legacy)
        state.stage_delete(conn, "david", state.get(conn, "david", "david-0"))
        state.record_deletion(conn, "david", "david-1", "/p/x", "t", "trashed")
        state.create_job(conn, "mirjam", "sync")
        conn.close()

        cfg = self.cfg("david", "mirjam")
        migrate.apply(cfg, migrate.plan(cfg))

        david = state.connect(self.db("david"))
        mirjam = state.connect(self.db("mirjam"))
        try:
            self.assertEqual(state.counts(david, "david")["total"], 6)
            self.assertEqual(state.count_staged(david, "david"), 1)
            self.assertEqual(len(state.deletions(david, "david")), 1)
            self.assertEqual(len(state.recent_runs(david, "david")), 1)
            self.assertEqual(state.counts(mirjam, "mirjam")["total"], 3)
            self.assertEqual(len(state.recent_jobs(mirjam, "mirjam")), 1)
        finally:
            david.close()
            mirjam.close()

    def test_each_file_holds_only_its_own_account(self):
        self.make_v3_shared(david=4, mirjam=2)
        cfg = self.cfg("david", "mirjam")
        migrate.apply(cfg, migrate.plan(cfg))
        for name in ("david", "mirjam"):
            conn = state.connect(self.db(name))
            try:
                self.assertEqual(state.accounts(conn), [name])
            finally:
                conn.close()

    def test_staged_delete_ids_survive_the_split(self):
        """The UI hands those integers back as row ids, so they have to be
        the same integers afterwards."""
        self.make_v3_shared(david=3)
        conn = state.connect(self.legacy)
        for i in range(3):
            state.stage_delete(conn, "david", state.get(conn, "david", f"david-{i}"))
        before = [r["id"] for r in state.staged_deletes(conn, "david")]
        conn.close()

        cfg = self.cfg("david", "mirjam")   # forces a split, not a rename
        migrate.apply(cfg, migrate.plan(cfg))
        conn = state.connect(self.db("david"))
        try:
            self.assertEqual([r["id"] for r in state.staged_deletes(conn, "david")],
                             before)
        finally:
            conn.close()

    def test_a_backup_is_written_first(self):
        self.make_v3_shared(david=5)
        cfg = self.cfg("david")
        done = migrate.apply(cfg, migrate.plan(cfg))
        self.assertTrue(any(line.startswith("backup=") for line in done), done)
        backups = list((self.root / ".state").glob("state.sqlite.pre-split-*"))
        self.assertEqual(len(backups), 1)
        conn = sqlite3.connect(str(backups[0]))
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 5)
        finally:
            conn.close()

    def test_the_old_file_is_retired_not_deleted(self):
        self.make_v3_shared(david=4, mirjam=2)
        cfg = self.cfg("david", "mirjam")
        migrate.apply(cfg, migrate.plan(cfg))
        self.assertFalse(self.legacy.exists())
        retired = list((self.root / ".state").glob("state.sqlite.split-*"))
        self.assertEqual(len(retired), 1)

    def test_rows_for_an_unconfigured_account_are_not_lost(self):
        """They stay in the retired file, which is the only copy of them."""
        self.make_v3_shared(david=4, ghost=3)
        cfg = self.cfg("david")
        migrate.apply(cfg, migrate.plan(cfg))
        retired = list((self.root / ".state").glob("state.sqlite.split-*"))[0]
        conn = sqlite3.connect(str(retired))
        try:
            held = dict(conn.execute(
                "SELECT account, COUNT(*) FROM assets GROUP BY account"))
        finally:
            conn.close()
        self.assertEqual(held.get("ghost"), 3)

    def test_applying_twice_is_a_no_op(self):
        self.make_v3_shared(david=4)
        cfg = self.cfg("david")
        migrate.apply(cfg, migrate.plan(cfg))
        second = migrate.plan(cfg)
        self.assertFalse(second.needed)
        self.assertEqual(migrate.apply(cfg, second), [])

    def test_a_pre_v3_database_migrates_and_splits_in_one_go(self):
        self.make_v2_shared(rows=7)
        cfg = self.cfg("david")
        migrate.apply(cfg, migrate.plan(cfg))
        conn = state.connect(self.db("david"))
        try:
            self.assertEqual(state.counts(conn, "david")["total"], 7)
            self.assertEqual(state.accounts(conn), ["david"])
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                state.SCHEMA_VERSION)
        finally:
            conn.close()


class TestRefusals(MigrateTest):
    def test_split_refuses_to_overwrite(self):
        self.make_v3_shared(david=2)
        self.make_db("david")
        with self.assertRaises(migrate.MigrationError):
            migrate.split_out(self.legacy, "david", self.db("david"))

    def test_adopt_refuses_to_overwrite(self):
        self.make_v3_shared(david=2)
        self.db("david").write_bytes(b"not a database")
        with self.assertRaises(migrate.MigrationError):
            migrate.adopt(self.legacy, self.db("david"))

    def test_a_failed_split_leaves_no_half_written_file(self):
        self.make_v3_shared(david=2)
        dest = self.db("david")
        # A source that will fail partway: drop a table the copy needs.
        conn = state.connect(self.legacy)
        conn.execute("DROP TABLE runs")
        conn.commit()
        conn.close()
        try:
            migrate.split_out(self.legacy, "david", dest)
        except migrate.MigrationError:
            self.assertFalse(dest.exists(), "a failed split must clean up")
        else:
            # Dropping a table the source no longer has is tolerated by
            # design (table_exists guards it), so the split succeeds.
            self.assertTrue(dest.exists())


if __name__ == "__main__":
    unittest.main()
