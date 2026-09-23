"""The loop a pipeline container runs.

The design it implements: the UI writes a `jobs` row and executes nothing;
each pipeline's own agent picks its own work out of its own database. That is
what makes the container layout possible without a docker socket, so the tests
here are mostly about the boundary -- an agent must run its own jobs, its own
schedule, and nobody else's.

The loop itself shells out to `sync.py`, which is covered elsewhere; here the
subprocess call is replaced so the tests stay fast and offline.
"""

import copy
import sys
import tempfile
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import state  # noqa: E402
from src.agent import Agent, next_due, parse_at  # noqa: E402
from src.config import DEFAULTS, Config  # noqa: E402
from tests.helpers import silence_logs  # noqa: E402


class AgentTest(unittest.TestCase):
    def setUp(self):
        silence_logs()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        data = copy.deepcopy(DEFAULTS)
        data["staging"]["root"] = str(self.root)
        data["immich"]["url"] = "http://vm:2283/api"
        data["immich"]["api_key"] = "k"
        data["accounts"] = [
            {"name": n, "staging_dir": str(self.root / n),
             "immich_api_key": f"{n}-key"} for n in ("david", "mirjam")]
        self.cfg = Config(data)
        self.accounts = {a.account_name: a for a in self.cfg.accounts}
        self.conns = {}
        for name, account in self.accounts.items():
            account.state_dir.mkdir(parents=True, exist_ok=True)
            conn = state.connect(account.db_path)
            state.init_schema(conn, name)
            self.conns[name] = conn

    def tearDown(self):
        for conn in self.conns.values():
            conn.close()
        self.tmp.cleanup()

    def agent(self, name="david", **overrides):
        account = self.accounts[name]
        for dotted, value in overrides.items():
            account.set(dotted.replace("__", "."), value)
        agent = Agent(account, config_path="/config/config.yaml")
        self.ran: list[list[str]] = []
        agent.execute = lambda argv: (self.ran.append(argv) or (0, "ok"))
        return agent


