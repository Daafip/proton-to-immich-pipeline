#!/usr/bin/env python3
"""proton-immich-sync -- one-way Proton Drive -> staging -> Immich.

    sync.py pull | download | push | verify | reap | run | status

Exit codes: 0 ok, 1 partial failure, 2 auth failure, 3 lock held.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import log, report, state  # noqa: E402
from src.config import ConfigError, load  # noqa: E402
from src.pipeline import AuthFailure, Pipeline  # noqa: E402
from src.proton import ProtonError  # noqa: E402
from src.immich import ImmichError  # noqa: E402

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_AUTH = 2
EXIT_LOCKED = 3

CONFIG_CANDIDATES = [
    os.environ.get("PIS_CONFIG"),
    "./config.yaml",
    "/etc/proton-immich-sync/config.yaml",
    str(Path(__file__).resolve().parent / "config.yaml"),
]


def find_config(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for candidate in CONFIG_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    return None


class SingleInstance:
    """flock guard -- a nightly timer must never overlap a running backfill."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "w")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            raise
        self.handle.write(f"{os.getpid()}\n{time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        self.handle.flush()
        return self

    def __exit__(self, *exc):
        if self.handle:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync.py", description="Proton Drive -> Immich pipeline")
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--human-logs", action="store_true",
                        help="plain text logs instead of one JSON object per line")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="make no remote or local changes")

    # The same switches are accepted after the subcommand too, so both
    # `sync.py -n pull` and `sync.py pull --dry-run` work. SUPPRESS keeps an
    # unused subcommand copy from overwriting the value given up front.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    common.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--human-logs", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("-n", "--dry-run", action="store_true",
                        default=argparse.SUPPRESS,
                        help="make no remote or local changes")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("pull", parents=[common],
                   help="discover remote nodes (no downloads)")

    p_dl = sub.add_parser("download", parents=[common],
                          help="fetch discovered nodes into staging")
    p_dl.add_argument("--limit", type=int, help="max files this pass")
    p_dl.add_argument("--max-bytes", type=int, help="max bytes this pass")
    p_dl.add_argument("--backfill", action="store_true",
                      help="use the backfill limits instead of the nightly ones")

    p_push = sub.add_parser("push", parents=[common], help="upload staged files to Immich")
    p_push.add_argument("--limit", type=int)

    p_verify = sub.add_parser("verify", parents=[common], help="confirm uploads server-side")
    p_verify.add_argument("--limit", type=int)

    p_reap = sub.add_parser("reap", parents=[common], help="purge verified files from staging")
    p_reap.add_argument("--keep-days", type=int,
                        help="retention grace period (0 = purge immediately)")

    p_run = sub.add_parser("run", parents=[common], help="pull, download, push, verify, reap")
    p_run.add_argument("--backfill", action="store_true")
    p_run.add_argument("--now", action="store_true",
                       help="retry failed assets immediately, ignoring backoff")

    p_status = sub.add_parser("status", parents=[common], help="print pipeline health")
    p_status.add_argument("--json", action="store_true", help="raw status.json")
    p_status.add_argument("--probe", action="store_true",
                          help="actively check Proton and Immich reachability")

    p_login = sub.add_parser("login", parents=[common],
                             help="sign in to Proton (serves a phone-friendly link)")
    p_login.add_argument("--port", type=int,
                         help="port for the redirect page (default 8399)")
    p_login.add_argument("--bind", default="0.0.0.0",
                         help="address to bind the redirect page to")
    p_login.add_argument("--no-serve", action="store_true",
                         help="just print the URL, start no server")
    p_login.add_argument("--timeout", type=int,
                         help="seconds to wait for the sign-in (default 300)")

    p_requeue = sub.add_parser("requeue", parents=[common], help="put failed/quarantined rows back in play")
    p_requeue.add_argument("node_ids", nargs="*")

    return parser


def print_human_status(status: dict) -> None:
    def row(label, value):
        print(f"  {label:<20} {value}")

    print("proton-immich-sync")
    row("last run", status.get("last_run") or "never")
    row("last success", status.get("last_success") or "never")
    row("exit code", status.get("last_run_exit_code"))
    row("new / uploaded", f"{status.get('new')} / {status.get('uploaded')}")
    row("failed", status.get("failed"))
    row("backlog", status.get("backlog"))
    row("quarantined", status.get("quarantined"))
    row("auth ok", status.get("auth_ok"))
    row("immich ok", status.get("immich_ok"))
    row("stale", status.get("stale"))
    row("staging free", f"{status.get('staging_free_gb')} GB")
    print("  states")
    for key, value in sorted((status.get("counts") or {}).items()):
        if value:
            print(f"    {key:<16} {value}")


