#!/usr/bin/env python3
"""proton-to-immich-pipeline -- one-way Proton Drive -> staging -> Immich.

    sync.py pull | download | push | verify | reap | reconcile | run | status
    sync.py staged | delete-staged | unstage        the delete queue
    sync.py serve | web-password                    the web UI

Exit codes: 0 ok, 1 partial failure, 2 auth failure, 3 lock held.

Every command works on one account. With a single-account config that is
implicit; with an `accounts:` list, name it with --account. The account is
resolved once, into an Account object that carries its Proton session, staging
subtree and Immich key together -- see src/config.py.
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
from src.immich import ImmichConfigError, ImmichError  # noqa: E402

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_AUTH = 2
EXIT_LOCKED = 3
# Setup is wrong and every run will fail the same way until someone fixes it.
# Deliberately outside the unit's SuccessExitStatus, so systemd shows failed.
EXIT_CONFIG = 4

# Commands that move files and therefore need the staging tree. Everything
# else needs only the state directory.
# `status` is in here on purpose: it is the safe first command an install
# runs, and creating the layout is part of what makes it useful. `serve` is
# the one that must not -- it works on the *base* config, so it would leave
# stray ready/ and incoming/ directories beside the per-account ones.
NEEDS_STAGING = {"pull", "download", "push", "precheck", "verify", "reap",
                 "run", "reconcile", "delete-staged", "login", "agent",
                 "status"}

CONFIG_CANDIDATES = [
    os.environ.get("PIS_CONFIG"),
    "./config.yaml",
    "/etc/proton-to-immich-pipeline/config.yaml",
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
    parser.add_argument("-a", "--account", default=os.environ.get("PIS_ACCOUNT"),
                        help="which account to work on (required when the "
                             "config lists more than one; defaults to "
                             "$PIS_ACCOUNT, which is how a container says "
                             "which pipeline it is)")
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
    common.add_argument("-a", "--account", default=argparse.SUPPRESS,
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
    p_dl.add_argument("--precheck", action="store_true",
                      help="skip files Immich already holds, using Proton's sha1")

    p_push = sub.add_parser("push", parents=[common], help="upload staged files to Immich")
    p_push.add_argument("--limit", type=int)

    p_pre = sub.add_parser("precheck", parents=[common],
                           help="mark discovered files Immich already holds")
    p_pre.add_argument("--limit", type=int)

    p_verify = sub.add_parser("verify", parents=[common], help="confirm uploads server-side")
    p_verify.add_argument("--limit", type=int)

    p_reap = sub.add_parser("reap", parents=[common], help="purge verified files from staging")
    p_reap.add_argument("--keep-days", type=int,
                        help="retention grace period (0 = purge immediately)")

    p_run = sub.add_parser("run", parents=[common], help="pull, download, push, verify, reap")
    p_run.add_argument("--backfill", action="store_true")
    p_run.add_argument("--now", action="store_true",
                       help="retry failed assets immediately, ignoring backoff")
    p_run.add_argument("--precheck", action="store_true",
                       help="skip files Immich already holds, using Proton's sha1")

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

    # -- the delete queue --------------------------------------------------
    sub.add_parser("reconcile", parents=[common],
                   help="stage assets trashed in Immich for deletion in Proton")

    p_staged = sub.add_parser("staged", parents=[common],
                              help="list what is staged for deletion in Proton")
    p_staged.add_argument("--json", action="store_true")
    p_staged.add_argument("--csv", action="store_true",
                          help="machine-readable, for the mark_only workflow")
    p_staged.add_argument("--all", action="store_true",
                          help="include rows already trashed, failed or cancelled")

    p_del = sub.add_parser(
        "delete-staged", parents=[common],
        help="trash staged files in Proton (the only destructive command)")
    p_del.add_argument("ids", nargs="*", type=int,
                       help="staged row ids; omit to take the oldest --limit")
    p_del.add_argument("--limit", type=int,
                       help="cap this pass (never above delete.batch_cap)")
    p_del.add_argument("--yes", action="store_true",
                       help="required for a real run; without it this is a dry run")

    p_unstage = sub.add_parser(
        "unstage", parents=[common],
        help="take rows off the delete queue (restore them in Immich first)")
    p_unstage.add_argument("ids", nargs="+", type=int)

    # -- the web UI --------------------------------------------------------
    p_serve = sub.add_parser("serve", parents=[common],
                             help="serve the status and delete-queue web UI")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--bind")
    p_serve.add_argument("--no-auth", action="store_true",
                         help="skip the login (localhost development only)")

    sub.add_parser("web-password", parents=[common],
                   help="hash a password for web.password_hash")

    sub.add_parser("agent", parents=[common],
                   help="run one pipeline forever: the schedule plus the job "
                        "queue (this is what a pipeline container runs)")

    p_mig = sub.add_parser(
        "migrate", parents=[common],
        help="move an existing install to the layout the config asks for")
    p_mig.add_argument("--yes", action="store_true",
                       help="required to make changes; without it this only "
                            "prints the plan")
    p_mig.add_argument("--assign-to", metavar="ACCOUNT",
                       help="owner for rows in a database that predates "
                            "account support")

    return parser


def print_human_status(status: dict) -> None:
    def row(label, value):
        print(f"  {label:<20} {value}")

    print(f"proton-to-immich-pipeline [{status.get('account', 'default')}]")
    row("last run", status.get("last_run") or "never")
    row("last success", status.get("last_success") or "never")
    row("exit code", status.get("last_run_exit_code"))
    row("new / uploaded", f"{status.get('new')} / {status.get('uploaded')}")
    row("failed", status.get("failed"))
    row("backlog", status.get("backlog"))
    row("quarantined", status.get("quarantined"))
    row("staged for delete", f"{status.get('staged_deletes')} "
                             f"({status.get('delete_action')})")
    if status.get("delete_failed"):
        row("delete failed", status.get("delete_failed"))
    row("auth ok", status.get("auth_ok"))
    row("immich ok", status.get("immich_ok"))
    row("stale", status.get("stale"))
    row("staging free", f"{status.get('staging_free_gb')} GB")
    print("  states")
    for key, value in sorted((status.get("counts") or {}).items()):
        if value:
            print(f"    {key:<16} {value}")


def _status_for(cfg, conn, args) -> dict:
    """One account's status, probing Proton and Immich only if asked.

    Without --probe the reachability flags come from the last run's
    status.json: probing spawns a Proton CLI process, which is not something
    a bare `sync.py status` should do.
    """
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
            log.warn("status.proton_probe_failed", account=cfg.account_name,
                     detail=str(exc)[:200])
            auth_ok = False
        try:
            immich_ok = ImmichClient(cfg).auth_ok()
        except ImmichError:
            immich_ok = False

    if args.probe:
        # --probe is the "refresh everything downstream" command: it checks
        # the services, rewrites status.json *and* publishes to MQTT. It used
        # to write the file and stop, which made it useless for testing a
        # broker -- the only thing that published was a full `run`.
        return report.publish(conn, cfg, auth_ok=auth_ok, immich_ok=immich_ok)
    return report.build_status(conn, cfg, auth_ok=auth_ok, immich_ok=immich_ok)


def _status_exit(status: dict) -> int:
    if status.get("auth_ok") is False:
        return EXIT_AUTH
    return EXIT_PARTIAL if status.get("stale") else EXIT_OK


def cmd_status(cfg, conn, args) -> int:
    status = _status_for(cfg, conn, args)
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print_human_status(status)
    return _status_exit(status)


def ensure_writable(directory: Path) -> str | None:
    """Create a directory and prove we can write in it.

    Returns None on success, or the reason as a string. `mkdir` succeeding is
    not enough: a bind mount can be traversable but not writable, and the
    failure would then surface several frames deep inside sqlite or the Proton
    CLI instead of here.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return str(exc)
    probe = directory / ".write-probe"
    try:
        probe.touch()
        probe.unlink()
    except OSError as exc:
        return str(exc)
    return None


