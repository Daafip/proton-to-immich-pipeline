import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import state  # noqa: E402
from src.config import load  # noqa: E402
from src.pipeline import AuthFailure, Pipeline  # noqa: E402
from tests.helpers import (FakeImmichClient, FakeImmichServer, FakeProtonBackend,  # noqa: E402
                           FakeUploader, sha1_bytes, silence_logs)


class PipelineTest(unittest.TestCase):
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
        state.init_schema(self.conn)

        self.backend = FakeProtonBackend()
        self.server = FakeImmichServer()
        self.client = FakeImmichClient(self.server)
        self.uploader = FakeUploader(self.server)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def pipe(self, **kwargs) -> Pipeline:
        return Pipeline(self.cfg, self.conn, backend=self.backend,
                        client=self.client, uploader=self.uploader, **kwargs)

    def seed(self, count=3):
        for i in range(count):
            self.backend.add(f"/Photos/IMG_{i}.jpg", f"content-{i}".encode() * 10)

    def ready_files(self):
        return sorted(p for p in self.cfg.ready_dir.rglob("*") if p.is_file())


class TestHappyPath(PipelineTest):
    def test_full_run(self):
        self.seed(3)
        stats = self.pipe(run_id="r1").run()
        self.assertEqual(stats.discovered, 3)
        self.assertEqual(stats.downloaded, 3)
        self.assertEqual(stats.uploaded, 3)
        self.assertEqual(stats.verified, 3)
        self.assertEqual(stats.purged, 3)
        self.assertEqual(stats.failed, 0)
        counts = state.counts(self.conn)
        self.assertEqual(counts[state.PURGED], 3)
        self.assertEqual(self.ready_files(), [], "staging should shrink back")

    def test_files_land_in_year_month_buckets(self):
        self.backend.add("/Photos/a.jpg", b"x" * 10, modified="2026-07-15T00:00:00+00:00")
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        pipeline.download()
        self.assertTrue((self.cfg.ready_dir / "2026-07" / "a.jpg").exists())

    def test_second_run_is_a_no_op(self):
        self.seed(2)
        self.pipe(run_id="r1").run()
        second = self.pipe(run_id="r2")
        second.run()
        self.assertEqual(second.stats.discovered, 0)
        self.assertEqual(second.stats.downloaded, 0)
        self.assertEqual(second.stats.uploaded, 0)
        self.assertEqual(len(self.backend.downloads), 2, "no re-downloads")

    def test_pull_dry_run_writes_nothing(self):
        self.seed(2)
        stats = self.pipe(run_id="r1", dry_run=True).pull()
        self.assertEqual(stats.discovered, 2)
        self.assertEqual(state.counts(self.conn)["total"], 0)

    def test_pull_counts_new_changed_unchanged(self):
        self.seed(2)
        self.pipe(run_id="r1").pull()
        self.backend.add("/Photos/IMG_0.jpg", b"different content entirely")
        self.backend.add("/Photos/IMG_new.jpg", b"brand new")
        pipeline = self.pipe(run_id="r2")
        pipeline.pull()
        self.assertEqual(pipeline.stats.discovered, 1)
        self.assertEqual(pipeline.stats.changed, 1)
        self.assertEqual(pipeline.stats.unchanged, 1)

    def test_non_media_files_skipped(self):
        self.backend.add("/Photos/notes.txt", b"hello")
        self.backend.add("/Photos/a.jpg", b"image")
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        self.assertEqual(pipeline.stats.discovered, 1)
        self.assertEqual(pipeline.stats.skipped, 1)


