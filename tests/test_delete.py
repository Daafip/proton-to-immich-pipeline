"""The delete queue: reconcile stages, execute trashes.

This is the only destructive code in the pipeline, so the tests here are
mostly about what it refuses to do. The acceptance criterion from the plan --
trash a photo in Immich, next sync stages it, execute from the UI, it lands in
Proton's trash, it does not come back -- is `test_the_whole_cycle` at the end.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import state  # noqa: E402
from src.config import load  # noqa: E402
from src.pipeline import AuthFailure, Pipeline  # noqa: E402
from tests.helpers import (FakeImmichClient, FakeImmichServer,  # noqa: E402
                           FakeProtonBackend, FakeUploader, silence_logs)

ACCOUNT = "default"


class DeleteTest(unittest.TestCase):
    def setUp(self):
        silence_logs()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load(None)
        self.cfg.set("staging.root", self.tmp.name)
        self.cfg.set("immich.api_key", "k")
        self.cfg.set("immich.url", "http://vm:2283/api")
        self.cfg.set("proton.roots", ["/Photos"])
        self.cfg.set("staging.min_free_gb", 0)
        self.cfg.set("limits.backoff_base_sec", 0)
        self.cfg.set("reap.keep_days", 0)
        for directory in (self.cfg.state_dir, self.cfg.ready_dir,
                          self.cfg.incoming_dir, self.cfg.batch_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.conn = state.connect(self.cfg.db_path)
        state.init_schema(self.conn, ACCOUNT)

        self.backend = FakeProtonBackend()
        self.server = FakeImmichServer()
        self.client = FakeImmichClient(self.server)
        self.uploader = FakeUploader(self.server)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def pipe(self, **kwargs) -> Pipeline:
        kwargs.setdefault("backend", self.backend)
        kwargs.setdefault("client", self.client)
        kwargs.setdefault("uploader", self.uploader)
        return Pipeline(self.cfg, self.conn, **kwargs)

    # -- fixtures ---------------------------------------------------------
    def sync(self, count=3, run_id="r1"):
        """Get `count` photos all the way into Immich, the normal way."""
        for i in range(count):
            self.backend.add(f"/Photos/IMG_{i}.jpg", f"content-{i}".encode() * 10)
        self.pipe(run_id=run_id).run()
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM assets ORDER BY node_id")]

    def trash_in_immich(self, node_id: str) -> str:
        """What "someone deleted this photo in Immich" does to our view."""
        row = state.get(self.conn, ACCOUNT, node_id)
        return self.server.trash_asset(row["immich_asset_id"],
                                       filename=row["remote_name"])

    def staged_rows(self):
        return state.staged_deletes(self.conn, ACCOUNT)

    def staged_ids(self):
        return [int(r["id"]) for r in self.staged_rows()]


# ---------------------------------------------------------------------------
# C1 / C2 -- reconcile
# ---------------------------------------------------------------------------

class TestReconcile(DeleteTest):
    def test_an_asset_trashed_in_immich_is_staged(self):
        self.sync(3)
        self.trash_in_immich("node-1")
        pipeline = self.pipe(run_id="r2")
        pipeline.reconcile()
        self.assertEqual(pipeline.stats.staged, 1)
        rows = self.staged_rows()
        self.assertEqual([r["node_id"] for r in rows], ["node-1"])
        self.assertEqual(rows[0]["remote_path"], "/Photos/IMG_0.jpg")
        self.assertEqual(rows[0]["state"], state.STAGED)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.STAGED_FOR_DELETE)

    def test_untrashed_assets_are_left_alone(self):
        self.sync(3)
        self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)
        for node_id in ("node-2", "node-3"):
            self.assertEqual(state.get(self.conn, ACCOUNT, node_id)["status"],
                             state.PURGED)

    def test_staging_is_idempotent_across_nightly_runs(self):
        """A photo sitting in Immich's trash for a week is staged once."""
        self.sync(2)
        self.trash_in_immich("node-1")
        first = self.pipe(run_id="r2")
        first.reconcile()
        second = self.pipe(run_id="r3")
        second.reconcile()
        self.assertEqual(first.stats.staged, 1)
        self.assertEqual(second.stats.staged, 0)
        self.assertEqual(len(self.staged_rows()), 1)

    def test_reconcile_never_mutates_proton(self):
        self.sync(2)
        self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()
        self.assertEqual(self.backend.trash_calls, [])
        self.assertIn("/Photos/IMG_0.jpg", self.backend.files)

    def test_the_staged_row_survives_immich_emptying_its_trash(self):
        """The reason the list lives in our DB and is not computed live.

        Immich purges its trash after ~30 days. A list recomputed from the
        server would silently drop this row, and the file would stay in Proton
        with nothing left to say it should not.
        """
        self.sync(2)
        self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()
        self.server.empty_trash()
        self.pipe(run_id="r3").reconcile()
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)
        self.assertEqual(self.staged_rows()[0]["node_id"], "node-1")

    def test_in_flight_assets_are_not_staged(self):
        """Staging a row the uploader is still working on would race it."""
        self.backend.add("/Photos/a.jpg", b"x" * 20)
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        pipeline.download()
        # `downloaded`, not yet in Immich. Pretend the server knows the id.
        self.conn.execute(
            "UPDATE assets SET immich_asset_id=? WHERE account=? AND node_id=?",
            ("asset-x", ACCOUNT, "node-1"))
        self.conn.commit()
        self.server.trash_asset("asset-x")
        second = self.pipe(run_id="r2")
        second.reconcile()
        self.assertEqual(second.stats.staged, 0)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.DOWNLOADED)

    def test_both_media_types_are_queried(self):
        self.sync(1)
        self.client.trash_queries.clear()  # sync() already ran one reconcile
        self.pipe(run_id="r2").reconcile()
        # /search/metadata takes one type per request, so the client is asked
        # for both and dedupes -- a photo-only query would miss every video.
        self.assertEqual(self.client.trash_queries, [("IMAGE", "VIDEO")])

    def test_videos_are_found_too(self):
        self.backend.add("/Photos/clip.mp4", b"video-bytes" * 10)
        self.pipe(run_id="r1").run()
        row = state.get(self.conn, ACCOUNT, "node-1")
        self.server.trash_asset(row["immich_asset_id"], "clip.mp4",
                                asset_type="VIDEO")
        pipeline = self.pipe(run_id="r2")
        pipeline.reconcile()
        self.assertEqual(pipeline.stats.staged, 1)

    def test_an_unreachable_immich_does_not_fail_the_sync(self):
        """The photos are already safely uploaded; the queue can wait a night."""
        self.sync(2)
        self.trash_in_immich("node-1")
        pipeline = self.pipe(run_id="r2", client=FakeImmichClient(
            self.server, fail_trash_search=True))
        pipeline.reconcile()
        self.assertEqual(pipeline.stats.staged, 0)
        self.assertEqual(len(pipeline.stats.aborted), 1)
        self.assertIn("reconcile", pipeline.stats.aborted[0])

    def test_a_trashed_match_is_restored_and_really_uploaded(self):
        """The bug this replaces: /assets/bulk-upload-check dedupes on
        checksum and answers just as happily for an asset in the trash. The
        row was recorded as uploaded, the photo was not in the library, and
        reconcile then staged the Proton original for deletion -- all in one
        run, on a trash entry that predated the pipeline.

        Re-uploading cannot fix it either: a trashed asset still owns its
        checksum, so the upload comes back as another duplicate. Restoring is
        the only route into the library.
        """
        from tests.helpers import sha1_bytes
        for i in range(3):
            content = f"already-there-{i}".encode() * 10
            self.backend.add(f"/Photos/OLD_{i}.jpg", content)
            self.server.trash_asset(self.server.add(sha1_bytes(content)),
                                    f"OLD_{i}.jpg")

        pipeline = self.pipe(run_id="r1")
        pipeline.run()

        self.assertEqual(pipeline.stats.restored, 3)
        self.assertEqual(pipeline.stats.uploaded, 3)
        self.assertEqual(pipeline.stats.staged, 0,
                         "nothing should be queued for deletion")
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 0)
        self.assertEqual(self.server.trash, {}, "they are back in the library")
        # And the count people actually look at is no longer stuck at zero.
        self.assertEqual(state.uploaded_total(self.conn, ACCOUNT), 3)

    def test_without_restore_it_fails_rather_than_claiming_an_upload(self):
        """`restore_trashed_duplicates: false` is for when the trash really is
        a "do not want these" pile. The asset must still never be recorded as
        uploaded while it is in the trash."""
        from tests.helpers import sha1_bytes
        self.cfg.set("immich.restore_trashed_duplicates", False)
        content = b"already-there" * 10
        self.backend.add("/Photos/OLD.jpg", content)
        self.server.trash_asset(self.server.add(sha1_bytes(content)), "OLD.jpg")

        pipeline = self.pipe(run_id="r1")
        pipeline.run()

        self.assertEqual(pipeline.stats.uploaded, 0)
        self.assertEqual(pipeline.stats.restored, 0)
        row = state.get(self.conn, ACCOUNT, "node-1")
        self.assertEqual(row["status"], state.FAILED)
        self.assertIn("in the trash", row["last_error"])
        self.assertEqual(state.uploaded_total(self.conn, ACCOUNT), 0)

    def test_a_restore_failure_does_not_fake_an_upload(self):
        """Older Immich versions move the endpoint. A failure has to surface,
        not quietly become a successful-looking upload."""
        from tests.helpers import sha1_bytes, FakeImmichClient
        content = b"already-there" * 10
        self.backend.add("/Photos/OLD.jpg", content)
        self.server.trash_asset(self.server.add(sha1_bytes(content)), "OLD.jpg")

        pipeline = self.pipe(run_id="r1", client=FakeImmichClient(
            self.server, fail_restore=True))
        pipeline.run()

        self.assertEqual(pipeline.stats.uploaded, 0)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.FAILED)

    def test_a_live_duplicate_is_still_just_a_duplicate(self):
        """An asset that is present and not trashed is a genuine duplicate --
        no restore, no failure, no extra trash query cost beyond the one."""
        from tests.helpers import sha1_bytes
        content = b"genuinely-there" * 10
        self.backend.add("/Photos/DUP.jpg", content)
        self.server.add(sha1_bytes(content))          # present, NOT trashed

        pipeline = self.pipe(run_id="r1")
        pipeline.run()
        self.assertEqual(pipeline.stats.duplicates, 1)
        self.assertEqual(pipeline.stats.restored, 0)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.PURGED)

    def test_the_ordinary_case_does_not_warn(self):
        """Uploaded by us, then trashed by a person: that is what the queue is
        for, and it should stay quiet."""
        from src import log
        self.sync(2)
        self.trash_in_immich("node-1")
        events = []
        original = log._emit
        log._emit = lambda level, event, fields: events.append(
            (level, event, fields))
        try:
            self.pipe(run_id="r2").reconcile()
        finally:
            log._emit = original
        self.assertFalse([f for lvl, ev, f in events
                          if ev == "reconcile.staged_without_uploading"])

    def test_dry_run_counts_but_writes_nothing(self):
        self.sync(2)
        self.trash_in_immich("node-1")
        pipeline = self.pipe(run_id="r2", dry_run=True)
        pipeline.reconcile()
        self.assertEqual(pipeline.stats.staged, 1)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 0)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.PURGED)

    def test_reconcile_runs_as_the_last_step_of_run(self):
        self.sync(2)
        self.trash_in_immich("node-1")
        pipeline = self.pipe(run_id="r2")
        pipeline.run()
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)

    def test_reconcile_can_be_switched_off(self):
        self.sync(2)
        self.trash_in_immich("node-1")
        self.cfg.set("reconcile.enabled", False)
        self.pipe(run_id="r2").run()
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 0)

    def test_an_asset_belonging_to_another_account_is_not_staged(self):
        """The trash query returns the whole library. Only our rows match."""
        self.sync(1)
        self.conn.execute(
            "INSERT INTO assets (account, node_id, remote_path, remote_name,"
            " status, immich_asset_id, first_seen) VALUES (?,?,?,?,?,?,?)",
            ("mirjam", "her-node", "/Photos/hers.jpg", "hers.jpg",
             state.PURGED, "her-asset", "2026-01-01T00:00:00+00:00"))
        self.conn.commit()
        self.server.trash_asset("her-asset", "hers.jpg")
        pipeline = self.pipe(run_id="r2")
        pipeline.reconcile()
        self.assertEqual(pipeline.stats.staged, 0)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 0)
        self.assertEqual(state.count_staged(self.conn, "mirjam"), 0)