# Paths that only make sense on a host. Seeing one of these inside a container
# means the config is the bare-metal one, not the container one -- a different
# problem with a different fix from a badly-owned bind mount.
HOST_LOOKING = ("/mnt/", "/srv/", "/home/", "/opt/", "/var/lib/")


def permission_help(directory: Path, problem: str) -> str:
    """The message that actually gets someone unstuck.

    Two very different causes look identical as an errno, so this works out
    which one it is before offering a fix:

    * inside a container, a path like /mnt/immich/... is the *bare-metal*
      config mounted by mistake -- chowning anything will not help;
    * otherwise it is a bind mount whose host directory did not exist, so the
      daemon created it as root and the container is not root.
    """
    # The path we tried to create is usually a subdirectory that does not
    # exist yet; the thing with the wrong owner is the nearest one that does.
    existing = directory
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent

    head = (f"cannot use {directory}: {problem}\n\n"
            f"It must be writable by uid {os.getuid()}:{os.getgid()}. "
            f"The nearest\nexisting directory is {existing}.\n")

    inside = log.in_container()
    if inside and str(directory).startswith(HOST_LOOKING):
        return head + (
            "\n"
            "That looks like a HOST path, and this is a container -- so the\n"
            "mounted config.yaml is almost certainly the bare-metal one.\n"
            "The container config uses /state, /staging and /secrets:\n"
            "\n"
            "    cp config.docker.yaml config.yaml\n"
            "\n"
            "Chowning anything will not fix this one.")
    if inside:
        return head + (
            "\n"
            "Inside a container this is almost always a bind mount whose host\n"
            "directory did not exist, so the daemon created it as root. Fix\n"
            "it on the HOST, at the path behind that mount, using the uid in\n"
            "PIS_UID -- NOT `$(id -u)`, which is 0 if you are already root:\n"
            "\n"
            f"    sudo chown -R {os.getuid()}:{os.getgid()} /path/behind/the/mount")
    return head + (
        "\n"
        "The account running this must own it:\n"
        "\n"
        f"    sudo chown -R $(whoami) {existing}")