class TestDownload(PipelineTest):
    def test_limit_caps_the_pass(self):
        self.seed(5)
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        pipeline.download(limit=2)
        self.assertEqual(pipeline.stats.downloaded, 2)
        self.assertEqual(state.counts(self.conn)[state.DISCOVERED], 3)

    def test_free_space_floor_aborts(self):
        self.seed(2)
        self.cfg.set("staging.min_free_gb", 10_000_000)
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        pipeline.download()
        self.assertEqual(pipeline.stats.downloaded, 0)
        self.assertTrue(pipeline.stats.aborted)

    def test_size_mismatch_is_caught_before_promotion(self):
        self.backend.add("/Photos/a.jpg", b"x" * 100)
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        self.backend.files["/Photos/a.jpg"] = b"x" * 40  # truncated transfer
        pipeline.download()
        row = state.get(self.conn, "node-1")
        self.assertEqual(row["status"], state.FAILED)
        self.assertIn("size mismatch", row["last_error"])
        self.assertEqual(self.ready_files(), [])

    def test_transfer_failure_retries_then_succeeds(self):
        self.backend.add("/Photos/a.jpg", b"x" * 10)
        self.backend.fail_paths.add("/Photos/a.jpg")
        first = self.pipe(run_id="r1")
        first.pull()
        first.download()
        self.assertEqual(first.stats.failed, 1)
        self.assertEqual(state.get(self.conn, "node-1")["attempts"], 1)

        self.backend.fail_paths.clear()
        second = self.pipe(run_id="r2")
        second.download()
        self.assertEqual(second.stats.downloaded, 1)
        self.assertEqual(state.get(self.conn, "node-1")["status"], state.DOWNLOADED)

    def test_quarantine_after_repeated_failures(self):
        self.backend.add("/Photos/a.jpg", b"x" * 10)
        self.backend.fail_paths.add("/Photos/a.jpg")
        self.pipe(run_id="r0").pull()
        for i in range(5):
            self.pipe(run_id=f"r{i}").download()
        self.assertEqual(state.get(self.conn, "node-1")["status"], state.QUARANTINED)

    def test_incoming_scratch_is_cleaned(self):
        self.seed(1)
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        pipeline.download()
        leftovers = [p for p in self.cfg.incoming_dir.rglob("*") if p.is_file()]
        self.assertEqual(leftovers, [])

    def test_changed_remote_is_downloaded_again(self):
        self.backend.add("/Photos/a.jpg", b"v1" * 10)
        self.pipe(run_id="r1").run()
        self.backend.add("/Photos/a.jpg", b"v2-longer" * 10,
                         node_id="node-1", modified="2026-09-01T00:00:00+00:00")
        second = self.pipe(run_id="r2")
        second.run()
        self.assertEqual(second.stats.changed, 1)
        self.assertEqual(second.stats.downloaded, 1)


class TestPush(PipelineTest):
    def prepared(self):
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        pipeline.download()
        return pipeline

    def test_upload_records_asset_ids(self):
        self.seed(2)
        pipeline = self.prepared()
        pipeline.push()
        rows = self.conn.execute("SELECT * FROM assets").fetchall()
        self.assertTrue(all(r["status"] == state.UPLOADED for r in rows))
        self.assertTrue(all(r["immich_asset_id"] for r in rows))

    def test_server_side_duplicate_is_recorded_not_failed(self):
        self.backend.add("/Photos/dupe.jpg", b"already there")
        self.server.add(sha1_bytes(b"already there"), "existing-asset")
        pipeline = self.prepared()
        pipeline.push()
        row = state.get(self.conn, "node-1")
        self.assertEqual(row["status"], state.UPLOADED)
        self.assertEqual(row["is_duplicate"], 1)
        self.assertEqual(row["immich_asset_id"], "existing-asset")
        self.assertEqual(pipeline.stats.duplicates, 1)
        self.assertEqual(self.uploader.calls, [], "duplicates are never re-sent")

    def test_batch_dir_holds_only_selected_files_and_is_removed(self):
        self.seed(2)
        pipeline = self.prepared()
        # A stale file in ready/ that is already verified must not be re-sent.
        stale = self.cfg.ready_dir / "2026-08" / "old.jpg"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"old")
        pipeline.push()
        self.assertEqual(len(self.uploader.calls), 1)
        self.assertFalse((self.cfg.batch_dir / "r1").exists())
        self.assertIsNone(self.server.asset_id_for(sha1_bytes(b"old")))

    def test_cli_failure_charges_an_attempt_to_each_row(self):
        self.seed(2)
        self.uploader.fail = True
        pipeline = self.prepared()
        pipeline.push()
        rows = self.conn.execute("SELECT * FROM assets").fetchall()
        self.assertTrue(all(r["status"] == state.FAILED for r in rows))
        self.assertTrue(all(r["attempts"] == 1 for r in rows))
        self.assertFalse((self.cfg.batch_dir / "r1").exists())

    def test_missing_local_file_fails_the_row(self):
        self.seed(1)
        pipeline = self.prepared()
        Path(state.get(self.conn, "node-1")["local_path"]).unlink()
        pipeline.push()
        row = state.get(self.conn, "node-1")
        self.assertEqual(row["status"], state.FAILED)
        self.assertIn("local file missing", row["last_error"])

    def test_api_mode_upload(self):
        self.cfg.set("immich.upload_mode", "api")
        self.seed(2)
        pipeline = self.prepared()
        pipeline.push()
        self.assertEqual(len(self.client.uploaded), 2)
        self.assertEqual(self.uploader.calls, [])

    def test_push_dry_run_changes_nothing(self):
        self.seed(1)
        self.prepared()
        pipeline = self.pipe(run_id="r2", dry_run=True)
        pipeline.push()
        self.assertEqual(state.get(self.conn, "node-1")["status"], state.DOWNLOADED)
        self.assertEqual(self.uploader.calls[0][1], True)

    def test_push_survives_precheck_outage(self):
        self.seed(1)
        pipeline = self.prepared()
        pipeline._client = FakeImmichClient(self.server, fail_precheck=True)
        pipeline.push()
        # Precheck and postcheck both unavailable -> falls back to find_by_checksum
        self.assertEqual(state.get(self.conn, "node-1")["status"], state.UPLOADED)


