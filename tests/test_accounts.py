"""Two accounts, one database, no cross-contamination.

The plan's hard rule: *the Proton session, the staging dir and the Immich API
key travel as one object, never as separate globals.* The failure mode is
uploading one person's photos into the other's library, which is tedious to
unpick after the fact. These tests run two complete pipelines against separate
fakes through one shared state.sqlite and check that nothing leaks either way.
"""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import state  # noqa: E402
from src.config import DEFAULTS, Config  # noqa: E402
from src.pipeline import Pipeline  # noqa: E402
from tests.helpers import (FakeImmichClient, FakeImmichServer,  # noqa: E402
                           FakeProtonBackend, FakeUploader, silence_logs)


class TwoAccountTest(unittest.TestCase):
    def setUp(self):
        silence_logs()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)

        data = copy.deepcopy(DEFAULTS)
        data["staging"]["root"] = str(root)
        data["staging"]["min_free_gb"] = 0
        data["limits"]["backoff_base_sec"] = 0
        data["reap"]["keep_days"] = 0
        data["immich"]["url"] = "http://vm:2283/api"
        data["proton"]["roots"] = ["/Photos"]
        data["accounts"] = [
            {"name": "david",
             "staging_dir": str(root / "david"),
             "proton_cache_dir": str(root / ".proton" / "david"),
             "immich_api_key": "david-key",
             "album_name": "Proton Import (David)"},
            {"name": "mirjam",
             "staging_dir": str(root / "mirjam"),
             "proton_cache_dir": str(root / ".proton" / "mirjam"),
             "immich_api_key": "mirjam-key",
             "album_name": "Proton Import (Mirjam)"},
        ]
        self.cfg = Config(data)
        self.accounts = {a.account_name: a for a in self.cfg.accounts}
        for account in self.accounts.values():
            for directory in (account.state_dir, account.ready_dir,
                              account.incoming_dir, account.batch_dir):
                directory.mkdir(parents=True, exist_ok=True)

        # One database per pipeline, all in the shared state directory.
        self.conns = {}
        for name, account in self.accounts.items():
            conn = state.connect(account.db_path)
            state.init_schema(conn, name)
            self.conns[name] = conn

        # Separate Proton volumes and separate Immich users.
        self.world = {}
        for name in self.accounts:
            server = FakeImmichServer()
            self.world[name] = {
                "backend": FakeProtonBackend(),
                "server": server,
                "client": FakeImmichClient(server),
                "uploader": FakeUploader(server),
            }

    def tearDown(self):
        for conn in self.conns.values():
            conn.close()
        self.tmp.cleanup()

    def db(self, name):
        return self.conns[name]

    def pipe(self, name, **kwargs) -> Pipeline:
        parts = self.world[name]
        return Pipeline(self.accounts[name], self.conns[name],
                        backend=parts["backend"], client=parts["client"],
                        uploader=parts["uploader"], **kwargs)

    def seed(self, name, count=2, prefix="IMG"):
        """Deliberately the same node ids and filenames in both volumes:
        Proton node ids are unique per volume, not globally."""
        backend = self.world[name]["backend"]
        for i in range(count):
            backend.add(f"/Photos/{prefix}_{i}.jpg",
                        f"{name}-content-{i}".encode() * 10,
                        node_id=f"node-{i}")


