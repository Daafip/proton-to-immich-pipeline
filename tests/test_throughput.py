"""Batched downloads and the global circuit breaker.

The two items known-issues.md listed as "Not implemented", and both matter
most at backfill scale:

* **Batching.** The CLI costs ~1.2 s of startup per invocation whatever it
  does, so one process per file is ~8 hours of pure startup for a 25k-file
  library. These tests are mostly about the constraints that make batching
  safe rather than about the saving itself.
* **The breaker.** Per-asset backoff does not help when everything starts
  failing at once; without a global stop, one rate-limited night quarantines
  hundreds of files that were never broken.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import state  # noqa: E402
from src.config import load  # noqa: E402
from src.pipeline import AuthFailure, CircuitBreaker, Pipeline  # noqa: E402
from src.proton import ProtonCliBackend  # noqa: E402
from tests.helpers import (FakeImmichClient, FakeImmichServer,  # noqa: E402
                           FakeProtonBackend, FakeUploader, silence_logs)

ACCOUNT = "default"


class ThroughputTest(unittest.TestCase):
    def setUp(self):
        silence_logs()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load(None)
        self.cfg.set("staging.root", self.tmp.name)
        self.cfg.set("immich.api_key", "k")
        self.cfg.set("immich.url", "http://vm:2283/api")
        self.cfg.set("proton.roots", ["/"])
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

    def seed(self, count, folder="/Photos", prefix="IMG"):
        for i in range(count):
            self.backend.add(f"{folder}/{prefix}_{i}.jpg",
                             f"{folder}-content-{i}".encode() * 10)

    def pulled(self, **kwargs):
        pipeline = self.pipe(**kwargs)
        pipeline.pull()
        return pipeline

    def rows(self):
        return self.conn.execute(
            "SELECT * FROM assets WHERE account=? ORDER BY node_id", (ACCOUNT,)
        ).fetchall()


# ---------------------------------------------------------------------------
# batching
# ---------------------------------------------------------------------------

class TestBatchPlanning(ThroughputTest):
    def plan(self, size):
        self.pipe().pull()
        rows = state.select_for_download(self.conn, ACCOUNT)
        return self.pipe()._plan_batches(rows, size)

    def test_a_batch_size_of_one_is_exactly_the_old_behaviour(self):
        self.seed(5)
        batches = self.plan(1)
        self.assertEqual(len(batches), 5)
        self.assertTrue(all(len(b) == 1 for b in batches))

    def test_files_are_grouped_up_to_the_batch_size(self):
        self.seed(10)
        batches = self.plan(4)
        self.assertEqual([len(b) for b in batches], [4, 4, 2])

    def test_a_batch_never_spans_two_source_folders(self):
        """All files in one call land in one destination folder, and names are
        only guaranteed unique within a folder."""
        self.seed(3, folder="/Photos/2019")
        self.seed(3, folder="/Photos/2020")
        batches = self.plan(10)
        for batch in batches:
            folders = {r["remote_path"].rsplit("/", 1)[0] for r in batch}
            self.assertEqual(len(folders), 1, batch)
        self.assertEqual(sorted(len(b) for b in batches), [3, 3])

    def test_a_duplicate_filename_gets_its_own_batch(self):
        """A name can repeat across folders and an undecryptable name falls
        back to a node uid, so uniqueness is checked, not assumed. With
        `--conflict-strategy skip` a collision would keep the wrong file."""
        self.backend.add("/Photos/a.jpg", b"first" * 10, node_id="n1")
        self.backend.add("/Photos/b.jpg", b"second" * 10, node_id="n2")
        self.pipe().pull()
        # Force a collision the walker itself could not produce.
        self.conn.execute(
            "UPDATE assets SET remote_name='a.jpg' WHERE account=? AND node_id=?",
            (ACCOUNT, "n2"))
        self.conn.commit()
        rows = state.select_for_download(self.conn, ACCOUNT)
        batches = self.pipe()._plan_batches(rows, 10)
        self.assertEqual([len(b) for b in batches], [1, 1])

    def test_an_unsafe_filename_gets_its_own_batch(self):
        """An undecryptable name falls back to a uid, which is base64 and can
        contain a slash -- not a basename the CLI will write as-is."""
        self.backend.add("/Photos/ok.jpg", b"x" * 10, node_id="n1")
        self.backend.add("/Photos/weird.jpg", b"y" * 10, node_id="n2")
        self.pipe().pull()
        self.conn.execute(
            "UPDATE assets SET remote_name='dir/evil.jpg'"
            " WHERE account=? AND node_id=?", (ACCOUNT, "n2"))
        self.conn.commit()
        rows = state.select_for_download(self.conn, ACCOUNT)
        batches = self.pipe()._plan_batches(rows, 10)
        self.assertEqual(sorted(len(b) for b in batches), [1, 1])


class TestBatchDownload(ThroughputTest):
    def test_one_call_moves_the_whole_batch(self):
        self.seed(10)
        self.cfg.set("proton.download_batch_size", 10)
        pipeline = self.pulled()
        pipeline.download()
        self.assertEqual(pipeline.stats.downloaded, 10)
        self.assertEqual(len(self.backend.batch_calls), 1)
        self.assertEqual(len(self.backend.batch_calls[0]), 10)

    def test_the_files_are_identical_to_the_unbatched_result(self):
        """Batching must not change a single byte or a single path."""
        self.seed(6)
        self.cfg.set("proton.download_batch_size", 1)
        self.pulled().download()
        singly = {p.name: p.read_bytes()
                  for p in self.cfg.ready_dir.rglob("*") if p.is_file()}
        singly_rows = {r["node_id"]: (r["local_path"], r["sha1"])
                       for r in self.rows()}

        # Start over with batching on.
        self.conn.execute("DELETE FROM assets")
        self.conn.commit()
        for path in self.cfg.ready_dir.rglob("*"):
            if path.is_file():
                path.unlink()
        self.cfg.set("proton.download_batch_size", 25)
        self.backend.batch_calls.clear()
        self.pulled().download()
        batched = {p.name: p.read_bytes()
                   for p in self.cfg.ready_dir.rglob("*") if p.is_file()}
        batched_rows = {r["node_id"]: (r["local_path"], r["sha1"])
                        for r in self.rows()}

        self.assertEqual(singly, batched)
        self.assertEqual(singly_rows, batched_rows)
        self.assertEqual(len(self.backend.batch_calls), 1)

    def test_a_full_run_still_works_batched(self):
        self.seed(30)
        pipeline = self.pipe(run_id="r1")
        pipeline.run()
        self.assertEqual(pipeline.stats.downloaded, 30)
        self.assertEqual(pipeline.stats.uploaded, 30)
        self.assertEqual(pipeline.stats.verified, 30)
        self.assertEqual(state.backlog(self.conn, ACCOUNT), 0)

    def test_each_batch_gets_its_own_scratch_folder(self):
        """A leftover from an earlier batch must never be promoted as this
        one's file -- `--conflict-strategy skip` would keep the wrong bytes."""
        self.seed(6)
        self.cfg.set("proton.download_batch_size", 2)
        self.pulled().download()
        for row in self.rows():
            self.assertEqual(row["status"], state.DOWNLOADED)
            self.assertTrue(Path(row["local_path"]).exists())
        # Nothing left behind in incoming/.
        leftovers = [p for p in self.cfg.incoming_dir.rglob("*") if p.is_file()]
        self.assertEqual(leftovers, [])

    def test_a_size_mismatch_still_fails_only_its_own_row(self):
        self.seed(4)
        self.pulled()
        self.conn.execute(
            "UPDATE assets SET remote_size=999999 WHERE account=? AND node_id=?",
            (ACCOUNT, "node-2"))
        self.conn.commit()
        pipeline = self.pipe()
        pipeline.download()
        self.assertEqual(pipeline.stats.downloaded, 3)
        self.assertEqual(pipeline.stats.failed, 1)
        self.assertEqual(state.get(self.conn, ACCOUNT, "node-2")["status"],
                         state.FAILED)

    def test_one_bad_file_does_not_charge_the_rest_of_the_batch(self):
        """The batch call fails on the bad file, so what did not land is
        retried one at a time -- exact attribution instead of 24 innocent
        rows burning an attempt each."""
        self.seed(5)
        self.cfg.set("proton.download_batch_size", 5)
        self.backend.fail_paths.add("/Photos/IMG_2.jpg")
        pipeline = self.pulled()
        pipeline.download()
        self.assertEqual(pipeline.stats.downloaded, 4)
        self.assertEqual(pipeline.stats.failed, 1)
        for row in self.rows():
            if row["node_id"] == "node-3":       # IMG_2.jpg
                self.assertEqual(row["status"], state.FAILED)
                self.assertEqual(row["attempts"], 1)
            else:
                self.assertEqual(row["status"], state.DOWNLOADED)
                self.assertEqual(row["attempts"], 0)

    def test_files_that_landed_before_a_failure_are_kept(self):
        """A batch that dies partway still leaves real bytes on disk;
        discarding them would mean transferring them again."""
        self.seed(5)
        self.cfg.set("proton.download_batch_size", 5)
        self.backend.fail_paths.add("/Photos/IMG_1.jpg")
        self.pulled().download()
        # IMG_0 landed inside the batch call before it raised.
        self.assertEqual(self.backend.downloads.count("/Photos/IMG_0.jpg"), 1,
                         "it must not be fetched twice")

    def test_an_expired_session_mid_batch_aborts_the_run(self):
        self.seed(5)
        self.backend.auth_fail_paths.add("/Photos/IMG_3.jpg")
        pipeline = self.pulled()
        with self.assertRaises(AuthFailure):
            pipeline.download()

    def test_limits_and_the_byte_cap_still_apply(self):
        self.seed(20)
        pipeline = self.pulled()
        pipeline.download(limit=7)
        self.assertEqual(pipeline.stats.downloaded, 7)
        self.assertEqual(state.counts(self.conn, ACCOUNT)[state.DISCOVERED], 13)

    def test_dry_run_downloads_nothing(self):
        self.seed(8)
        self.pipe().pull()
        pipeline = self.pipe(dry_run=True)
        pipeline.download()
        self.assertEqual(pipeline.stats.downloaded, 8)
        self.assertEqual(self.backend.batch_calls, [])
        self.assertEqual(self.backend.downloads, [])