class TestPrecheck(PipelineTest):
    """Skip downloading what Immich already holds, using Proton's claimed sha1."""

    def seed_with_digests(self, count=3):
        contents = []
        for i in range(count):
            body = f"content-{i}".encode() * 10
            path = f"/Photos/IMG_{i}.jpg"
            self.backend.add(path, body)
            contents.append(body)
        return contents

    def pull_with_claimed(self, contents):
        """Discovery records the digest Proton reports."""
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        for i, body in enumerate(contents):
            self.conn.execute(
                "UPDATE assets SET claimed_sha1=? WHERE node_id=?",
                (sha1_bytes(body), f"node-{i + 1}"))
        self.conn.commit()
        return pipeline

    def test_files_already_in_immich_are_never_downloaded(self):
        contents = self.seed_with_digests(3)
        self.server.add(sha1_bytes(contents[0]), "existing-1")
        pipeline = self.pull_with_claimed(contents)

        pipeline.precheck()
        self.assertEqual(pipeline.stats.skipped_present, 1)
        row = state.get(self.conn, "node-1")
        self.assertEqual(row["status"], state.UPLOADED)
        self.assertEqual(row["immich_asset_id"], "existing-1")
        self.assertEqual(row["is_duplicate"], 1)
        self.assertIsNone(row["local_path"], "it was never fetched")

        pipeline.download()
        self.assertNotIn("/Photos/IMG_0.jpg", self.backend.downloads)
        self.assertEqual(len(self.backend.downloads), 2)

    def test_unknown_digests_are_left_for_download(self):
        contents = self.seed_with_digests(2)
        pipeline = self.pull_with_claimed(contents)
        pipeline.precheck()
        self.assertEqual(pipeline.stats.skipped_present, 0)
        self.assertEqual(state.counts(self.conn)[state.DISCOVERED], 2)

    def test_rows_without_a_claimed_digest_are_ignored(self):
        self.seed(2)
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        pipeline.precheck()
        self.assertEqual(pipeline.stats.skipped_present, 0)

    def test_skipped_rows_still_verify_against_the_server(self):
        contents = self.seed_with_digests(1)
        self.server.add(sha1_bytes(contents[0]), "existing-1")
        pipeline = self.pull_with_claimed(contents)
        pipeline.precheck()
        pipeline.verify()
        self.assertEqual(state.get(self.conn, "node-1")["status"], state.VERIFIED)

    def test_skipped_rows_reach_a_terminal_state(self):
        contents = self.seed_with_digests(1)
        self.server.add(sha1_bytes(contents[0]), "existing-1")
        pipeline = self.pull_with_claimed(contents)
        pipeline.precheck()
        pipeline.verify()
        pipeline.reap()
        self.assertEqual(state.get(self.conn, "node-1")["status"], state.PURGED)
        self.assertEqual(state.backlog(self.conn), 0)

    def test_precheck_outage_falls_back_to_downloading(self):
        contents = self.seed_with_digests(2)
        self.server.add(sha1_bytes(contents[0]), "existing-1")
        pipeline = self.pull_with_claimed(contents)
        pipeline._client = FakeImmichClient(self.server, fail_precheck=True)
        pipeline.precheck()
        self.assertEqual(pipeline.stats.skipped_present, 0)
        pipeline.download()
        self.assertEqual(pipeline.stats.downloaded, 2, "nothing is lost")

    def test_dry_run_records_nothing(self):
        contents = self.seed_with_digests(1)
        self.server.add(sha1_bytes(contents[0]), "existing-1")
        self.pull_with_claimed(contents)
        dry = self.pipe(run_id="r2", dry_run=True)
        dry.precheck()
        self.assertEqual(dry.stats.skipped_present, 1)
        self.assertEqual(state.get(self.conn, "node-1")["status"], state.DISCOVERED)

    def test_batching_covers_every_row(self):
        contents = self.seed_with_digests(5)
        for body in contents:
            self.server.add(sha1_bytes(body))
        pipeline = self.pull_with_claimed(contents)
        pipeline.precheck(chunk=2)
        self.assertEqual(pipeline.stats.skipped_present, 5)

    def test_run_skips_precheck_unless_enabled(self):
        contents = self.seed_with_digests(1)
        self.server.add(sha1_bytes(contents[0]), "existing-1")
        self.pull_with_claimed(contents)
        pipeline = self.pipe(run_id="r2")
        pipeline.run()
        self.assertEqual(pipeline.stats.skipped_present, 0)
        self.assertIn("/Photos/IMG_0.jpg", self.backend.downloads)

    def test_run_uses_precheck_when_enabled(self):
        contents = self.seed_with_digests(1)
        self.server.add(sha1_bytes(contents[0]), "existing-1")
        self.pull_with_claimed(contents)
        self.cfg.set("immich.precheck_claimed_digests", True)
        pipeline = self.pipe(run_id="r2")
        pipeline.run()
        self.assertEqual(pipeline.stats.skipped_present, 1)
        self.assertEqual(self.backend.downloads, [])