class TestIsolation(TwoAccountTest):
    def test_both_accounts_sync_through_one_database(self):
        self.seed("david", 3)
        self.seed("mirjam", 2)
        david = self.pipe("david", run_id="d1")
        david.run()
        mirjam = self.pipe("mirjam", run_id="m1")
        mirjam.run()

        self.assertEqual(david.stats.uploaded, 3)
        self.assertEqual(mirjam.stats.uploaded, 2)
        self.assertEqual(state.counts(self.db("david"), "david")["total"], 3)
        self.assertEqual(state.counts(self.db("mirjam"), "mirjam")["total"], 2)
        # Five assets across two files -- the whole point of the layout.
        self.assertEqual(
            sum(c.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"]
                for c in self.conns.values()), 5)
        self.assertEqual(
            self.db("david").execute(
                "SELECT COUNT(*) c FROM assets").fetchone()["c"], 3)

    def test_the_same_node_id_in_both_volumes_stays_two_assets(self):
        self.seed("david", 2)
        self.seed("mirjam", 2)
        self.pipe("david", run_id="d1").run()
        self.pipe("mirjam", run_id="m1").run()
        for name in ("david", "mirjam"):
            row = state.get(self.db(name), name, "node-0")
            self.assertIsNotNone(row, name)
            self.assertEqual(row["status"], state.PURGED)
        # Different content, so different checksums and different asset ids.
        self.assertNotEqual(state.get(self.db("david"), "david", "node-0")["sha1"],
                            state.get(self.db("mirjam"), "mirjam", "node-0")["sha1"])
        self.assertNotEqual(
            state.get(self.db("david"), "david", "node-0")["immich_asset_id"],
            state.get(self.db("mirjam"), "mirjam", "node-0")["immich_asset_id"])

    def test_each_account_downloads_into_its_own_staging_subtree(self):
        self.seed("david", 2)
        self.seed("mirjam", 2)
        for name, run in (("david", "d1"), ("mirjam", "m1")):
            pipeline = self.pipe(name, run_id=run)
            pipeline.pull()
            pipeline.download()
        for name in ("david", "mirjam"):
            files = [p for p in self.accounts[name].ready_dir.rglob("*")
                     if p.is_file()]
            self.assertEqual(len(files), 2, name)
            for path in files:
                self.assertIn(f"/{name}/ready/", str(path))

    def test_each_account_uploads_only_to_its_own_immich(self):
        self.seed("david", 3)
        self.seed("mirjam", 1)
        self.pipe("david", run_id="d1").run()
        self.pipe("mirjam", run_id="m1").run()
        self.assertEqual(len(self.world["david"]["server"].by_checksum), 3)
        self.assertEqual(len(self.world["mirjam"]["server"].by_checksum), 1)
        # No checksum appears in both libraries.
        self.assertEqual(
            set(self.world["david"]["server"].by_checksum)
            & set(self.world["mirjam"]["server"].by_checksum), set())

    def test_the_immich_key_travels_with_the_account(self):
        from src.immich import ImmichClient
        self.assertEqual(ImmichClient(self.accounts["david"]).api_key, "david-key")
        self.assertEqual(ImmichClient(self.accounts["mirjam"]).api_key, "mirjam-key")

    def test_the_album_name_travels_with_the_account(self):
        from src.immich import ImmichCliUploader
        david = ImmichCliUploader(self.accounts["david"])
        argv = david.build_argv(Path("/import"))
        self.assertIn("Proton Import (David)", argv)
        self.assertIn("david-key", " ".join(argv))
        self.assertNotIn("mirjam-key", " ".join(argv))

    def test_one_accounts_failure_does_not_touch_the_other(self):
        self.seed("david", 2)
        self.seed("mirjam", 2)
        self.world["david"]["backend"].fail_paths.add("/Photos/IMG_0.jpg")
        david = self.pipe("david", run_id="d1")
        david.run()
        mirjam = self.pipe("mirjam", run_id="m1")
        mirjam.run()
        self.assertEqual(state.counts(self.db("david"), "david")[state.FAILED], 1)
        self.assertEqual(state.counts(self.db("mirjam"), "mirjam")[state.FAILED], 0)
        self.assertEqual(state.backlog(self.db("mirjam"), "mirjam"), 0)

    def test_requeue_and_resume_stay_within_one_account(self):
        self.seed("david", 1)
        self.seed("mirjam", 1)
        self.pipe("david", run_id="d1").pull()
        self.pipe("mirjam", run_id="m1").pull()
        state.mark_downloading(self.db("david"), "david", "node-0")
        reset = state.resume(self.db("david"), "david")
        self.assertEqual(reset, {state.DOWNLOADING: 1})
        self.assertEqual(state.get(self.db("mirjam"), "mirjam", "node-0")["status"],
                         state.DISCOVERED)

    def test_runs_are_recorded_per_account(self):
        self.seed("david", 1)
        self.seed("mirjam", 1)
        self.pipe("david", run_id="shared-run-id").run()
        self.pipe("mirjam", run_id="shared-run-id").run()
        # The same run id in two accounts is two rows: (account, run_id) is
        # the primary key, so two staggered timers cannot collide.
        state.finish_run(self.db("david"), "david", "shared-run-id", 1, 1, 1, 0, 0)
        state.finish_run(self.db("mirjam"), "mirjam", "shared-run-id", 1, 1, 1, 0, 0)
        self.assertEqual(
            [c.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
             for c in self.conns.values()], [1, 1],
            "the same run id in two pipelines is two rows in two files")
        self.assertEqual(len(state.recent_runs(self.db("david"), "david")), 1)

    def test_status_is_reported_per_account(self):
        from src import report
        self.seed("david", 2)
        self.pipe("david", run_id="d1").run()
        david = report.build_status(self.db("david"), self.accounts["david"])
        mirjam = report.build_status(self.db("mirjam"), self.accounts["mirjam"])
        self.assertEqual(david["account"], "david")
        self.assertEqual(david["uploaded_total"], 2)
        self.assertEqual(mirjam["uploaded_total"], 0)

    def test_mqtt_topics_do_not_collide(self):
        from src import report
        topics = {report.mqtt_identity(a)[2] for a in self.accounts.values()}
        nodes = {report.mqtt_identity(a)[1] for a in self.accounts.values()}
        self.assertEqual(len(topics), 2, topics)
        self.assertEqual(len(nodes), 2, nodes)