class TestBatchArgv(unittest.TestCase):
    """The argv actually handed to the CLI."""

    def backend(self, **overrides):
        cfg = load(None)
        for key, value in overrides.items():
            cfg.set(key, value)
        return ProtonCliBackend(cfg)

    def test_one_path_argument_per_file(self):
        argv = self.backend()._template_multi(
            "download",
            ["filesystem", "download", "--conflict-strategy", "skip",
             "{path}", "{dest_dir}"],
            ["/my-files/a.jpg", "/my-files/b c.jpg"],
            dest_dir="/scratch", dest="/scratch")
        self.assertEqual(argv, ["filesystem", "download", "--conflict-strategy",
                                "skip", "/my-files/a.jpg", "/my-files/b c.jpg",
                                "/scratch"])

    def test_the_destination_is_still_a_single_folder(self):
        argv = self.backend()._template_multi(
            "download", ["filesystem", "download", "{path}", "{dest_dir}"],
            ["/a", "/b", "/c"], dest_dir="/out", dest="/out")
        self.assertEqual(argv[-1], "/out")
        self.assertEqual(argv.count("/out"), 1)

    def test_a_custom_template_is_honoured(self):
        backend = self.backend(**{"proton.cmd": {
            "download": ["fs", "get", "--json", "{path}", "{dest_dir}"]}})
        argv = backend._template_multi("download", ["ignored"], ["/a", "/b"],
                                       dest_dir="/out", dest="/out")
        self.assertEqual(argv, ["fs", "get", "--json", "/a", "/b", "/out"])

    def test_the_timeout_scales_with_the_batch(self):
        """proton.timeout_sec budgets one invocation, and one invocation now
        does the work of len(nodes) files."""
        from src.proton import RemoteNode
        backend = self.backend(**{"proton.timeout_sec": 100})
        seen = {}

        def capture(args, timeout=None):
            seen["timeout"] = timeout
        backend._run_download = capture
        nodes = [RemoteNode(node_id=f"n{i}", path=f"/p/{i}.jpg", name=f"{i}.jpg",
                            size=1, modified=None, is_folder=False)
                 for i in range(7)]
        backend.download_many(nodes, Path("/tmp/does-not-matter"))
        self.assertEqual(seen["timeout"], 700)