def cmd_migrate(base_cfg, args) -> int:
    """Print what the layout change would do, and do it with --yes.

    Never automatic. The failure this guards against is a pipeline quietly
    creating an empty `david.sqlite` next to a `state.sqlite` full of David's
    rows, then re-downloading the entire library -- so the plan is always
    shown first and nothing happens without being asked.
    """
    from src import migrate

    try:
        plan = migrate.plan(base_cfg, assign_to=args.assign_to)
    except migrate.MigrationError as exc:
        print(f"cannot plan a migration: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    for warning in plan.warnings:
        print(f"  warning: {warning}")

    if not plan.needed:
        print("  nothing to do: every account already has its own database")
        for name, path in base_cfg.db_paths().items():
            mark = "ok     " if path.exists() else "missing"
            print(f"    {mark}  {name:<16} {path}")
        return EXIT_PARTIAL if plan.warnings else EXIT_OK

    print(f"  plan ({len(plan.steps)} step(s)):")
    for step in plan.steps:
        print(f"    - {step.describe()}")

    if not args.yes:
        print()
        print("  This is a dry run. Re-run with --yes to carry it out.")
        print("  A backup is written beside the database first, and the old")
        print("  file is kept, renamed, rather than deleted.")
        return EXIT_OK

    try:
        done = migrate.apply(base_cfg, plan)
    except migrate.MigrationError as exc:
        log.error("migrate.failed", detail=str(exc))
        print(f"  migration failed: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    for line in done:
        print(f"    {line}")
    log.info("migrate.done", steps=len(plan.steps), detail=" | ".join(done))
    print()
    print("  Verify before the timer runs again:")
    for name in base_cfg.db_paths():
        print(f"    sync.py --account {name} pull --dry-run    # must be 0 new")
    return EXIT_OK


def cmd_status_all(accounts, conn, args) -> int:
    """Every account in one pass. The worst exit code wins, so a cron wrapper
    checking `sync.py status` still notices one broken account."""
    if args.json:
        payload = []
        worst = EXIT_OK
        for account in accounts:
            status = _status_for(account, conn, args)
            payload.append(status)
            worst = max(worst, _status_exit(status))
        print(json.dumps(payload, indent=2, sort_keys=True))
        return worst
    worst = EXIT_OK
    for index, account in enumerate(accounts):
        if index:
            print()
        status = _status_for(account, conn, args)
        print_human_status(status)
        worst = max(worst, _status_exit(status))
    return worst


def cmd_staged(cfg, conn, args) -> int:
    """Print the delete queue.

    `--csv` is there for the mark_only workflow: the list of paths to delete
    by hand in Proton's own web app, in a form a spreadsheet or `xargs` can
    take.
    """
    states = (state.STAGED,) if not args.all else (
        state.STAGED, state.STAGE_DELETING, state.STAGE_TRASHED,
        state.STAGE_FAILED, state.STAGE_CANCELLED)
    rows = state.staged_deletes(conn, cfg.account_name, states=states)

    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2, sort_keys=True))
        return EXIT_OK
    if args.csv:
        import csv
        writer = csv.writer(sys.stdout)
        writer.writerow(["id", "node_id", "remote_name", "remote_path",
                         "capture_time", "staged_at", "state", "error"])
        for r in rows:
            writer.writerow([r["id"], r["node_id"], r["remote_name"],
                             r["remote_path"], r["capture_time"],
                             r["staged_at"], r["state"], r["error"] or ""])
        return EXIT_OK

    if not rows:
        print("nothing staged for deletion")
        return EXIT_OK
    print(f"{len(rows)} staged for deletion in Proton "
          f"(delete.action: {cfg.delete_action})")
    print(f"  {'id':>5}  {'captured':<20} {'state':<10} path")
    for r in rows:
        captured = (r["capture_time"] or "")[:19]
        print(f"  {r['id']:>5}  {captured:<20} {r['state']:<10} {r['remote_path']}")
        if r["error"]:
            print(f"         {r['error'][:110]}")
    if cfg.delete_action == "mark_only":
        print("\n  delete.action is mark_only: delete these in Proton yourself,")
        print("  then run `sync.py delete-staged --yes` to close the rows out.")
    return EXIT_OK