def cmd_status(cfg, conn, args) -> int:
    auth_ok = immich_ok = None
    previous_path = cfg.status_path
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text())
            auth_ok, immich_ok = previous.get("auth_ok"), previous.get("immich_ok")
        except (OSError, json.JSONDecodeError):
            pass
    if args.probe:
        from src.immich import ImmichClient
        from src.proton import get_backend
        try:
            auth_ok = get_backend(cfg).auth_ok()
        except ProtonError as exc:
            log.warn("status.proton_probe_failed", detail=str(exc)[:200])
            auth_ok = False
        try:
            immich_ok = ImmichClient(cfg).auth_ok()
        except ImmichError:
            immich_ok = False

    status = report.build_status(conn, cfg, auth_ok=auth_ok, immich_ok=immich_ok)
    if args.probe:
        report.write_status(cfg.status_path, status)
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print_human_status(status)
    if status.get("auth_ok") is False:
        return EXIT_AUTH
    return EXIT_OK if not status.get("stale") else EXIT_PARTIAL


def exit_code_for(stats) -> int:
    if stats.failed or stats.quarantined or stats.aborted:
        return EXIT_PARTIAL
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = load(find_config(args.config))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_PARTIAL

    log.configure(
        json_logs=bool(cfg.get("logging.json", True)) and not args.human_logs,
        verbose=args.verbose or bool(cfg.get("logging.verbose", False)),
    )

    problems = cfg.validate()
    if problems and args.command not in ("status", "login"):
        for problem in problems:
            log.error("config.invalid", problem=problem)
        return EXIT_PARTIAL

    required = [cfg.state_dir, cfg.ready_dir, cfg.incoming_dir, cfg.batch_dir]
    if cfg.get("proton.backend") == "proton-cli":
        # The Proton CLI writes cache, app data and logs here; failing now with
        # one clear line beats a traceback from inside a subprocess call.
        required.append(cfg.proton_cache_dir)
    for directory in required:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"cannot create {directory}: {exc}", file=sys.stderr)
            return EXIT_PARTIAL

    conn = state.connect(cfg.db_path)
    state.init_schema(conn)

    if args.command == "status":
        try:
            return cmd_status(cfg, conn, args)
        finally:
            conn.close()

    if args.command == "login":
        from src.login import run_login
        try:
            return run_login(cfg, port=args.port, bind=args.bind,
                             serve=not args.no_serve, timeout=args.timeout)
        finally:
            conn.close()

    if args.command == "requeue":
        count = state.requeue(conn, args.node_ids or None)
        log.info("requeue.done", rows=count)
        conn.close()
        return EXIT_OK

    run_id = time.strftime("%Y%m%dT%H%M%S")
    log.set_run_id(run_id)

    if getattr(args, "now", False):
        cfg.set("limits.backoff_base_sec", 0)

    try:
        with SingleInstance(cfg.lock_path):
            pipeline = Pipeline(cfg, conn, run_id=run_id, dry_run=args.dry_run)
            code = EXIT_OK
            try:
                if args.command == "run":
                    pipeline.run(backfill=args.backfill)
                else:
                    reset = state.resume(conn)
                    if reset:
                        log.info("resumed", **{k: v for k, v in reset.items()})
                    if args.command == "pull":
                        pipeline.pull()
                    elif args.command == "download":
                        pipeline.download(limit=args.limit, max_bytes=args.max_bytes,
                                          backfill=args.backfill)
                    elif args.command == "push":
                        pipeline.push(limit=args.limit)
                    elif args.command == "verify":
                        pipeline.verify(limit=args.limit)
                    elif args.command == "reap":
                        pipeline.reap(keep_days=args.keep_days)
                code = exit_code_for(pipeline.stats)
            except AuthFailure as exc:
                log.error("auth.failed", detail=str(exc)[:500])
                pipeline.auth_ok = False
                code = EXIT_AUTH
            except (ProtonError, ImmichError) as exc:
                log.error("run.failed", detail=str(exc)[:500])
                code = EXIT_PARTIAL
            except KeyboardInterrupt:
                log.warn("run.interrupted")
                code = EXIT_PARTIAL

            stats = pipeline.stats
            if args.command == "run" and not args.dry_run:
                state.finish_run(conn, run_id, stats.discovered + stats.changed,
                                 stats.downloaded, stats.uploaded, stats.failed, code)
            if not args.dry_run:
                try:
                    report.publish(conn, cfg, auth_ok=pipeline.auth_ok,
                                   immich_ok=pipeline.immich_ok)
                except Exception as exc:  # noqa: BLE001 - never fail a run on reporting
                    log.warn("report.failed", detail=str(exc)[:200])

            log.info("done", command=args.command, exit_code=code, **stats.as_dict())
            return code
    except BlockingIOError:
        log.warn("lock.held", path=str(cfg.lock_path))
        return EXIT_LOCKED
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