# ---------------------------------------------------------------------------
# C4 -- the puller must never resurrect a staged row
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# withdrawing a staged row again
#
# The queue was append-only, so it could end up contradicting the one signal
# it is built on: photos restored in Immich stayed queued for deletion in
# Proton. The hard part is that "restored" and "purged by Immich's 30-day
# sweep" both look like *absent from the trash listing*, and they need
# opposite outcomes -- so nothing is withdrawn without positive proof the
# asset is back in the library.
# ---------------------------------------------------------------------------

class TestCancelRestored(DeleteTest):
    def stage_one(self):
        self.sync(2)
        asset_id = self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)
        return asset_id

    def test_restoring_in_immich_withdraws_the_pending_deletion(self):
        """The symptom this is for: a queue listing photos that are plainly
        in Immich and not in its trash."""
        self.stage_one()
        self.server.restore_asset(
            state.get(self.conn, ACCOUNT, "node-1")["immich_asset_id"])

        pipeline = self.pipe(run_id="r3")
        pipeline.reconcile()

        self.assertEqual(pipeline.stats.cancelled, 1)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 0)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.PURGED, "back to an ordinary completed asset")
        self.assertEqual(self.backend.trash_calls, [])
        self.assertIn("/Photos/IMG_0.jpg", self.backend.files)

    def test_emptying_the_trash_in_immich_withdraws_everything(self):
        """Restoring every photo at once is the same signal as restoring one.

        This is the path that used to be skipped entirely: an empty trash
        returned before any withdrawal could happen.
        """
        self.sync(3)
        for node_id in ("node-1", "node-2"):
            self.trash_in_immich(node_id)
        self.pipe(run_id="r2").reconcile()
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 2)

        for node_id in ("node-1", "node-2"):
            self.server.restore_asset(
                state.get(self.conn, ACCOUNT, node_id)["immich_asset_id"])

        pipeline = self.pipe(run_id="r3")
        pipeline.reconcile()
        self.assertEqual(pipeline.stats.cancelled, 2)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 0)

    def test_a_purge_is_not_a_restore(self):
        """The distinction the whole method turns on. Immich deletes its trash
        after ~30 days; the staged row is then the only surviving record that
        the Proton original should go, so it must not be withdrawn."""
        self.stage_one()
        self.server.empty_trash()

        pipeline = self.pipe(run_id="r3")
        pipeline.reconcile()

        self.assertEqual(pipeline.stats.cancelled, 0)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)

    def test_a_failed_lookup_changes_nothing(self):
        """An unreachable Immich must not quietly drain the queue."""
        self.stage_one()
        self.server.restore_asset(
            state.get(self.conn, ACCOUNT, "node-1")["immich_asset_id"])

        pipeline = self.pipe(run_id="r3", client=FakeImmichClient(
            self.server, fail_asset_lookup=True))
        pipeline.reconcile()

        self.assertEqual(pipeline.stats.cancelled, 0)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)

    def test_a_truncated_trash_scan_withdraws_nothing(self):
        """With a capped scan, "not in the trash" and "not reached yet" are
        the same answer, and one of them deletes photos."""
        self.stage_one()
        client = FakeImmichClient(self.server)
        client.trash_scan_complete = False
        # The trash listing comes back empty, which would otherwise look like
        # every staged photo having been restored.
        self.server.trash.clear()

        pipeline = self.pipe(run_id="r3", client=client)
        pipeline.reconcile()

        self.assertEqual(pipeline.stats.cancelled, 0)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)
        self.assertEqual(client.asset_lookups, [], "not even asked")

    def test_rows_already_executed_are_never_withdrawn(self):
        """The Proton file is already in Proton's trash. Sending the asset
        back would have the puller download it all over again."""
        self.stage_one()
        self.cfg.set("delete.action", "execute")
        self.pipe(run_id="r3").execute_deletes(dry_run=False)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.REMOTE_TRASHED)

        self.server.restore_asset(
            state.get(self.conn, ACCOUNT, "node-1")["immich_asset_id"])
        pipeline = self.pipe(run_id="r4")
        pipeline.reconcile()

        self.assertEqual(pipeline.stats.cancelled, 0)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.REMOTE_TRASHED)

    def test_a_dry_run_reports_without_writing(self):
        self.stage_one()
        self.server.restore_asset(
            state.get(self.conn, ACCOUNT, "node-1")["immich_asset_id"])

        self.pipe(run_id="r3", dry_run=True).reconcile()
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)

    def test_it_can_be_turned_off(self):
        """For anyone who wants the queue to stay an append-only record."""
        self.stage_one()
        self.cfg.set("reconcile.cancel_restored", False)
        self.server.restore_asset(
            state.get(self.conn, ACCOUNT, "node-1")["immich_asset_id"])

        pipeline = self.pipe(run_id="r3")
        pipeline.reconcile()
        self.assertEqual(pipeline.stats.cancelled, 0)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)

    def test_the_steady_state_makes_no_lookups(self):
        """Nothing left the trash, so there is nothing to ask about."""
        self.stage_one()
        self.client.asset_lookups.clear()
        self.pipe(run_id="r3").reconcile()
        self.assertEqual(self.client.asset_lookups, [])