# ---------------------------------------------------------------------------
# the circuit breaker
# ---------------------------------------------------------------------------

class TestCircuitBreakerUnit(unittest.TestCase):
    def test_it_trips_on_the_nth_consecutive_failure(self):
        breaker = CircuitBreaker(3)
        self.assertEqual([breaker.record_failure() for _ in range(3)],
                         [False, False, True])

    def test_a_success_resets_the_run(self):
        breaker = CircuitBreaker(3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        self.assertFalse(breaker.record_failure())
        self.assertFalse(breaker.tripped)

    def test_zero_disables_it(self):
        breaker = CircuitBreaker(0)
        for _ in range(500):
            self.assertFalse(breaker.record_failure())

    def test_a_negative_threshold_is_a_typo_not_a_hair_trigger(self):
        self.assertFalse(CircuitBreaker(-5).record_failure())


class TestCircuitBreakerInDownload(ThroughputTest):
    def test_a_total_outage_stops_the_pass_early(self):
        """Every file fails, so without the breaker all 30 would burn an
        attempt; five nights of that quarantines the lot."""
        self.seed(30)
        self.cfg.set("limits.consecutive_failures", 5)
        self.cfg.set("proton.download_batch_size", 1)
        pipeline = self.pulled()
        self.backend.fail_paths.update(self.backend.files)
        pipeline.download()
        self.assertEqual(pipeline.stats.failed, 5)
        self.assertEqual(state.counts(self.conn, ACCOUNT)[state.DISCOVERED], 25)

    def test_the_rows_not_reached_keep_their_attempts(self):
        self.seed(20)
        self.cfg.set("limits.consecutive_failures", 4)
        self.cfg.set("proton.download_batch_size", 1)
        self.pulled()
        self.backend.fail_paths.update(self.backend.files)
        self.pipe().download()
        untouched = [r for r in self.rows() if r["status"] == state.DISCOVERED]
        self.assertEqual(len(untouched), 16)
        self.assertTrue(all(r["attempts"] == 0 for r in untouched))

    def test_tripping_is_reported_as_an_aborted_pass(self):
        self.seed(10)
        self.cfg.set("limits.consecutive_failures", 3)
        self.cfg.set("proton.download_batch_size", 1)
        pipeline = self.pulled()
        self.backend.fail_paths.update(self.backend.files)
        pipeline.download()
        self.assertEqual(len(pipeline.stats.aborted), 1)
        self.assertIn("consecutive failures", pipeline.stats.aborted[0])
        # aborted => exit 1, so the timer and Home Assistant both notice.
        import sync
        self.assertEqual(sync.exit_code_for(pipeline.stats), sync.EXIT_PARTIAL)

    def test_scattered_failures_do_not_trip_it(self):
        """Three bad files out of thirty is not an outage."""
        self.seed(30)
        self.cfg.set("limits.consecutive_failures", 5)
        self.cfg.set("proton.download_batch_size", 1)
        pipeline = self.pulled()
        for i in (2, 11, 25):
            self.backend.fail_paths.add(f"/Photos/IMG_{i}.jpg")
        pipeline.download()
        self.assertEqual(pipeline.stats.downloaded, 27)
        self.assertEqual(pipeline.stats.failed, 3)
        self.assertEqual(pipeline.stats.aborted, [])

    def test_it_is_off_when_set_to_zero(self):
        self.seed(12)
        self.cfg.set("limits.consecutive_failures", 0)
        self.cfg.set("proton.download_batch_size", 1)
        pipeline = self.pulled()
        self.backend.fail_paths.update(self.backend.files)
        pipeline.download()
        self.assertEqual(pipeline.stats.failed, 12)
        self.assertEqual(pipeline.stats.aborted, [])

    def test_a_tripped_pass_leaves_no_row_stuck_downloading(self):
        self.seed(20)
        self.cfg.set("limits.consecutive_failures", 3)
        self.cfg.set("proton.download_batch_size", 5)
        self.pulled()
        self.backend.fail_paths.update(self.backend.files)
        self.pipe().download()
        stuck = [r for r in self.rows() if r["status"] == state.DOWNLOADING]
        self.assertEqual(stuck, [], "resume() would rewind these, but a pass "
                                    "that stops cleanly should not leave them")


class TestCircuitBreakerInVerify(ThroughputTest):
    def test_an_immich_outage_stops_verify_early(self):
        self.seed(20)
        pipeline = self.pipe(run_id="r1")
        pipeline.pull()
        pipeline.download()
        pipeline.push()
        self.assertEqual(pipeline.stats.uploaded, 20)

        # Immich forgets everything: every verify now fails.
        self.server.by_checksum.clear()
        self.cfg.set("limits.consecutive_failures", 4)
        verifier = self.pipe(run_id="r2")
        verifier.verify()
        self.assertEqual(verifier.stats.failed, 4)
        self.assertEqual(state.counts(self.conn, ACCOUNT)[state.UPLOADED], 16)
        self.assertTrue(verifier.stats.aborted)


if __name__ == "__main__":
    unittest.main()
