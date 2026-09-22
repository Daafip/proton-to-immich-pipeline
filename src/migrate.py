"""Moving an existing install to the layout the config asks for.

There have been two layout changes so far and there will be more, so this is
written as *compare what is on disk to what the config wants, then say what
would fix it* rather than as a ladder of one-off upgrade scripts:

    v1/v2   one state.sqlite, no account column
    v3      one state.sqlite, rows tagged with an account
    v4      one <account>.sqlite per pipeline        <- current

The v3 step is handled inside `state.init_schema`, because it is a pure schema
change to a file that is already the right file. This module handles the v4
step, which is different in kind: it *splits one database into several*, and
that has to be deliberate. A pipeline that silently created an empty
`david.sqlite` next to a `state.sqlite` full of David's rows would re-download
the entire library, so nothing here ever runs by itself -- `sync.py migrate`
does, after printing the plan.

Adding a future layout change means adding a detector and a step kind. The
plan/apply split, the backup and the refusal to guess all come for free.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import log, state

# Every table carrying an `account` column, and therefore everything a split
# has to move. Kept next to the schema it mirrors.
ACCOUNT_TABLES = ("assets", "runs", "staged_deletes", "deletions", "jobs")


class MigrationError(Exception):
    pass


@dataclass
class Step:
    """One thing that needs doing, in human terms."""
    kind: str                      # split | schema | adopt
    account: str
    source: Path | None = None
    dest: Path | None = None
    rows: int = 0
    detail: str = ""

    def describe(self) -> str:
        if self.kind == "split":
            return (f"split {self.rows} assets for {self.account!r} out of "
                    f"{self.source.name} into {self.dest.name}")
        if self.kind == "adopt":
            return (f"rename {self.source.name} to {self.dest.name} "
                    f"({self.rows} assets for {self.account!r})")
        if self.kind == "schema":
            return f"bring {self.source.name} up to schema v{state.SCHEMA_VERSION}"
        return self.detail or self.kind


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    legacy: Path | None = None
    # Accounts the legacy database holds that the config does not name.
    orphans: list[str] = field(default_factory=list)

    @property
    def needed(self) -> bool:
        return bool(self.steps)


def needs_migration(cfg) -> bool:
    """Cheap check for the startup guard: two stats, no database opened.

    True when the old shared database is still there and at least one
    configured account has no database of its own -- the exact shape that
    would otherwise start an account from scratch.
    """
    try:
        accounts = cfg.accounts
    except Exception:  # noqa: BLE001 - a broken config is reported elsewhere
        return False
    legacy = accounts[0].legacy_db_path
    if not legacy.exists():
        return False
    return any(not a.db_path.exists() for a in accounts)


def _accounts_in(conn: sqlite3.Connection) -> dict[str, int]:
    """account -> asset count, for a database that has an account column."""
    try:
        rows = conn.execute(
            "SELECT account, COUNT(*) c FROM assets GROUP BY account").fetchall()
    except sqlite3.Error:
        return {}
    return {row["account"]: row["c"] for row in rows}


def _is_pre_v3(conn: sqlite3.Connection) -> bool:
    return (state.table_exists(conn, "assets")
            and "account" not in state.table_columns(conn, "assets"))


def plan(cfg, assign_to: str | None = None) -> Plan:
    """What would have to happen to reach the configured layout."""
    accounts = cfg.accounts
    names = [a.account_name for a in accounts]
    result = Plan(legacy=accounts[0].legacy_db_path)

    have = {a.account_name: a.db_path.exists() for a in accounts}
    legacy = result.legacy

    if not legacy.exists():
        for account in accounts:
            if not have[account.account_name]:
                # Nothing to move: a first run creates it.
                log.debug("migrate.fresh", account=account.account_name)
        return result

    conn = state.connect(legacy)
    pre_v3 = False
    try:
        pre_v3 = _is_pre_v3(conn)
        if pre_v3:
            # No account column at all, so every row is unowned and somebody
            # has to say whose they are. One configured account is not a
            # guess; two is.
            owner = assign_to or (names[0] if len(names) == 1 else None)
            if owner is None:
                raise MigrationError(
                    f"{legacy.name} predates account support, so every row in "
                    f"it needs an owner, and this config names {len(names)} "
                    f"accounts ({', '.join(names)}). Re-run with "
                    f"--assign-to <account>.")
            if owner not in names:
                raise MigrationError(
                    f"--assign-to {owner!r} is not a configured account "
                    f"({', '.join(names)})")
            total = conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"]
            result.steps.append(Step("schema", owner, source=legacy, rows=total,
                                     detail="assign every row to " + owner))
            present = {owner: total}
        else:
            present = _accounts_in(conn)
    finally:
        conn.close()

    for account in accounts:
        name = account.account_name
        rows = present.get(name, 0)
        if have[name]:
            if rows:
                result.warnings.append(
                    f"{account.db_path.name} already exists and {legacy.name} "
                    f"still holds {rows} assets for {name!r}; leaving both "
                    f"alone. Merge them by hand or delete the stale one.")
            continue
        if not rows:
            continue
        # One account and nothing else in the file: a rename is the whole job,
        # and it keeps every row byte-identical without a second copy of a
        # large library's metadata.
        #
        # A pre-v3 file is copied instead, even when it holds one account. The
        # rows have just been rewritten by the schema step, and leaving the
        # original in place is a second safety net beyond the backup.
        kind = "adopt" if (len(present) == 1 and not pre_v3) else "split"
        result.steps.append(Step(kind, name, source=legacy,
                                 dest=account.db_path, rows=rows))

    result.orphans = sorted(set(present) - set(names))
    for orphan in result.orphans:
        result.warnings.append(
            f"{legacy.name} holds {present[orphan]} assets for {orphan!r}, "
            f"which the config does not name. They are left where they are; "
            f"add the account to the config and re-run, or ignore them.")
    return result


def apply(cfg, migration: Plan, backup: bool = True) -> list[str]:
    """Carry out a plan. Returns one line per action, for logging."""
    if not migration.needed:
        return []
    legacy = migration.legacy
    done: list[str] = []

    if backup and legacy and legacy.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        target = legacy.with_name(f"{legacy.name}.pre-split-{stamp}")
        try:
            source = state.connect(legacy)
            try:
                with sqlite3.connect(str(target)) as dest:
                    source.backup(dest)
            finally:
                source.close()
        except (sqlite3.Error, OSError) as exc:
            raise MigrationError(
                f"cannot back up {legacy} to {target}: {exc}; refusing to "
                f"migrate") from exc
        done.append(f"backup={target.name}")

    # The schema step first: everything after it assumes an account column.
    for step in migration.steps:
        if step.kind != "schema":
            continue
        conn = state.connect(step.source)
        try:
            state.init_schema(conn, step.account)
        finally:
            conn.close()
        done.append(f"schema: {step.source.name} -> v{state.SCHEMA_VERSION}, "
                    f"{step.rows} rows assigned to {step.account}")

    # Splits before the adopt, so an adopt never moves a file others need.
    for step in migration.steps:
        if step.kind != "split":
            continue
        moved = split_out(step.source, step.account, step.dest)
        done.append(f"split: {step.account} -> {step.dest.name} "
                    f"({moved.get('assets', 0)} assets)")

    for step in migration.steps:
        if step.kind != "adopt":
            continue
        adopt(step.source, step.dest)
        done.append(f"adopt: {step.source.name} -> {step.dest.name}")

    # Anything left in the shared file is either an orphan account or has
    # already been copied out. Move it aside so the startup guard stops
    # firing, and so it is obvious this file is no longer the live one.
    if legacy and legacy.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        retired = legacy.with_name(f"{legacy.name}.split-{stamp}")
        legacy.replace(retired)
        for suffix in ("-wal", "-shm"):
            stray = legacy.with_name(legacy.name + suffix)
            if stray.exists():
                stray.unlink()
        done.append(f"retired: {legacy.name} -> {retired.name}")
    return done


def split_out(source_path: Path, account: str, dest_path: Path) -> dict[str, int]:
    """Copy one account's rows out of a shared database into its own file.

    ATTACH rather than a row-by-row copy in Python: it is one statement per
    table inside a single transaction, and it preserves the integer primary
    keys of `staged_deletes` and `jobs`, which the UI hands back as row ids.
    """
    if dest_path.exists():
        raise MigrationError(
            f"{dest_path} already exists; refusing to split into it")
    dest = state.connect(dest_path)
    try:
        state.init_schema(dest, account)
    finally:
        dest.close()

    source = state.connect(source_path)
    moved: dict[str, int] = {}
    try:
        source.execute("ATTACH DATABASE ? AS split_dest", (str(dest_path),))
        try:
            for table in ACCOUNT_TABLES:
                if not state.table_exists(source, table):
                    continue
                # Both databases are at SCHEMA_VERSION by now -- the schema
                # step ran first -- so the source's columns are the right set.
                names = ", ".join(state.table_columns(source, table))
                cur = source.execute(
                    f"INSERT INTO split_dest.{table} ({names})"
                    f" SELECT {names} FROM main.{table} WHERE account=?",
                    (account,))
                moved[table] = cur.rowcount
            source.commit()
        finally:
            source.execute("DETACH DATABASE split_dest")
    except sqlite3.Error as exc:
        dest_path.unlink(missing_ok=True)
        raise MigrationError(
            f"could not split {account!r} out of {source_path.name}: {exc}"
        ) from exc
    finally:
        source.close()
    return moved


def adopt(source_path: Path, dest_path: Path) -> None:
    """Rename a shared database that holds exactly one account into its place.

    Cheaper and safer than a copy when there is nothing to leave behind: no
    second copy of a multi-gigabyte library's metadata, and every row keeps
    its rowid. The WAL and SHM files come along so no committed write is lost.
    """
    if dest_path.exists():
        raise MigrationError(
            f"{dest_path} already exists; refusing to rename onto it")
    for suffix in ("", "-wal", "-shm"):
        source = source_path.with_name(source_path.name + suffix)
        if source.exists():
            shutil.move(str(source), str(dest_path.with_name(
                dest_path.name + suffix)))