def cmd_unstage(cfg, conn, args) -> int:
    count = state.unstage(conn, cfg.account_name, args.ids)
    log.info("unstage.done", rows=count)
    print(f"unstaged {count} row(s); they are ordinary completed assets again")
    return EXIT_OK


def cmd_web_password(cfg, args) -> int:
    """Hash a password for web.password_hash.

    Interactive only, and never echoed: the point of storing a hash is that
    the config on the SSD does not hold the password, so reading it from a
    command-line argument -- where it would land in shell history and `ps` --
    would defeat the exercise.
    """
    import getpass
    from src.web import hash_password
    first = getpass.getpass("web UI password: ")
    if not first:
        print("empty password; nothing written", file=sys.stderr)
        return EXIT_PARTIAL
    if first != getpass.getpass("again: "):
        print("passwords do not match", file=sys.stderr)
        return EXIT_PARTIAL
    digest = hash_password(first)
    print("\nDocker -- put this line in .env:\n")
    print(f"PIS_WEB_PASSWORD_HASH={digest}")
    # Only the real assignment may start a line with the variable name, so
    # `web-password | grep ^PIS_WEB_PASSWORD_HASH >> .env` does the right
    # thing instead of appending a sentence to the env file.
    print("\nBare metal -- put this in the config, or use the same env")
    print("line above in the service's EnvironmentFile:\n")
    print("web:")
    print(f"  password_hash: \"{digest}\"")
    print("\nThe hash is colon-separated on purpose: a `$` in it would be "
          "eaten\nby Docker Compose's .env interpolation, leaving a truncated "
          "hash and\na login that can never succeed.")
    return EXIT_OK