class TestInImmichCount(DeleteTest):
    def test_staging_does_not_empty_the_in_immich_count(self):
        """`uploaded_total` counted exactly the statuses staging moves a row
        out of, so the number the UI shows as "in immich" fell by one for
        every photo staged -- a library with 499 photos in it read
        `in immich 0, staged 499`."""
        self.sync(3)
        self.assertEqual(state.uploaded_total(self.conn, ACCOUNT), 3)

        self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()

        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)
        self.assertEqual(state.uploaded_total(self.conn, ACCOUNT), 3,
                         "staged for deletion in Proton, still in Immich")

    def test_it_survives_the_deletion_itself(self):
        """Deleting the Proton original does not remove the Immich asset."""
        self.sync(2)
        self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()
        self.cfg.set("delete.action", "execute")
        self.pipe(run_id="r3").execute_deletes(dry_run=False)

        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.REMOTE_TRASHED)
        self.assertEqual(state.uploaded_total(self.conn, ACCOUNT), 2)


class TestNoRedownload(DeleteTest):
    def stage_one(self):
        self.sync(2)
        self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()
        self.backend.downloads.clear()

    def test_a_staged_row_is_not_rediscovered(self):
        self.stage_one()
        pipeline = self.pipe(run_id="r3")
        pipeline.pull()
        pipeline.download()
        self.assertEqual(self.backend.downloads, [])
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.STAGED_FOR_DELETE)

    def test_a_staged_row_whose_size_changes_is_still_left_alone(self):
        """The nasty case: change detection would normally reset the row to
        `discovered` and fetch it again, undoing the deletion someone asked
        for. Staged statuses are exempt."""
        self.stage_one()
        self.backend.add("/Photos/IMG_0.jpg", b"completely different content",
                         node_id="node-1")
        pipeline = self.pipe(run_id="r3")
        pipeline.pull()
        self.assertEqual(pipeline.stats.changed, 0)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.STAGED_FOR_DELETE)
        pipeline.download()
        self.assertEqual(self.backend.downloads, [])

    def test_a_staged_rows_recorded_path_is_not_overwritten(self):
        """The execute path compares against the staged path, so a rename in
        the window between staging and deletion must not move the goalposts."""
        self.stage_one()
        before = state.get(self.conn, ACCOUNT, "node-1")["remote_path"]
        self.backend.files.pop("/Photos/IMG_0.jpg")
        self.backend.add("/Photos/renamed.jpg", b"content-0" * 10,
                         node_id="node-1")
        self.pipe(run_id="r3").pull()
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["remote_path"],
                         before)

    def test_requeue_skips_the_delete_queue(self):
        self.stage_one()
        state.requeue(self.conn, ACCOUNT, ["node-1"])
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.STAGED_FOR_DELETE)

    def test_every_delete_status_is_terminal_for_the_puller(self):
        self.stage_one()
        for status in state.DELETE_STATUSES:
            self.conn.execute(
                "UPDATE assets SET status=? WHERE account=? AND node_id=?",
                (status, ACCOUNT, "node-1"))
            self.conn.commit()
            result = state.upsert_discovered(
                self.conn, ACCOUNT, "node-1", "/Photos/IMG_0.jpg", "IMG_0.jpg",
                999999, "2030-01-01T00:00:00+00:00")
            self.assertEqual(result, "staged", status)
            self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                             status)