class TestSchedule(unittest.TestCase):
    def test_at_parses_hh_mm(self):
        self.assertEqual(parse_at("03:15"), (3, 15))
        self.assertEqual(parse_at("7"), (7, 0))
        self.assertIsNone(parse_at(""), "empty means jobs only, no schedule")
        self.assertIsNone(parse_at(None))

    def test_a_malformed_time_is_a_clear_error(self):
        for bad in ("half past three", "25:00", "03:99", "-1:00"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_at(bad)

    def test_next_due_is_always_in_the_future(self):
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(next_due((3, 15), now),
                         datetime(2026, 5, 2, 3, 15, tzinfo=timezone.utc))
        self.assertEqual(next_due((18, 30), now),
                         datetime(2026, 5, 1, 18, 30, tzinfo=timezone.utc))

    def test_the_exact_minute_rolls_to_tomorrow(self):
        """Strictly after, so a run at 03:15 does not immediately re-arm for
        the same 03:15 and loop."""
        now = datetime(2026, 5, 1, 3, 15, tzinfo=timezone.utc)
        self.assertEqual(next_due((3, 15), now).day, 2)


class TestScheduling(AgentTest):
    def test_no_schedule_means_jobs_only(self):
        agent = self.agent(agent__at="")
        self.assertIsNone(agent.schedule_next())
        self.assertFalse(agent.due_now())

    def test_a_schedule_arms_a_future_time(self):
        agent = self.agent(agent__at="03:15", agent__jitter_sec=0)
        due = agent.schedule_next()
        self.assertGreater(due, datetime.now(timezone.utc).astimezone())
        self.assertFalse(agent.due_now())

    def test_jitter_stays_inside_its_window(self):
        """Two pipelines share one Proton fair-use budget, so they are spread
        -- but not by more than they were told."""
        agent = self.agent(agent__at="03:15", agent__jitter_sec=600)
        base = next_due((3, 15), datetime.now(timezone.utc).astimezone())
        for _ in range(20):
            due = agent.schedule_next()
            self.assertGreaterEqual(due, base)
            self.assertLessEqual(due, base + timedelta(seconds=600))

    def test_due_now_fires_once_the_time_passes(self):
        agent = self.agent(agent__at="03:15", agent__jitter_sec=0)
        agent.schedule_next()
        self.assertTrue(agent.due_now(agent._due + timedelta(seconds=1)))


class TestJobArgv(AgentTest):
    def job(self, name, job_type, payload=None):
        job_id = state.create_job(self.conns[name], name, job_type, payload)
        return state.get_job(self.conns[name], job_id)

    def test_the_scheduled_run_is_a_plain_run(self):
        argv = self.agent().argv()
        self.assertEqual(argv[-1], "run")
        self.assertIn("--account", argv)
        self.assertEqual(argv[argv.index("--account") + 1], "david")

    def test_the_config_path_is_passed_through(self):
        argv = self.agent().argv()
        self.assertEqual(argv[argv.index("-c") + 1], "/config/config.yaml")

    def test_each_job_type_maps_to_its_subcommand(self):
        agent = self.agent()
        self.assertEqual(agent.argv(self.job("david", "sync"))[-1], "run")
        self.assertEqual(agent.argv(self.job("david", "reconcile"))[-1],
                         "reconcile")

    def test_a_real_delete_passes_yes_and_the_ids(self):
        agent = self.agent()
        argv = agent.argv(self.job("david", "delete",
                                   {"ids": [4, 9], "dry_run": False}))
        self.assertIn("delete-staged", argv)
        self.assertIn("--yes", argv)
        self.assertEqual(argv[-2:], ["4", "9"])

    def test_a_dry_run_delete_omits_yes(self):
        """Forgetting the flag can only ever be the safe direction."""
        agent = self.agent()
        argv = agent.argv(self.job("david", "delete",
                                   {"ids": [4], "dry_run": True}))
        self.assertNotIn("--yes", argv)

    def test_ids_are_coerced_to_integers(self):
        agent = self.agent()
        with self.assertRaises(ValueError):
            agent.argv(self.job("david", "delete",
                                {"ids": ["; rm -rf /"], "dry_run": False}))

    def test_an_unknown_type_is_refused(self):
        agent = self.agent()
        with self.assertRaises(ValueError):
            agent.argv(self.job("david", "mystery"))


class TestTakeJob(AgentTest):
    def test_a_queued_job_is_claimed_and_run(self):
        state.create_job(self.conns["david"], "david", "reconcile")
        agent = self.agent()
        self.assertTrue(agent.take_job(self.conns["david"]))
        self.assertEqual(len(self.ran), 1)
        self.assertEqual(self.ran[0][-1], "reconcile")
        job = state.recent_jobs(self.conns["david"], "david")[0]
        self.assertEqual(job["state"], state.JOB_DONE)
        self.assertEqual(job["exit_code"], 0)

    def test_an_empty_queue_is_not_work(self):
        agent = self.agent()
        self.assertFalse(agent.take_job(self.conns["david"]))
        self.assertEqual(self.ran, [])

    def test_an_agent_only_sees_its_own_database(self):
        """The boundary the whole layout rests on: mirjam's job lives in
        mirjam's file, which david's container does not even open."""
        state.create_job(self.conns["mirjam"], "mirjam", "sync")
        agent = self.agent("david")
        self.assertFalse(agent.take_job(self.conns["david"]))
        self.assertEqual(self.ran, [])
        self.assertEqual(
            state.recent_jobs(self.conns["mirjam"], "mirjam")[0]["state"],
            state.JOB_QUEUED)

    def test_a_row_for_another_account_is_put_back_not_run(self):
        """Only reachable if two agents were pointed at one database, which
        the layout prevents -- but running someone else's work would be worse
        than refusing it."""
        state.create_job(self.conns["david"], "mirjam", "sync")
        agent = self.agent("david")
        self.assertFalse(agent.take_job(self.conns["david"]))
        self.assertEqual(self.ran, [])
        job = state.recent_jobs(self.conns["david"], "mirjam")[0]
        self.assertEqual(job["state"], state.JOB_FAILED)
        self.assertIn("belongs to mirjam", job["detail"])

    def test_a_bad_job_row_is_failed_not_fatal(self):
        state.create_job(self.conns["david"], "david", "mystery")
        agent = self.agent()
        self.assertTrue(agent.take_job(self.conns["david"]))
        job = state.recent_jobs(self.conns["david"], "david")[0]
        self.assertEqual(job["state"], state.JOB_FAILED)
        self.assertIn("rejected", job["detail"])

    def run_for_real(self, returncode, output):
        """Exercise Agent.execute() itself, stubbing only the subprocess.

        Stubbing `execute` would remove the very logging under test.
        """
        import types
        from src import agent as agent_mod
        from src import log
        events = []
        original_run, original_emit = agent_mod.subprocess.run, log._emit
        agent_mod.subprocess.run = lambda argv, **kw: types.SimpleNamespace(
            returncode=returncode, stdout=output, stderr="")
        log._emit = lambda level, event, fields: events.append(
            (level, event, fields))
        try:
            Agent(self.accounts["david"]).execute(["true"])
        finally:
            agent_mod.subprocess.run = original_run
            log._emit = original_emit
        return [(lvl, f) for lvl, ev, f in events if ev == "agent.done"]

    def test_a_failing_run_logs_why_not_just_the_exit_code(self):
        """`agent.done exit_code=1` with no reason is useless: the detail used
        to go only into the jobs table, and the logs are where people look."""
        done = self.run_for_real(1, "pull.failed: no such root /Photos")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0][0], "warn", "a non-zero exit is not info")
        self.assertIn("no such root", done[0][1]["detail"])

    def test_a_clean_run_stays_quiet(self):
        done = self.run_for_real(0, "lots of routine output")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0][0], "info")
        self.assertNotIn("detail", done[0][1],
                         "do not dump a successful run's output every night")

    def test_mqtt_lines_from_a_clean_run_reach_the_log(self):
        """A clean run's output is not dumped, but whether MQTT published
        is exactly what `docker logs` has to show."""
        import contextlib
        import io
        published = json.dumps({"level": "info", "event": "mqtt.published"})
        noise = json.dumps({"level": "info", "event": "transition"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_for_real(0, f"{noise}\n{published}\n")
        self.assertIn('"mqtt.published"', out.getvalue())
        self.assertNotIn('"transition"', out.getvalue())

    def test_a_failing_run_is_recorded_not_swallowed(self):
        state.create_job(self.conns["david"], "david", "sync")
        agent = self.agent()
        agent.execute = lambda argv: (2, "auth failed")
        agent.take_job(self.conns["david"])
        job = state.recent_jobs(self.conns["david"], "david")[0]
        self.assertEqual(job["state"], state.JOB_FAILED)
        self.assertEqual(job["exit_code"], 2)


class TestLoop(AgentTest):
    def test_it_clears_jobs_left_running_by_a_restart(self):
        """A container restart mid-job leaves the row `running`, and the UI
        would then refuse every new job for that pipeline forever."""
        state.create_job(self.conns["david"], "david", "sync")
        state.claim_job(self.conns["david"])
        self.assertIsNotNone(state.active_job(self.conns["david"], "david"))

        agent = self.agent(agent__at="", agent__poll_sec=1)
        agent.stop()          # one pass, then exit
        agent.run()
        self.assertIsNone(state.active_job(self.conns["david"], "david"))

    def test_run_on_start_triggers_one_run(self):
        agent = self.agent(agent__at="", agent__run_on_start=True)
        agent.stop()
        agent.run()
        self.assertEqual(len(self.ran), 1)
        self.assertEqual(self.ran[0][-1], "run")

    def test_it_stops_on_a_signal(self):
        """SIGTERM is how a container is asked to stop; without a handler the
        runtime waits ten seconds and then kills it mid-download."""
        agent = self.agent(agent__at="")
        agent.install_signal_handlers()
        agent.stop()
        self.assertTrue(agent._stop.is_set())
        self.assertEqual(agent.run(), 0)


if __name__ == "__main__":
    unittest.main()