class TestVerifyAndReap(PipelineTest):
    def uploaded(self, count=2):
        self.seed(count)
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        pipeline.download()
        pipeline.push()
        return pipeline

    def test_verify_marks_verified(self):
        pipeline = self.uploaded(2)
        pipeline.verify()
        self.assertEqual(pipeline.stats.verified, 2)
        self.assertEqual(state.counts(self.conn)[state.VERIFIED], 2)

    def test_checksum_mismatch_fails_verification(self):
        pipeline = self.uploaded(1)
        row = state.get(self.conn, "node-1")
        self.server.corrupt.add(row["immich_asset_id"])
        pipeline.verify()
        row = state.get(self.conn, "node-1")
        self.assertEqual(row["status"], state.FAILED)
        self.assertIn("checksum mismatch", row["last_error"])

    def test_missing_asset_fails_verification(self):
        pipeline = self.uploaded(1)
        self.server.by_checksum.clear()
        pipeline.verify()
        self.assertEqual(state.get(self.conn, "node-1")["status"], state.FAILED)

    def test_reap_respects_grace_period(self):
        self.cfg.set("reap.keep_days", 7)
        pipeline = self.uploaded(1)
        pipeline.verify()
        pipeline.reap()
        self.assertEqual(pipeline.stats.purged, 0)
        self.assertEqual(len(self.ready_files()), 1)

        pipeline.reap(keep_days=0)
        self.assertEqual(pipeline.stats.purged, 1)
        self.assertEqual(self.ready_files(), [])
        self.assertEqual(state.get(self.conn, "node-1")["status"], state.PURGED)

    def test_reap_clears_abandoned_scratch_dirs(self):
        import os
        stale = self.cfg.incoming_dir / "20200101T000000"
        stale.mkdir(parents=True)
        (stale / "leftover.jpg").write_bytes(b"x")
        old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
        os.utime(stale, (old, old))
        self.pipe(run_id="r-now").reap()
        self.assertFalse(stale.exists())

    def test_reap_dry_run_keeps_files(self):
        pipeline = self.uploaded(1)
        pipeline.verify()
        dry = self.pipe(run_id="r2", dry_run=True)
        dry.reap(keep_days=0)
        self.assertEqual(len(self.ready_files()), 1)


class TestResilience(PipelineTest):
    def test_auth_failure_surfaces(self):
        self.backend.authed = False
        with self.assertRaises(AuthFailure):
            self.pipe(run_id="r1").pull()

    def test_auth_failure_mid_download_leaves_resumable_state(self):
        self.seed(2)
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        self.backend.auth_fail_paths.add("/Photos/IMG_0.jpg")
        with self.assertRaises(AuthFailure):
            pipeline.download()
        self.assertFalse(pipeline.auth_ok)
        state.resume(self.conn)
        stuck = self.conn.execute(
            "SELECT COUNT(*) c FROM assets WHERE status IN (?,?)",
            (state.DOWNLOADING, state.UPLOADING)).fetchone()["c"]
        self.assertEqual(stuck, 0)

    def test_killed_run_resumes_cleanly(self):
        self.seed(2)
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        pipeline.download()
        # Simulate a kill in the middle of a push.
        state.mark_uploading(self.conn, "node-1")
        resumed = self.pipe(run_id="r2")
        resumed.run()
        self.assertEqual(state.counts(self.conn)[state.PURGED], 2)

    def test_interrupted_download_row_is_retried(self):
        self.seed(1)
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        state.mark_downloading(self.conn, "node-1")
        second = self.pipe(run_id="r2")
        second.run()
        self.assertEqual(second.stats.downloaded, 1)


if __name__ == "__main__":
    unittest.main()