# ---------------------------------------------------------------------------
# C3 -- the execute path
# ---------------------------------------------------------------------------

class TestExecuteDeletes(DeleteTest):
    def setUp(self):
        super().setUp()
        self.cfg.set("delete.action", "execute")

    def stage(self, count=3):
        self.sync(count)
        for i in range(1, count + 1):
            self.trash_in_immich(f"node-{i}")
        self.pipe(run_id="r2").reconcile()
        return self.staged_ids()

    def test_a_staged_file_lands_in_protons_trash(self):
        ids = self.stage(1)
        pipeline = self.pipe(run_id="r3")
        pipeline.execute_deletes(ids=ids)
        self.assertEqual(pipeline.stats.trashed, 1)
        self.assertEqual(pipeline.stats.delete_failed, 0)
        self.assertEqual(self.backend.trash_calls, ["/Photos/IMG_0.jpg"])
        self.assertNotIn("/Photos/IMG_0.jpg", self.backend.files)
        self.assertIn("/Photos/IMG_0.jpg", self.backend.trashed)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.REMOTE_TRASHED)

    def test_only_the_named_rows_are_touched(self):
        ids = self.stage(3)
        self.pipe(run_id="r3").execute_deletes(ids=[ids[0]])
        self.assertEqual(len(self.backend.trash_calls), 1)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 2)

    def test_it_never_permanently_deletes(self):
        """Proton's trash is the undo. `filesystem delete` and `empty-trash`
        are never reachable from here -- the backend has no such method."""
        self.stage(1)
        self.pipe(run_id="r3").execute_deletes()
        self.assertFalse(hasattr(self.backend, "delete"))
        self.assertFalse(hasattr(self.backend, "empty_trash"))
        self.assertEqual(len(self.backend.trashed), 1)

    def test_a_reused_path_is_refused(self):
        """The node id at the staged path must still be the staged node id.
        Otherwise the path now holds a file nobody asked about."""
        ids = self.stage(1)
        self.backend.meta["/Photos/IMG_0.jpg"]["node_id"] = "somebody-elses-node"
        pipeline = self.pipe(run_id="r3")
        pipeline.execute_deletes(ids=ids)
        self.assertEqual(pipeline.stats.trashed, 0)
        self.assertEqual(pipeline.stats.delete_failed, 1)
        self.assertEqual(self.backend.trash_calls, [])
        self.assertIn("/Photos/IMG_0.jpg", self.backend.files)
        row = self.conn.execute(
            "SELECT * FROM staged_deletes WHERE id=?", (ids[0],)).fetchone()
        self.assertEqual(row["state"], state.STAGE_FAILED)
        self.assertIn("different node", row["error"])

    def test_a_node_already_gone_counts_as_done(self):
        """Someone deleted it by hand, or a previous pass succeeded and we
        crashed before recording it. The end state is the desired one."""
        ids = self.stage(1)
        self.backend.files.pop("/Photos/IMG_0.jpg")
        pipeline = self.pipe(run_id="r3")
        pipeline.execute_deletes(ids=ids)
        self.assertEqual(pipeline.stats.trashed, 1)
        self.assertEqual(self.backend.trash_calls, [])
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.REMOTE_TRASHED)
        audit = state.deletions(self.conn, ACCOUNT)
        self.assertEqual(audit[0]["result"], "already_absent")

    def test_a_trash_failure_is_flagged_not_swallowed(self):
        ids = self.stage(1)
        self.backend.trash_fail_paths.add("/Photos/IMG_0.jpg")
        pipeline = self.pipe(run_id="r3")
        pipeline.execute_deletes(ids=ids)
        self.assertEqual(pipeline.stats.delete_failed, 1)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.DELETE_FAILED)
        self.assertIn("/Photos/IMG_0.jpg", self.backend.files)
        audit = state.deletions(self.conn, ACCOUNT)
        self.assertEqual(audit[0]["result"], "failed")
        self.assertIn("simulated trash failure", audit[0]["error"])

    def test_a_lying_trash_is_caught_by_the_verification(self):
        """`trash` exits 0 but the node is still there. Without the re-resolve
        afterwards this would be recorded as a success."""
        ids = self.stage(1)
        self.backend.trash_noop_paths.add("/Photos/IMG_0.jpg")
        pipeline = self.pipe(run_id="r3")
        pipeline.execute_deletes(ids=ids)
        self.assertEqual(pipeline.stats.trashed, 0)
        self.assertEqual(pipeline.stats.delete_failed, 1)
        row = self.conn.execute(
            "SELECT * FROM staged_deletes WHERE id=?", (ids[0],)).fetchone()
        self.assertIn("still at that path", row["error"])

    def test_an_unresolvable_node_is_not_trashed_blind(self):
        ids = self.stage(1)
        self.backend.resolve_fail_paths.add("/Photos/IMG_0.jpg")
        pipeline = self.pipe(run_id="r3")
        pipeline.execute_deletes(ids=ids)
        self.assertEqual(self.backend.trash_calls, [])
        self.assertEqual(pipeline.stats.delete_failed, 1)

    def test_an_expired_session_aborts_rather_than_guessing(self):
        ids = self.stage(2)
        self.backend.trash_auth_fail_paths.add("/Photos/IMG_0.jpg")
        pipeline = self.pipe(run_id="r3")
        with self.assertRaises(AuthFailure):
            pipeline.execute_deletes(ids=ids)
        # The row stays mid-flight; resume() rewinds it next run.
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.DELETING)
        state.resume(self.conn, ACCOUNT)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.STAGED_FOR_DELETE)
        self.assertEqual(self.staged_rows()[0]["state"], state.STAGED)

    def test_dry_run_changes_nothing_anywhere(self):
        ids = self.stage(2)
        pipeline = self.pipe(run_id="r3")
        pipeline.execute_deletes(ids=ids, dry_run=True)
        self.assertEqual(pipeline.stats.delete_skipped, 2)
        self.assertEqual(pipeline.stats.trashed, 0)
        self.assertEqual(self.backend.trash_calls, [])
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 2)
        self.assertEqual(state.deletions(self.conn, ACCOUNT), [])

    def test_the_batch_cap_bounds_every_pass(self):
        self.stage(3)
        self.cfg.set("delete.batch_cap", 2)
        pipeline = self.pipe(run_id="r3")
        pipeline.execute_deletes()
        self.assertEqual(pipeline.stats.trashed, 2)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)

    def test_the_cap_cannot_be_raised_by_the_caller(self):
        ids = self.stage(3)
        self.cfg.set("delete.batch_cap", 1)
        pipeline = self.pipe(run_id="r3")
        pipeline.execute_deletes(ids=ids, limit=99)
        self.assertEqual(pipeline.stats.trashed, 1)

    def test_a_row_already_executed_is_not_executed_again(self):
        ids = self.stage(1)
        self.pipe(run_id="r3").execute_deletes(ids=ids)
        again = self.pipe(run_id="r4")
        again.execute_deletes(ids=ids)
        self.assertEqual(again.stats.trashed, 0)
        self.assertEqual(len(self.backend.trash_calls), 1)

    def test_ids_from_another_account_resolve_to_nothing(self):
        """Row ids come from the DB scoped to the account, so a stray id
        cannot reach across. This is what makes ids-not-paths safe."""
        ids = self.stage(1)
        other = Pipeline(self.cfg.account(None), self.conn,
                         backend=self.backend, client=self.client,
                         uploader=self.uploader, run_id="r3")
        other.account = "mirjam"
        other.execute_deletes(ids=ids)
        self.assertEqual(other.stats.trashed, 0)
        self.assertEqual(self.backend.trash_calls, [])
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)

    def test_every_attempt_is_audited(self):
        ids = self.stage(2)
        self.backend.trash_fail_paths.add("/Photos/IMG_1.jpg")
        self.pipe(run_id="r3").execute_deletes(ids=ids)
        audit = state.deletions(self.conn, ACCOUNT)
        self.assertEqual(len(audit), 2)
        self.assertEqual({r["result"] for r in audit}, {"trashed", "failed"})
        for row in audit:
            self.assertTrue(row["executed_at"])
            self.assertTrue(row["staged_at"])

    def test_a_row_with_no_recorded_path_is_refused(self):
        ids = self.stage(1)
        self.conn.execute("UPDATE staged_deletes SET remote_path=NULL WHERE id=?",
                          (ids[0],))
        self.conn.commit()
        pipeline = self.pipe(run_id="r3")
        pipeline.execute_deletes(ids=ids)
        self.assertEqual(self.backend.trash_calls, [])
        self.assertEqual(pipeline.stats.delete_failed, 1)


