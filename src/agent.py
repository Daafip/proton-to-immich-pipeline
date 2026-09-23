"""The long-running loop one pipeline container runs.

In the systemd layout a timer starts `sync.py run` and the web UI spawns it
too. Neither works across a container boundary: the UI container has no Proton
session, no staging volume for that account and no business running someone
else's pipeline. So the direction is inverted.

    UI container          writes a `jobs` row into <account>.sqlite
    pipeline container    polls its own jobs table and runs the work

That is the whole design. The UI never executes anything, which means no
docker socket, no sudo and no cross-container exec -- and each pipeline
container still owns its own database, staging tree and Proton session
exactly as it does under systemd.

The same loop also owns the schedule, so a container does not need cron: it
wakes, checks whether the daily run is due, and goes back to sleep.

Runs happen in a subprocess rather than in-process, for the same reason the
web worker does it that way: `sync.py` already owns the flock, the exit codes
and the structured logging, and a wedged download takes a child process with
it instead of the agent.
"""

from __future__ import annotations

import json
import random
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import log, state

ROOT = Path(__file__).resolve().parent.parent
SYNC_PY = ROOT / "sync.py"


def parse_at(value: str | None) -> tuple[int, int] | None:
    """"03:15" -> (3, 15). None disables the schedule."""
    if not value:
        return None
    try:
        hour, _, minute = str(value).partition(":")
        h, m = int(hour), int(minute or 0)
    except ValueError:
        raise ValueError(f"agent.at must look like HH:MM, got {value!r}") from None
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"agent.at is out of range: {value!r}")
    return h, m


def next_due(at: tuple[int, int], after: datetime) -> datetime:
    """The next occurrence of a daily time, strictly after `after`."""
    candidate = after.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