def exit_code_for(stats) -> int:
    if (stats.failed or stats.quarantined or stats.aborted
            or stats.delete_failed):
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

    if args.command == "web-password":
        return cmd_web_password(cfg, args)

    if args.command == "migrate":
        return cmd_migrate(cfg, args)

    # The layout guard. An install whose databases have not been split yet
    # would otherwise open an empty <account>.sqlite, find nothing in state,
    # and re-download the whole library -- so refuse, loudly, with the fix.
    from src import migrate as _migrate
    if _migrate.needs_migration(cfg):
        log.error(
            "layout.migration_required",
            detail="this install still has the shared state.sqlite and at "
                   "least one account has no database of its own; run "
                   "`sync.py migrate` to see the plan")
        print("This install predates the one-database-per-pipeline layout.\n"
              "Nothing has been changed. Run:\n\n"
              "    sync.py migrate            # shows the plan\n"
              "    sync.py migrate --yes      # carries it out\n",
              file=sys.stderr)
        return EXIT_CONFIG

    # From here on everything works on one account, and `cfg` becomes that
    # account: its Proton session, its staging subtree, its Immich key, as one
    # object. `serve` is the exception -- it spans every account.
    base_cfg = cfg
    # `status` with no --account reports on every account rather than
    # refusing: "how is the pipeline doing" is a question about all of them.
    # Every other command acts on exactly one, and says so if that is unclear.
    status_all: list | None = None
    if args.command != "serve":
        try:
            cfg = cfg.account(args.account)
        except ConfigError as exc:
            if args.command == "status" and args.account is None:
                status_all = base_cfg.accounts
                cfg = status_all[0]
            else:
                print(f"config error: {exc}", file=sys.stderr)
                return EXIT_PARTIAL

    # Validation comes *after* the account is resolved, and validates what
    # this process will actually be.
    #
    # It matters in the container layout: a pipeline container holds exactly
    # one IMMICH_API_KEY, for its own account, and that key lands on the base
    # config and so on every account in it. Checking "do two accounts share a
    # key" against a process that can only ever act as one of them reports a
    # collision that cannot happen. Validating the resolved account asks the
    # right question: are *my* settings usable?
    #
    # The whole-config review still happens where it belongs -- `status` with
    # no --account, and `serve` -- which is where a genuinely shared key is
    # worth catching.
    if args.command == "serve":
        # Holds no Proton or Immich credentials; the pipelines do.
        problems = base_cfg.validate(scope="ui")
    elif status_all is not None:
        problems = base_cfg.validate()
    else:
        problems = cfg.validate()
    if problems:
        # `status` and `login` still run -- they are what you reach for when
        # something is wrong -- but they must not stay silent about it.
        fatal = args.command not in ("status", "login")
        for problem in problems:
            (log.error if fatal else log.warn)("config.invalid", problem=problem)
        if fatal:
            return EXIT_PARTIAL

    # Only the commands that actually move files need a staging tree. `serve`
    # in particular runs against the *base* config, so creating ready/ and
    # incoming/ for it would leave stray empty directories beside the
    # per-account ones it is supposed to be reading.
    required = [cfg.state_dir]
    if args.command in NEEDS_STAGING:
        required += [cfg.ready_dir, cfg.incoming_dir, cfg.batch_dir]
        if cfg.get("proton.backend") == "proton-cli":
            # The Proton CLI writes cache, app data and logs here; failing now
            # with one clear line beats a traceback from inside a subprocess.
            required.append(cfg.proton_cache_dir)
    for directory in required:
        problem = ensure_writable(directory)
        if problem:
            log.error("directory.not_writable", path=str(directory),
                      detail=problem)
            print(permission_help(directory, problem), file=sys.stderr)
            return EXIT_CONFIG

    # `serve` spans every account, so there is no single database for it to
    # open here -- it opens each account's own, read-only, as it needs them.
    if args.command == "serve":
        from src.web import serve
        return serve(base_cfg, port=args.port, bind=args.bind,
                     require_auth=not args.no_auth)

    conn = state.connect(cfg.db_path)
    try:
        notes = state.init_schema(conn, cfg.account_name)
    except state.MigrationError as exc:
        conn.close()
        log.error("schema.migration_failed", detail=str(exc))
        return EXIT_CONFIG
    if notes:
        log.info("schema.migrated", to_version=state.SCHEMA_VERSION,
                 detail=" ".join(notes), account=cfg.account_name)

    if args.command == "status":
        try:
            if status_all is None:
                return cmd_status(cfg, conn, args)
            return cmd_status_all(status_all, conn, args)
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
        count = state.requeue(conn, cfg.account_name, args.node_ids or None)
        log.info("requeue.done", rows=count)
        conn.close()
        return EXIT_OK

    if args.command == "staged":
        try:
            return cmd_staged(cfg, conn, args)
        finally:
            conn.close()

    if args.command == "unstage":
        try:
            return cmd_unstage(cfg, conn, args)
        finally:
            conn.close()

    if args.command == "agent":
        conn.close()
        from src.agent import run_agent
        return run_agent(cfg, config_path=str(cfg.path) if cfg.path else None)

    run_id = time.strftime("%Y%m%dT%H%M%S")
    log.set_run_id(run_id)

    if getattr(args, "now", False):
        cfg.set("limits.backoff_base_sec", 0)
    if getattr(args, "precheck", False):
        cfg.set("immich.precheck_claimed_digests", True)

    try:
        with SingleInstance(cfg.lock_path):
            pipeline = Pipeline(cfg, conn, run_id=run_id, dry_run=args.dry_run)
            code = EXIT_OK
            try:
                if args.command == "run":
                    pipeline.run(backfill=args.backfill)
                else:
                    reset = state.resume(conn, cfg.account_name)
                    if reset:
                        log.info("resumed", **{k: v for k, v in reset.items()})
                    if args.command == "pull":
                        pipeline.pull()
                    elif args.command == "precheck":
                        pipeline.precheck(limit=args.limit)
                    elif args.command == "download":
                        if cfg.get("immich.precheck_claimed_digests"):
                            pipeline.precheck()
                        pipeline.download(limit=args.limit, max_bytes=args.max_bytes,
                                          backfill=args.backfill)
                    elif args.command == "push":
                        pipeline.push(limit=args.limit)
                    elif args.command == "verify":
                        pipeline.verify(limit=args.limit)
                    elif args.command == "reap":
                        pipeline.reap(keep_days=args.keep_days)
                    elif args.command == "reconcile":
                        pipeline.reconcile()
                    elif args.command == "delete-staged":
                        # --yes is the confirmation the UI does with a modal.
                        # Without it this is a dry run, whatever else is asked.
                        pipeline.execute_deletes(
                            ids=args.ids or None, limit=args.limit,
                            dry_run=args.dry_run or not args.yes)
                code = exit_code_for(pipeline.stats)
            except AuthFailure as exc:
                log.error("auth.failed", detail=str(exc)[:500])
                pipeline.auth_ok = False
                code = EXIT_AUTH
            except ImmichConfigError as exc:
                log.error("config.invalid", detail=log.condense(str(exc)))
                code = EXIT_CONFIG
            except (ProtonError, ImmichError) as exc:
                log.error("run.failed", detail=log.condense(str(exc)))
                code = EXIT_PARTIAL
            except KeyboardInterrupt:
                log.warn("run.interrupted")
                code = EXIT_PARTIAL

            stats = pipeline.stats
            if args.command == "run" and not args.dry_run:
                state.finish_run(conn, cfg.account_name, run_id,
                                 stats.discovered + stats.changed,
                                 stats.downloaded, stats.uploaded, stats.failed, code)
            if not args.dry_run:
                try:
                    report.publish(conn, cfg, auth_ok=pipeline.auth_ok,
                                   immich_ok=pipeline.immich_ok)
                except Exception as exc:  # noqa: BLE001 - never fail a run on reporting
                    log.warn("report.failed", detail=str(exc)[:200])

            log.info("done", command=args.command, account=cfg.account_name,
                     exit_code=code, **stats.as_dict())
            return code
    except BlockingIOError:
        log.warn("lock.held", path=str(cfg.lock_path))
        return EXIT_LOCKED
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