class TestMarkOnly(DeleteTest):
    """The Photos-section case: Proton may refuse deletion from the CLI, so
    the queue is a to-do list and executing it just closes the rows out."""

    def stage(self, count=1):
        self.sync(count)
        for i in range(1, count + 1):
            self.trash_in_immich(f"node-{i}")
        self.pipe(run_id="r2").reconcile()
        return self.staged_ids()

    def test_mark_only_is_the_default(self):
        self.assertEqual(self.cfg.delete_action, "mark_only")

    def test_nothing_is_called_on_proton(self):
        ids = self.stage(1)
        pipeline = self.pipe(run_id="r3")
        pipeline.execute_deletes(ids=ids)
        self.assertEqual(self.backend.trash_calls, [])
        self.assertIn("/Photos/IMG_0.jpg", self.backend.files)

    def test_the_row_is_still_closed_out_and_audited(self):
        ids = self.stage(1)
        pipeline = self.pipe(run_id="r3")
        pipeline.execute_deletes(ids=ids)
        self.assertEqual(pipeline.stats.trashed, 1)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.REMOTE_TRASHED)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 0)
        audit = state.deletions(self.conn, ACCOUNT)
        self.assertEqual(audit[0]["result"], "mark_only")

    def test_dry_run_still_does_nothing(self):
        ids = self.stage(1)
        pipeline = self.pipe(run_id="r3")
        pipeline.execute_deletes(ids=ids, dry_run=True)
        self.assertEqual(pipeline.stats.trashed, 0)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)