class TestDeleteIsolation(TwoAccountTest):
    def stage_for(self, name):
        self.seed(name, 2)
        self.pipe(name, run_id=f"{name}-1").run()
        row = state.get(self.db(name), name, "node-0")
        self.world[name]["server"].trash_asset(row["immich_asset_id"],
                                               row["remote_name"])
        self.pipe(name, run_id=f"{name}-2").reconcile()
        return state.staged_deletes(self.db(name), name)

    def test_reconcile_stages_only_its_own_account(self):
        david_rows = self.stage_for("david")
        self.assertEqual(len(david_rows), 1)
        self.assertEqual(state.count_staged(self.db("mirjam"), "mirjam"), 0)

    def test_an_id_from_one_account_cannot_delete_from_the_other(self):
        """Both volumes hold `/Photos/IMG_0.jpg` at node `node-0`. Staging
        David's must not put Mirjam's file within reach."""
        david_rows = self.stage_for("david")
        staged_id = int(david_rows[0]["id"])

        self.accounts["mirjam"].set("delete.action", "execute")
        mirjam = self.pipe("mirjam", run_id="m9")
        mirjam.execute_deletes(ids=[staged_id])

        self.assertEqual(mirjam.stats.trashed, 0)
        self.assertEqual(self.world["mirjam"]["backend"].trash_calls, [])
        self.assertEqual(self.world["david"]["backend"].trash_calls, [])
        self.assertEqual(state.count_staged(self.db("david"), "david"), 1)

    def test_deleting_in_one_account_leaves_the_others_file_alone(self):
        david_rows = self.stage_for("david")
        self.seed("mirjam", 2)
        self.pipe("mirjam", run_id="m1").run()

        self.accounts["david"].set("delete.action", "execute")
        david = self.pipe("david", run_id="d9")
        david.execute_deletes(ids=[int(david_rows[0]["id"])])

        self.assertEqual(david.stats.trashed, 1)
        self.assertNotIn("/Photos/IMG_0.jpg", self.world["david"]["backend"].files)
        self.assertIn("/Photos/IMG_0.jpg", self.world["mirjam"]["backend"].files)
        self.assertEqual(state.get(self.db("david"), "david", "node-0")["status"],
                         state.REMOTE_TRASHED)
        self.assertEqual(state.get(self.db("mirjam"), "mirjam", "node-0")["status"],
                         state.PURGED)

    def test_the_audit_trail_is_per_account(self):
        david_rows = self.stage_for("david")
        self.accounts["david"].set("delete.action", "execute")
        self.pipe("david", run_id="d9").execute_deletes(
            ids=[int(david_rows[0]["id"])])
        self.assertEqual(len(state.deletions(self.db("david"), "david")), 1)
        self.assertEqual(state.deletions(self.db("mirjam"), "mirjam"), [])


class TestWebSeesBoth(TwoAccountTest):
    def test_the_api_reports_every_account(self):
        from src import web
        self.seed("david", 2)
        self.pipe("david", run_id="d1").run()
        api = web.Api(self.cfg, require_auth=False)
        data = api.get_accounts()
        self.assertEqual({a["account"] for a in data["accounts"]},
                         {"david", "mirjam"})
        by_name = {a["account"]: a for a in data["accounts"]}
        self.assertEqual(by_name["david"]["uploaded_total"], 2)
        self.assertEqual(by_name["mirjam"]["uploaded_total"], 0)

    def test_a_job_for_one_account_does_not_block_the_other(self):
        """Each account has its own lock, so a forced run for one must not be
        refused because the other is working."""
        from src import web
        api = web.Api(self.cfg, require_auth=False)
        self.assertFalse(api.create_job("david", "sync")["rejected"])
        self.assertFalse(api.create_job("mirjam", "sync")["rejected"])
        self.assertTrue(api.create_job("david", "sync")["rejected"])

    def test_the_config_route_lists_both_with_their_delete_actions(self):
        from src import web
        # Set on the config entry, not on the Account: `cfg.accounts` derives
        # fresh Account objects from the config data each time it is read, so
        # an Account is a view to use, not a place to store settings.
        self.cfg.data["accounts"][0]["delete_action"] = "execute"
        api = web.Api(self.cfg, require_auth=False)
        listed = {a["name"]: a["delete_action"]
                  for a in api.get_config()["accounts"]}
        self.assertEqual(listed, {"david": "execute", "mirjam": "mark_only"})

    def test_the_worker_builds_a_unit_name_per_account(self):
        from src import web
        self.cfg.set("web.job_runner", "systemd")
        api = web.Api(self.cfg, require_auth=False)
        worker = web.JobWorker(self.cfg, api.accounts, None)
        for name in ("david", "mirjam"):
            job_id = state.create_job(self.db(name), name, "sync")
            argv = worker.argv_for(state.get_job(self.db(name), job_id))
            self.assertEqual(argv[-1],
                             f"proton-to-immich-pipeline@{name}.service")


if __name__ == "__main__":
    unittest.main()