class Agent:
    """One pipeline's loop. `cfg` is that account's Account object."""

    def __init__(self, cfg, config_path: str | None = None):
        self.cfg = cfg
        self.account = cfg.account_name
        self.config_path = config_path
        self.poll_sec = max(1, int(cfg.get("agent.poll_sec", 5)))
        self.at = parse_at(cfg.get("agent.at"))
        self.jitter_sec = max(0, int(cfg.get("agent.jitter_sec", 0)))
        self.timeout = int(cfg.get("agent.job_timeout_sec", 28800))
        self.run_on_start = bool(cfg.get("agent.run_on_start", False))
        self._stop = threading.Event()
        self._due: datetime | None = None

    # -- lifecycle ---------------------------------------------------------
    def stop(self, *_: object) -> None:
        log.info("agent.stopping", account=self.account)
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """SIGTERM is how a container is asked to stop. Without this the
        runtime waits ten seconds and then kills it, which can land in the
        middle of a download."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self.stop)
            except ValueError:  # pragma: no cover - not the main thread
                pass

    # -- the schedule ------------------------------------------------------
    def schedule_next(self, now: datetime | None = None) -> datetime | None:
        if self.at is None:
            self._due = None
            return None
        now = now or datetime.now(timezone.utc).astimezone()
        due = next_due(self.at, now)
        if self.jitter_sec:
            # Spread two pipelines that share a Proton account's fair-use
            # budget, and avoid every container waking at the same second.
            due += timedelta(seconds=random.randint(0, self.jitter_sec))
        self._due = due
        log.info("agent.scheduled", account=self.account, at=due.isoformat())
        return due

    def due_now(self, now: datetime | None = None) -> bool:
        if self._due is None:
            return False
        return (now or datetime.now(timezone.utc).astimezone()) >= self._due

    # -- work --------------------------------------------------------------
    def argv(self, job=None) -> list[str]:
        argv = [sys.executable, str(SYNC_PY)]
        if self.config_path:
            argv += ["-c", self.config_path]
        argv += ["--account", self.account]
        if job is None:
            return argv + ["run"]

        import json
        job_type = str(job["type"])
        if job_type == "sync":
            return argv + ["run"]
        if job_type == "reconcile":
            return argv + ["reconcile"]
        if job_type == "delete":
            payload = json.loads(job["payload"] or "{}")
            argv.append("delete-staged")
            # No --yes means a dry run, whatever else was asked for.
            if not payload.get("dry_run"):
                argv.append("--yes")
            if payload.get("limit"):
                argv += ["--limit", str(int(payload["limit"]))]
            argv += [str(int(i)) for i in (payload.get("ids") or [])]
            return argv
        raise ValueError(f"unknown job type {job_type!r}")

    def execute(self, argv: list[str]) -> tuple[int, str]:
        log.info("agent.exec", account=self.account, argv=" ".join(argv[-3:]))
        started = time.monotonic()
        output = ""
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=self.timeout, check=False,
                                  cwd=str(ROOT))
            code = proc.returncode
            output = f"{proc.stdout}\n{proc.stderr}"
            detail = log.condense(f"{proc.stderr}\n{proc.stdout}", 3000)
        except FileNotFoundError as exc:
            code, detail = 4, f"{argv[0]} not found: {exc}"
        except subprocess.TimeoutExpired:
            code, detail = 1, f"timed out after {self.timeout}s"
        seconds = round(time.monotonic() - started, 1)
        _relay_report_lines(output)
        if code == 0:
            log.info("agent.done", account=self.account, exit_code=code,
                     seconds=seconds)
        else:
            # The reason used to go only into the jobs table, so the logs --
            # which is where anyone actually looks -- showed `exit_code: 1`
            # and nothing else. A run in which every file failed was
            # indistinguishable from a clean one.
            log.warn("agent.done", account=self.account, exit_code=code,
                     seconds=seconds, detail=log.condense(detail, 1200))
        return code, detail

    def take_job(self, conn) -> bool:
        """Run one queued job for this account, if there is one."""
        job = state.claim_job(conn)
        if job is None:
            return False
        if str(job["account"]) != self.account:
            # Not ours. Only possible if someone pointed two agents at one
            # database, which the layout is designed to prevent -- but putting
            # it back is cheaper than running someone else's work.
            state.finish_job(conn, int(job["id"]), 4,
                             f"claimed by the {self.account} agent but "
                             f"belongs to {job['account']}")
            return False
        try:
            argv = self.argv(job)
        except Exception as exc:  # noqa: BLE001 - a bad row must not stop the loop
            state.finish_job(conn, int(job["id"]), 4, f"rejected: {exc}")
            log.error("agent.job_rejected", account=self.account,
                      detail=str(exc)[:300])
            return True
        code, detail = self.execute(argv)
        state.finish_job(conn, int(job["id"]), code, detail)
        return True

    # -- the loop ----------------------------------------------------------
    def run(self) -> int:
        conn = state.connect(self.cfg.db_path)
        try:
            released = state.release_stale_jobs(conn)
            if released:
                # A container restart mid-job leaves the row `running`, and
                # the UI would refuse every new one until it is cleared.
                log.info("agent.released_stale_jobs", account=self.account,
                         rows=released)
            self.schedule_next()
            log.info("agent.started", account=self.account,
                     poll_sec=self.poll_sec,
                     at=self.cfg.get("agent.at") or "no schedule",
                     db=str(self.cfg.db_path))

            if self.run_on_start:
                log.info("agent.run_on_start", account=self.account)
                self.execute(self.argv())

            while not self._stop.is_set():
                worked = False
                try:
                    worked = self.take_job(conn)
                except Exception as exc:  # noqa: BLE001 - the loop must survive
                    log.error("agent.job_failed", account=self.account,
                              detail=str(exc)[:300])
                if self._stop.is_set():
                    break
                if self.due_now():
                    self.execute(self.argv())
                    self.schedule_next()
                    worked = True
                if not worked:
                    self._stop.wait(self.poll_sec)
        finally:
            conn.close()
        log.info("agent.stopped", account=self.account)
        return 0


RELAYED_EVENTS = ("mqtt.", "report.")


def _relay_report_lines(output: str) -> None:
    """Re-print the child's MQTT and status lines as they were written.

    The child's output is captured, and a clean run's is deliberately not
    dumped into the log. That also swallowed `mqtt.published` and
    `mqtt.publish_failed`, so in a container there was no way to tell from
    `docker logs` whether anything reached the broker.
    """
    for line in output.splitlines():
        try:
            event = str(json.loads(line).get("event", ""))
        except (ValueError, AttributeError):
            # Text logs: "[warn] mqtt.publish_failed host=..."
            parts = line.split(" ", 2)
            event = parts[1] if len(parts) > 1 and line.startswith("[") else ""
        if event.startswith(RELAYED_EVENTS):
            warn = '"level": "warn"' in line or line.startswith("[warn]")
            print(line, file=sys.stderr if warn else sys.stdout, flush=True)


def run_agent(cfg, config_path: str | None = None) -> int:
    agent = Agent(cfg, config_path=config_path)
    agent.install_signal_handlers()
    return agent.run()