class TestUnstage(DeleteTest):
    def test_a_row_can_be_taken_back_off_the_queue(self):
        """The escape hatch: restore it in Immich, unstage it here, and it is
        an ordinary completed asset again -- not a re-download."""
        self.sync(1)
        self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()
        ids = state.staged_deletes(self.conn, ACCOUNT)
        self.assertEqual(state.unstage(self.conn, ACCOUNT,
                                       [int(r["id"]) for r in ids]), 1)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.PURGED)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 0)
        pipeline = self.pipe(run_id="r3")
        pipeline.pull()
        pipeline.download()
        self.assertEqual(pipeline.stats.downloaded, 0)

    def test_a_failed_row_can_be_unstaged(self):
        self.cfg.set("delete.action", "execute")
        self.sync(1)
        self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()
        ids = self.staged_ids()
        self.backend.trash_fail_paths.add("/Photos/IMG_0.jpg")
        self.pipe(run_id="r3").execute_deletes(ids=ids)
        self.assertEqual(state.unstage(self.conn, ACCOUNT, ids), 1)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.PURGED)

    def test_an_unstaged_row_still_in_immich_trash_is_staged_again(self):
        """Documented behaviour -- "restore it in Immich too, or the next sync
        will simply stage it again". The bug this guards is the asset going to
        `staged_for_delete` while its queue row stayed `cancelled`: invisible
        in the UI and terminal for the puller, so a stuck photo.
        """
        self.sync(1)
        self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()
        state.unstage(self.conn, ACCOUNT, self.staged_ids())
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 0)

        again = self.pipe(run_id="r3")
        again.reconcile()
        self.assertEqual(again.stats.staged, 1)
        rows = self.staged_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], state.STAGED)
        self.assertIsNone(rows[0]["executed_at"])
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.STAGED_FOR_DELETE)

    def test_a_failed_row_is_not_silently_reset_each_night(self):
        """A delete that failed stays visible as `failed` with its reason,
        rather than being quietly re-staged by the next reconcile."""
        self.cfg.set("delete.action", "execute")
        self.sync(1)
        self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()
        self.backend.trash_fail_paths.add("/Photos/IMG_0.jpg")
        self.pipe(run_id="r3").execute_deletes(ids=self.staged_ids())

        nightly = self.pipe(run_id="r4")
        nightly.reconcile()
        self.assertEqual(nightly.stats.staged, 0)
        row = state.staged_deletes(self.conn, ACCOUNT,
                                   states=(state.STAGE_FAILED,))[0]
        self.assertIn("simulated trash failure", row["error"])

    def test_resync_sends_a_row_back_through_the_whole_pipeline(self):
        """The recovery path for rows that were never really uploaded.

        Plain unstage returns them to `purged`, which is terminal for the
        puller -- nothing would ever retry them, and they would sit there
        looking synced while absent from Immich.
        """
        self.sync(1)
        self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()
        ids = self.staged_ids()

        self.assertEqual(state.unstage(self.conn, ACCOUNT, ids, resync=True), 1)
        row = state.get(self.conn, ACCOUNT, "node-1")
        self.assertEqual(row["status"], state.DISCOVERED)
        self.assertIsNone(row["immich_asset_id"], "forget what Immich matched")
        self.assertIsNone(row["sha1"])
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 0)

        # And it genuinely goes round again, rather than being skipped.
        pipeline = self.pipe(run_id="r3")
        pipeline.pull()
        pipeline.download()
        self.assertEqual(pipeline.stats.downloaded, 1)

    def test_plain_unstage_still_does_not_re_download(self):
        """The other case: trashed in Immich by accident, restored there. The
        asset is fine where it is and must not be fetched again."""
        self.sync(1)
        self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()
        state.unstage(self.conn, ACCOUNT, self.staged_ids())
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-1")["status"],
                         state.PURGED)
        pipeline = self.pipe(run_id="r3")
        pipeline.pull()
        pipeline.download()
        self.assertEqual(pipeline.stats.downloaded, 0)

    def test_an_already_trashed_row_cannot_be_unstaged(self):
        self.cfg.set("delete.action", "execute")
        self.sync(1)
        self.trash_in_immich("node-1")
        self.pipe(run_id="r2").reconcile()
        ids = self.staged_ids()
        self.pipe(run_id="r3").execute_deletes(ids=ids)
        self.assertEqual(state.unstage(self.conn, ACCOUNT, ids), 0)


class TestAcceptance(DeleteTest):
    def test_the_whole_cycle(self):
        """The plan's acceptance criterion, end to end.

        Trash a photo in Immich -> next sync stages it -> execute -> it lands
        in Proton's trash -> it does not reappear on the next sync.
        """
        self.cfg.set("delete.action", "execute")
        self.sync(3)

        # 1. someone deletes one photo in Immich
        self.trash_in_immich("node-2")

        # 2. the next nightly sync stages it
        nightly = self.pipe(run_id="r2")
        nightly.run()
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 1)
        staged = self.staged_rows()[0]
        self.assertEqual(staged["remote_name"], "IMG_1.jpg")

        # 3. the operator executes it from the UI
        executor = self.pipe(run_id="r3")
        executor.execute_deletes(ids=[int(staged["id"])])
        self.assertEqual(executor.stats.trashed, 1)
        self.assertIn("/Photos/IMG_1.jpg", self.backend.trashed)
        self.assertNotIn("/Photos/IMG_1.jpg", self.backend.files)

        # 4. it does not come back, and the others are untouched
        after = self.pipe(run_id="r4")
        after.run()
        self.assertEqual(after.stats.discovered, 0)
        self.assertEqual(after.stats.downloaded, 0)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-2")["status"],
                         state.REMOTE_TRASHED)
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 0)
        for node_id in ("node-1", "node-3"):
            self.assertEqual(state.get(self.conn, ACCOUNT, node_id)["status"],
                             state.PURGED)
        self.assertEqual(sorted(self.backend.files),
                         ["/Photos/IMG_0.jpg", "/Photos/IMG_2.jpg"])


if __name__ == "__main__":
    unittest.main()
