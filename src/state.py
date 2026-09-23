"""SQLite state: schema, transitions, resume, the delete queue.

The state DB is a requirement rather than an optimisation -- Proton's fair use
policy means we transfer only what actually changed.

Three conventions worth knowing:

* **Every row is scoped by `account`.** `node_id` is unique within one Proton
  volume, not globally, so `(account, node_id)` is the primary key and every
  query takes the account explicitly. There is deliberately no module-level
  "current account": mixing one account's staging path with another's Immich
  key would upload one person's photos into the other's library.
* `last_attempt` doubles as "time of last state change". The reaper's grace
  period is measured from it, so no extra timestamp column is needed.
* A `failed` row's retry stage is derived, not stored: `sha1 IS NULL` means the
  download never completed, so it retries from download; otherwise the file is
  sitting in ready/ and it retries from upload. `last_error` carries a
  "<stage>: " prefix for humans.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = 3

DISCOVERED = "discovered"
DOWNLOADING = "downloading"
DOWNLOADED = "downloaded"
UPLOADING = "uploading"
UPLOADED = "uploaded"
VERIFIED = "verified"
PURGED = "purged"
FAILED = "failed"
QUARANTINED = "quarantined"
# v3: the delete queue. Reconcile stages, the operator executes.
STAGED_FOR_DELETE = "staged_for_delete"
DELETING = "deleting"
REMOTE_TRASHED = "remote_trashed"
DELETE_FAILED = "delete_failed"

ALL_STATUSES = [
    DISCOVERED, DOWNLOADING, DOWNLOADED, UPLOADING, UPLOADED,
    VERIFIED, PURGED, FAILED, QUARANTINED,
    STAGED_FOR_DELETE, DELETING, REMOTE_TRASHED, DELETE_FAILED,
]

# Statuses the delete queue owns. The puller must never resurrect one: a staged
# row whose reported size or mtime shifts would otherwise be reset to
# `discovered` and downloaded again in the window between staging and
# deletion. This is the one place the two v2 features interact badly.
DELETE_STATUSES = (STAGED_FOR_DELETE, DELETING, REMOTE_TRASHED, DELETE_FAILED)

# Only a row that actually reached Immich can be staged for deletion.
STAGEABLE_STATUSES = (UPLOADED, VERIFIED, PURGED)

# Reset rule for a crashed run: never trust in-flight state. `deleting` rewinds
# to `staged_for_delete` rather than to a terminal state -- the execute path
# re-resolves every node before touching it, so a repeat is safe.
RESUME_MAP = {
    DOWNLOADING: DISCOVERED,
    UPLOADING: DOWNLOADED,
    DELETING: STAGED_FOR_DELETE,
}

# staged_deletes.state -- the queue's own lifecycle, next to the asset status.
STAGED = "staged"
STAGE_DELETING = "deleting"
STAGE_TRASHED = "trashed"
STAGE_FAILED = "failed"
STAGE_CANCELLED = "cancelled"

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_TYPES = ("sync", "reconcile", "delete")

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  account         TEXT NOT NULL,
  node_id         TEXT NOT NULL,
  remote_path     TEXT NOT NULL,
  remote_name     TEXT NOT NULL,
  remote_size     INTEGER,
  remote_modified TEXT,
  local_path      TEXT,
  sha1            TEXT,
  status          TEXT NOT NULL,
  immich_asset_id TEXT,
  is_duplicate    INTEGER DEFAULT 0,
  attempts        INTEGER DEFAULT 0,
  first_seen      TEXT NOT NULL,
  last_attempt    TEXT,
  last_error      TEXT,
  -- v2: from Proton's activeRevision. claimed_sha1 is the uploader's digest
  -- (not server-verified); capture_time is when the photo was actually taken.
  claimed_sha1    TEXT,
  capture_time    TEXT,
  -- v3: the checksum Immich reported at verify time, kept so the UI can show
  -- what was compared without a round trip.
  immich_checksum TEXT,
  PRIMARY KEY (account, node_id)
);

CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(account, status);
CREATE INDEX IF NOT EXISTS idx_assets_sha1 ON assets(sha1);
CREATE INDEX IF NOT EXISTS idx_assets_immich ON assets(immich_asset_id);

CREATE TABLE IF NOT EXISTS runs (
  account    TEXT NOT NULL,
  run_id     TEXT NOT NULL,
  started_at TEXT, finished_at TEXT,
  discovered INTEGER, downloaded INTEGER, uploaded INTEGER,
  failed     INTEGER, exit_code INTEGER,
  PRIMARY KEY (account, run_id)
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(account, started_at DESC);

-- The delete queue. Lives in our own DB from first sighting: Immich empties
-- its trash after ~30 days, and a list recomputed from the server would
-- silently drop anything not acted on before the purge, leaving the file in
-- Proton with nothing to say so.
CREATE TABLE IF NOT EXISTS staged_deletes (
  id              INTEGER PRIMARY KEY,
  account         TEXT NOT NULL,
  node_id         TEXT NOT NULL,
  remote_path     TEXT,
  remote_name     TEXT,
  immich_asset_id TEXT,
  capture_time    TEXT,
  staged_at       TEXT NOT NULL,
  state           TEXT NOT NULL,
  executed_at     TEXT,
  error           TEXT,
  UNIQUE (account, node_id)
);

CREATE INDEX IF NOT EXISTS idx_staged_state ON staged_deletes(account, state);

-- Append-only audit. Never updated, never deleted: this is the record of what
-- the only destructive code path in the pipeline actually did.
CREATE TABLE IF NOT EXISTS deletions (
  id          INTEGER PRIMARY KEY,
  account     TEXT NOT NULL,
  node_id     TEXT NOT NULL,
  remote_path TEXT,
  staged_at   TEXT,
  executed_at TEXT NOT NULL,
  result      TEXT NOT NULL,
  error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_deletions_account ON deletions(account, executed_at DESC);

-- Web UI job queue. A request never runs a sync inline: a long download would
-- time out the handler, and a page refresh would start a second one.
CREATE TABLE IF NOT EXISTS jobs (
  id          INTEGER PRIMARY KEY,
  account     TEXT NOT NULL,
  type        TEXT NOT NULL,
  state       TEXT NOT NULL,
  payload     TEXT,
  created_at  TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT,
  exit_code   INTEGER,
  detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, id);
CREATE INDEX IF NOT EXISTS idx_jobs_account ON jobs(account, id DESC);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """For the web UI: never block, never write.

    WAL plus mode=ro means a reader sees a consistent snapshot while a sync is
    mid-write, and an accidental UPDATE from a request handler fails loudly
    instead of contending with the pipeline for the write lock.
    """
    path = Path(db_path)
    uri = f"file:{urlquote(str(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA query_only=ON")
    return conn


def urlquote(text: str) -> str:
    from urllib.parse import quote
    return quote(text)


# --------------------------------------------------------------------------
# schema / migration
# --------------------------------------------------------------------------

def db_file(conn: sqlite3.Connection) -> Path | None:
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main" and row["file"]:
            return Path(row["file"])
    return None


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


class MigrationError(Exception):
    pass


def backup_db(conn: sqlite3.Connection, suffix: str) -> Path | None:
    """Snapshot the database next to itself before a destructive migration.

    sqlite3's own backup API rather than a file copy: it takes a consistent
    snapshot including anything still sitting in the WAL.
    """
    source = db_file(conn)
    if source is None:  # :memory:
        return None
    target = source.with_name(f"{source.name}.{suffix}")
    if target.exists():
        target.unlink()
    try:
        with sqlite3.connect(str(target)) as dest:
            conn.backup(dest)
    except (sqlite3.Error, OSError) as exc:
        raise MigrationError(
            f"cannot back up {source} to {target}: {exc}; refusing to migrate"
        ) from exc
    return target


def init_schema(conn: sqlite3.Connection, account: str | None = None) -> list[str]:
    """Bring the database to SCHEMA_VERSION. Returns what the migration did."""
    notes = migrate(conn, account)
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return notes


def migrate(conn: sqlite3.Connection, account: str | None = None) -> list[str]:
    """Rebuild pre-v3 tables so every row is scoped by account.

    SQLite cannot alter a primary key, so `(account, node_id)` means the
    rename-create-copy dance rather than an ADD COLUMN. Runs before the
    CREATE TABLE IF NOT EXISTS statements, which would otherwise try to add
    v3 indexes to a v2 table and fail on the missing column.

    The old tables are kept as `assets_v2` / `runs_v2`. They are the rollback,
    and they are cheap -- a v2 database that has already been migrated is
    detected by the presence of the `account` column, not by their absence.
    """
    notes: list[str] = []
    needs_assets = table_exists(conn, "assets") and \
        "account" not in table_columns(conn, "assets")
    needs_runs = table_exists(conn, "runs") and \
        "account" not in table_columns(conn, "runs")
    if not (needs_assets or needs_runs):
        return notes

    if not account:
        raise MigrationError(
            "this database predates multi-account support and every row needs "
            "an owner; set account.name in the config and re-run")

    backup = backup_db(conn, f"pre-v3-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}")
    if backup:
        notes.append(f"backup={backup.name}")

    if needs_assets:
        _rebuild(conn, "assets", "assets_v2", account,
                 ("idx_assets_status", "idx_assets_sha1", "idx_assets_immich"))
        notes.append("assets")
    if needs_runs:
        _rebuild(conn, "runs", "runs_v2", account, ("idx_runs_started",))
        notes.append("runs")
    conn.commit()
    return notes


# The v3 shapes, as CREATE statements the rebuild can run on their own. Kept
# in step with SCHEMA above; _rebuild only ever copies columns present in both.
_V3_TABLES = {
    "assets": """
        CREATE TABLE assets (
          account TEXT NOT NULL, node_id TEXT NOT NULL,
          remote_path TEXT NOT NULL, remote_name TEXT NOT NULL,
          remote_size INTEGER, remote_modified TEXT, local_path TEXT,
          sha1 TEXT, status TEXT NOT NULL, immich_asset_id TEXT,
          is_duplicate INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
          first_seen TEXT NOT NULL, last_attempt TEXT, last_error TEXT,
          claimed_sha1 TEXT, capture_time TEXT, immich_checksum TEXT,
          PRIMARY KEY (account, node_id))
    """,
    "runs": """
        CREATE TABLE runs (
          account TEXT NOT NULL, run_id TEXT NOT NULL,
          started_at TEXT, finished_at TEXT, discovered INTEGER,
          downloaded INTEGER, uploaded INTEGER, failed INTEGER,
          exit_code INTEGER,
          PRIMARY KEY (account, run_id))
    """,
}


def _rebuild(conn: sqlite3.Connection, table: str, archive: str, account: str,
             indexes: Sequence[str]) -> None:
    old_columns = table_columns(conn, table)
    # Indexes follow a renamed table, so a later CREATE INDEX with the same
    # name would collide. Drop them; SCHEMA recreates them on the new table.
    for index in indexes:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    if table_exists(conn, archive):
        conn.execute(f"DROP TABLE {archive}")
    conn.execute(f"ALTER TABLE {table} RENAME TO {archive}")
    conn.execute(_V3_TABLES[table])

    new_columns = table_columns(conn, table)
    carried = [c for c in old_columns if c in new_columns and c != "account"]
    columns = ", ".join(["account", *carried])
    selects = ", ".join(["?", *carried])
    conn.execute(
        f"INSERT INTO {table} ({columns}) SELECT {selects} FROM {archive}",
        (account,),
    )


def resume(conn: sqlite3.Connection, account: str) -> dict[str, int]:
    """Reset every in-flight row to its previous stable state."""
    reset: dict[str, int] = {}
    for unstable, stable in RESUME_MAP.items():
        if unstable == DOWNLOADING:
            cur = conn.execute(
                "UPDATE assets SET status=?, local_path=NULL, last_attempt=? "
                "WHERE account=? AND status=?",
                (stable, utcnow(), account, unstable),
            )
        else:
            cur = conn.execute(
                "UPDATE assets SET status=?, last_attempt=? "
                "WHERE account=? AND status=?",
                (stable, utcnow(), account, unstable),
            )
        if cur.rowcount:
            reset[unstable] = cur.rowcount
    # A row left mid-trash by a killed run goes back on the queue too.
    conn.execute(
        "UPDATE staged_deletes SET state=? WHERE account=? AND state=?",
        (STAGED, account, STAGE_DELETING),
    )
    conn.commit()
    return reset


def rewind(conn: sqlite3.Connection, account: str,
           node_ids: Iterable[str]) -> int:
    """Put named in-flight rows back to their previous stable state.

    Exactly what resume() would do on the next run, applied eagerly. A pass
    that stops on purpose -- the circuit breaker tripping -- should not leave
    rows looking in-flight until something else tidies up after it. No attempt
    is charged: these were never tried.
    """
    ids = [str(n) for n in node_ids]
    if not ids:
        return 0
    now = utcnow()
    changed = 0
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"UPDATE assets SET"
            f"  status=CASE status WHEN ? THEN ? WHEN ? THEN ? ELSE status END,"
            f"  local_path=CASE status WHEN ? THEN NULL ELSE local_path END,"
            f"  last_attempt=?"
            f" WHERE account=? AND node_id IN ({placeholders})"
            f"   AND status IN (?,?)",
            (DOWNLOADING, DISCOVERED, UPLOADING, DOWNLOADED,
             DOWNLOADING, now, account, *chunk, DOWNLOADING, UPLOADING),
        )
        changed += cur.rowcount
    conn.commit()
    return changed


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def upsert_discovered(
    conn: sqlite3.Connection,
    account: str,
    node_id: str,
    remote_path: str,
    remote_name: str,
    remote_size: int | None,
    remote_modified: str | None,
    now: str | None = None,
    claimed_sha1: str | None = None,
    capture_time: str | None = None,
) -> str:
    """Insert or refresh one node.

    Returns 'new', 'changed', 'unchanged' or 'staged' -- the last meaning the
    row belongs to the delete queue and was deliberately left alone.
    """
    now = now or utcnow()
    row = conn.execute(
        "SELECT * FROM assets WHERE account=? AND node_id=?", (account, node_id)
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO assets (account, node_id, remote_path, remote_name,"
            " remote_size, remote_modified, status, first_seen, last_attempt,"
            " claimed_sha1, capture_time) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (account, node_id, remote_path, remote_name, remote_size,
             remote_modified, DISCOVERED, now, now, claimed_sha1, capture_time),
        )
        return "new"

    if row["status"] in DELETE_STATUSES:
        # Awaiting or past deletion in Proton. Re-downloading it is exactly
        # what the operator asked us not to do, so nothing here is touched --
        # not even the path, which the execute path compares against.
        return "staged"

    content_changed = (
        (row["remote_size"] or 0) != (remote_size or 0)
        or (row["remote_modified"] or "") != (remote_modified or "")
    )

    if content_changed:
        # The bytes moved under us: start this node over.
        conn.execute(
            "UPDATE assets SET remote_path=?, remote_name=?, remote_size=?,"
            " remote_modified=?, local_path=NULL, sha1=NULL, status=?,"
            " immich_asset_id=NULL, immich_checksum=NULL, is_duplicate=0,"
            " attempts=0, last_attempt=?, last_error=NULL, claimed_sha1=?,"
            " capture_time=? WHERE account=? AND node_id=?",
            (remote_path, remote_name, remote_size, remote_modified,
             DISCOVERED, now, claimed_sha1, capture_time, account, node_id),
        )
        return "changed"

    if row["remote_path"] != remote_path or row["remote_name"] != remote_name:
        # A rename is metadata only -- node_id is stable, the bytes are not new.
        conn.execute(
            "UPDATE assets SET remote_path=?, remote_name=?"
            " WHERE account=? AND node_id=?",
            (remote_path, remote_name, account, node_id),
        )
    return "unchanged"


# --------------------------------------------------------------------------
# work selection
# --------------------------------------------------------------------------

def backoff_ready(
    attempts: int,
    last_attempt: str | None,
    base_sec: int,
    cap_sec: int,
    now: datetime | None = None,
) -> bool:
    """Exponential backoff: base * 2^(attempts-1), capped."""
    if attempts <= 0:
        return True
    ts = parse_ts(last_attempt)
    if ts is None:
        return True
    delay = min(base_sec * (2 ** (attempts - 1)), cap_sec)
    now = now or datetime.now(timezone.utc)
    return now >= ts + timedelta(seconds=delay)


def _eligible_failed(
    rows: Iterable[sqlite3.Row],
    stage: str,
    base_sec: int,
    cap_sec: int,
    now: datetime | None = None,
) -> list[sqlite3.Row]:
    out = []
    for row in rows:
        if row["status"] == FAILED:
            wants_download = row["sha1"] is None
            if (stage == "download") != wants_download:
                continue
            if not backoff_ready(row["attempts"], row["last_attempt"],
                                 base_sec, cap_sec, now):
                continue
        out.append(row)
    return out


def select_for_download(
    conn: sqlite3.Connection,
    account: str,
    limit: int | None = None,
    max_bytes: int | None = None,
    max_attempts: int = 5,
    backoff_base_sec: int = 300,
    backoff_cap_sec: int = 86400,
    now: datetime | None = None,
) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM assets WHERE account=? AND (status=?"
        " OR (status=? AND sha1 IS NULL AND attempts<?))"
        " ORDER BY remote_modified IS NULL, remote_modified, node_id",
        (account, DISCOVERED, FAILED, max_attempts),
    ).fetchall()
    rows = _eligible_failed(rows, "download", backoff_base_sec, backoff_cap_sec, now)

    picked: list[sqlite3.Row] = []
    total = 0
    for row in rows:
        if limit is not None and len(picked) >= limit:
            break
        size = row["remote_size"] or 0
        if max_bytes is not None and picked and total + size > max_bytes:
            continue
        picked.append(row)
        total += size
    return picked


def select_for_upload(
    conn: sqlite3.Connection,
    account: str,
    limit: int | None = None,
    max_attempts: int = 5,
    backoff_base_sec: int = 300,
    backoff_cap_sec: int = 86400,
    now: datetime | None = None,
) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM assets WHERE account=? AND (status=?"
        " OR (status=? AND sha1 IS NOT NULL AND local_path IS NOT NULL AND attempts<?))"
        " ORDER BY last_attempt IS NULL, last_attempt, node_id",
        (account, DOWNLOADED, FAILED, max_attempts),
    ).fetchall()
    rows = _eligible_failed(rows, "upload", backoff_base_sec, backoff_cap_sec, now)
    return rows[:limit] if limit is not None else rows


def select_for_precheck(
    conn: sqlite3.Connection,
    account: str,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Discovered rows carrying a claimed digest, so Immich can be asked
    whether the file is already there before a byte is transferred."""
    rows = conn.execute(
        "SELECT * FROM assets WHERE account=? AND status=?"
        " AND claimed_sha1 IS NOT NULL ORDER BY node_id",
        (account, DISCOVERED),
    ).fetchall()
    return rows[:limit] if limit is not None else rows


def select_for_verify(conn: sqlite3.Connection, account: str,
                      limit: int | None = None) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM assets WHERE account=? AND status=? ORDER BY last_attempt",
        (account, UPLOADED),
    ).fetchall()
    return rows[:limit] if limit is not None else rows


def select_for_reap(
    conn: sqlite3.Connection,
    account: str,
    keep_days: int = 7,
    now: datetime | None = None,
) -> list[sqlite3.Row]:
    """Verified rows whose grace period has elapsed and still hold a file."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=keep_days)
    rows = conn.execute(
        "SELECT * FROM assets WHERE account=? AND status=? AND local_path IS NOT NULL",
        (account, VERIFIED),
    ).fetchall()
    if keep_days <= 0:
        return list(rows)
    out = []
    for row in rows:
        ts = parse_ts(row["last_attempt"])
        if ts is None or ts <= cutoff:
            out.append(row)
    return out


def select_verified_without_file(conn: sqlite3.Connection,
                                 account: str) -> list[sqlite3.Row]:
    """Verified rows that never had a local file -- matched by claimed digest
    and so never downloaded. There is nothing to delete, only to close out."""
    return conn.execute(
        "SELECT * FROM assets WHERE account=? AND status=? AND local_path IS NULL",
        (account, VERIFIED),
    ).fetchall()


def get(conn: sqlite3.Connection, account: str, node_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM assets WHERE account=? AND node_id=?", (account, node_id)
    ).fetchone()


def find_by_asset_ids(conn: sqlite3.Connection, account: str,
                      asset_ids: Sequence[str]) -> dict[str, sqlite3.Row]:
    """immich_asset_id -> row, for the rows this account owns.

    Chunked: reconcile can hand over every trashed asset in the library, and
    SQLite caps a statement at 999 bound parameters by default.
    """
    out: dict[str, sqlite3.Row] = {}
    ids = [str(a) for a in asset_ids if a]
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM assets WHERE account=? AND immich_asset_id IN ({placeholders})",
            (account, *chunk),
        ).fetchall()
        for row in rows:
            out[str(row["immich_asset_id"])] = row
    return out


# --------------------------------------------------------------------------
# transitions
# --------------------------------------------------------------------------

def _set_status(conn: sqlite3.Connection, account: str, node_id: str,
                status: str, **cols: Any) -> None:
    cols.setdefault("last_attempt", utcnow())
    assignments = ", ".join(f"{k}=?" for k in cols)
    conn.execute(
        f"UPDATE assets SET status=?, {assignments} WHERE account=? AND node_id=?",
        (status, *cols.values(), account, node_id),
    )


def mark_downloading(conn: sqlite3.Connection, account: str, node_id: str) -> None:
    _set_status(conn, account, node_id, DOWNLOADING)
    conn.commit()


def mark_downloaded(conn: sqlite3.Connection, account: str, node_id: str,
                    local_path: str, sha1: str) -> None:
    _set_status(conn, account, node_id, DOWNLOADED, local_path=local_path,
                sha1=sha1, last_error=None)
    conn.commit()


def mark_uploading(conn: sqlite3.Connection, account: str, node_id: str) -> None:
    _set_status(conn, account, node_id, UPLOADING)
    conn.commit()


def mark_uploaded(
    conn: sqlite3.Connection,
    account: str,
    node_id: str,
    asset_id: str | None,
    is_duplicate: bool = False,
    sha1: str | None = None,
) -> None:
    extra: dict[str, Any] = {}
    if sha1 is not None:
        # Set when the row was matched on Proton's claimed digest without ever
        # being downloaded: it is the checksum Immich matched, so verify can
        # use it. local_path stays NULL, which is what marks it as never-fetched.
        extra["sha1"] = sha1
    _set_status(conn, account, node_id, UPLOADED, immich_asset_id=asset_id,
                is_duplicate=1 if is_duplicate else 0, last_error=None, **extra)
    conn.commit()


def mark_verified(conn: sqlite3.Connection, account: str, node_id: str,
                  immich_checksum: str | None = None) -> None:
    _set_status(conn, account, node_id, VERIFIED, attempts=0, last_error=None,
                immich_checksum=immich_checksum)
    conn.commit()


def mark_purged(conn: sqlite3.Connection, account: str, node_id: str) -> None:
    _set_status(conn, account, node_id, PURGED, local_path=None)
    conn.commit()


def mark_failed(
    conn: sqlite3.Connection,
    account: str,
    node_id: str,
    stage: str,
    error: str,
    max_attempts: int = 5,
) -> str:
    """attempts++, then failed or quarantined. Returns the resulting status."""
    row = get(conn, account, node_id)
    attempts = (row["attempts"] if row else 0) + 1
    status = QUARANTINED if attempts >= max_attempts else FAILED
    message = f"{stage}: {error}"[:2000]
    _set_status(conn, account, node_id, status, attempts=attempts,
                last_error=message)
    conn.commit()
    return status


def requeue(conn: sqlite3.Connection, account: str,
            node_ids: Iterable[str] | None = None) -> int:
    """Put quarantined/failed rows back in play (operator escape hatch).

    Never touches the delete queue: requeueing a staged row would re-download
    the very file someone asked to have removed.
    """
    now = utcnow()
    if node_ids:
        ids = list(node_ids)
        placeholders = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE assets SET status=CASE WHEN sha1 IS NULL THEN ? ELSE ? END,"
            f" attempts=0, last_error=NULL, last_attempt=?"
            f" WHERE account=? AND node_id IN ({placeholders})"
            f" AND status NOT IN ({','.join('?' * len(DELETE_STATUSES))})",
            (DISCOVERED, DOWNLOADED, now, account, *ids, *DELETE_STATUSES),
        )
    else:
        cur = conn.execute(
            "UPDATE assets SET status=CASE WHEN sha1 IS NULL THEN ? ELSE ? END,"
            " attempts=0, last_error=NULL, last_attempt=?"
            " WHERE account=? AND status IN (?,?)",
            (DISCOVERED, DOWNLOADED, now, account, FAILED, QUARANTINED),
        )
    conn.commit()
    return cur.rowcount


# --------------------------------------------------------------------------
# the delete queue
# --------------------------------------------------------------------------

def stage_delete(conn: sqlite3.Connection, account: str, row: sqlite3.Row,
                 now: str | None = None) -> bool:
    """Record one asset as trashed in Immich and therefore a deletion candidate.

    Idempotent, and never a mutation on Proton's side -- reconcile only ever
    adds to this list. Returns True when the row was newly staged.
    """
    if row["status"] in DELETE_STATUSES:
        return False
    if row["status"] not in STAGEABLE_STATUSES:
        # Still in flight. Staging it now would race the uploader.
        return False
    now = now or utcnow()
    # A row unstaged earlier and still in Immich's trash is staged again --
    # that is the documented behaviour ("restore it in Immich too, or the next
    # sync will simply stage it again"). Without the DO UPDATE the asset would
    # go to `staged_for_delete` while its queue row stayed `cancelled`, which
    # is invisible in the UI and terminal for the puller: a stuck photo.
    #
    # `trashed` and `failed` rows never reach here -- their asset status is
    # already in DELETE_STATUSES and returned above -- so a genuine failure
    # stays visible instead of being quietly reset every night.
    cur = conn.execute(
        "INSERT INTO staged_deletes (account, node_id, remote_path, remote_name,"
        " immich_asset_id, capture_time, staged_at, state)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(account, node_id) DO UPDATE SET"
        "   state=excluded.state, staged_at=excluded.staged_at,"
        "   remote_path=excluded.remote_path, remote_name=excluded.remote_name,"
        "   immich_asset_id=excluded.immich_asset_id,"
        "   capture_time=excluded.capture_time, executed_at=NULL, error=NULL"
        " WHERE staged_deletes.state=?",
        (account, row["node_id"], row["remote_path"], row["remote_name"],
         row["immich_asset_id"], row["capture_time"], now, STAGED,
         STAGE_CANCELLED),
    )
    _set_status(conn, account, row["node_id"], STAGED_FOR_DELETE, last_attempt=now)
    conn.commit()
    return cur.rowcount > 0


def mark_deleting(conn: sqlite3.Connection, account: str, node_id: str,
                  staged_id: int) -> None:
    """In flight towards Proton's trash. resume() rewinds this on a crash."""
    _set_status(conn, account, node_id, DELETING)
    conn.execute("UPDATE staged_deletes SET state=? WHERE id=?",
                 (STAGE_DELETING, staged_id))
    conn.commit()


def mark_remote_trashed(conn: sqlite3.Connection, account: str, row: sqlite3.Row,
                        result: str, now: str | None = None) -> None:
    """Terminal success: the file is in Proton's trash (or was already gone).

    Writes the asset status, the queue row and the audit row together, so the
    three can never disagree about what happened.
    """
    now = now or utcnow()
    _set_status(conn, account, row["node_id"], REMOTE_TRASHED, last_attempt=now)
    conn.execute(
        "UPDATE staged_deletes SET state=?, executed_at=?, error=NULL WHERE id=?",
        (STAGE_TRASHED, now, int(row["id"])))
    conn.commit()
    record_deletion(conn, account, row["node_id"], row["remote_path"],
                    row["staged_at"], result, executed_at=now)


def mark_delete_failed(conn: sqlite3.Connection, account: str, row: sqlite3.Row,
                       error: str, now: str | None = None) -> None:
    """Terminal failure, kept visible rather than dropped: the row stays in
    the UI with its reason, and the attempt is audited."""
    now = now or utcnow()
    _set_status(conn, account, row["node_id"], DELETE_FAILED, last_attempt=now,
                last_error=f"delete: {error}"[:2000])
    conn.execute(
        "UPDATE staged_deletes SET state=?, executed_at=?, error=? WHERE id=?",
        (STAGE_FAILED, now, error[:2000], int(row["id"])))
    conn.commit()
    record_deletion(conn, account, row["node_id"], row["remote_path"],
                    row["staged_at"], "failed", error=error, executed_at=now)


def unstage(conn: sqlite3.Connection, account: str, ids: Sequence[int],
            resync: bool = False) -> int:
    """Take rows back off the delete queue.

    Two different situations, so two outcomes:

    * default -- the asset goes back to `purged`. This is the escape hatch for
      a photo trashed in Immich by accident: restore it there, unstage here,
      and the next pull treats it as an ordinary completed asset rather than
      downloading it all over again.

    * `resync=True` -- the asset goes back to `discovered`, so the pipeline
      fetches and pushes it from scratch. This is the recovery path for rows
      that were never really uploaded: Immich recognised their checksum
      against an asset already in its trash, recorded a duplicate, and
      reconcile staged the Proton original. Sending those back to `purged`
      would strand them, because `purged` is terminal for the puller and
      nothing would ever retry them.
    """
    rows = get_staged(conn, account, ids)
    count = 0
    now = utcnow()
    for row in rows:
        if row["state"] not in (STAGED, STAGE_FAILED):
            continue
        conn.execute("UPDATE staged_deletes SET state=? WHERE id=?",
                     (STAGE_CANCELLED, int(row["id"])))
        if resync:
            # Clear everything the previous pass concluded: the local file is
            # long reaped, and the asset id points at whatever Immich matched.
            _set_status(conn, account, row["node_id"], DISCOVERED,
                        local_path=None, sha1=None, immich_asset_id=None,
                        immich_checksum=None, is_duplicate=0, attempts=0,
                        last_error=None, last_attempt=now)
        else:
            _set_status(conn, account, row["node_id"], PURGED, local_path=None)
        count += 1
    conn.commit()
    return count


def staged_deletes(conn: sqlite3.Connection, account: str,
                   states: Sequence[str] = (STAGED,),
                   limit: int | None = None) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(states))
    sql = (f"SELECT * FROM staged_deletes WHERE account=?"
           f" AND state IN ({placeholders}) ORDER BY staged_at, id")
    params: list[Any] = [account, *states]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_staged(conn: sqlite3.Connection, account: str,
               ids: Sequence[int]) -> list[sqlite3.Row]:
    """Rows by id, scoped to the account.

    The execute path resolves what to trash from here and nowhere else: a
    request body supplies ids, never paths.
    """
    wanted = [int(i) for i in ids]
    if not wanted:
        return []
    out: list[sqlite3.Row] = []
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        out += conn.execute(
            f"SELECT * FROM staged_deletes WHERE account=? AND id IN ({placeholders})"
            f" ORDER BY id",
            (account, *chunk),
        ).fetchall()
    return out


def mark_staged_state(conn: sqlite3.Connection, staged_id: int, staged_state: str,
                      error: str | None = None,
                      executed_at: str | None = None) -> None:
    conn.execute(
        "UPDATE staged_deletes SET state=?, error=?, executed_at=COALESCE(?, executed_at)"
        " WHERE id=?",
        (staged_state, error[:2000] if error else None, executed_at, staged_id),
    )
    conn.commit()


def record_deletion(conn: sqlite3.Connection, account: str, node_id: str,
                    remote_path: str | None, staged_at: str | None,
                    result: str, error: str | None = None,
                    executed_at: str | None = None) -> None:
    conn.execute(
        "INSERT INTO deletions (account, node_id, remote_path, staged_at,"
        " executed_at, result, error) VALUES (?,?,?,?,?,?,?)",
        (account, node_id, remote_path, staged_at, executed_at or utcnow(),
         result, error[:2000] if error else None),
    )
    conn.commit()


def deletions(conn: sqlite3.Connection, account: str,
              limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM deletions WHERE account=? ORDER BY executed_at DESC, id DESC"
        " LIMIT ?",
        (account, limit),
    ).fetchall()


def count_staged(conn: sqlite3.Connection, account: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM staged_deletes WHERE account=? AND state=?",
        (account, STAGED),
    ).fetchone()
    return row["c"]


# --------------------------------------------------------------------------
# jobs (web UI)
# --------------------------------------------------------------------------

def create_job(conn: sqlite3.Connection, account: str, job_type: str,
               payload: dict[str, Any] | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (account, type, state, payload, created_at)"
        " VALUES (?,?,?,?,?)",
        (account, job_type, JOB_QUEUED,
         json.dumps(payload) if payload else None, utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def active_job(conn: sqlite3.Connection, account: str) -> sqlite3.Row | None:
    """A queued or running job for this account, if any.

    The UI's "a second click while running is rejected" rule is enforced here
    rather than in the handler, so the systemd and subprocess runners behave
    the same way.
    """
    return conn.execute(
        "SELECT * FROM jobs WHERE account=? AND state IN (?,?) ORDER BY id LIMIT 1",
        (account, JOB_QUEUED, JOB_RUNNING),
    ).fetchone()


def recent_jobs(conn: sqlite3.Connection, account: str | None = None,
                limit: int = 20) -> list[sqlite3.Row]:
    if account:
        return conn.execute(
            "SELECT * FROM jobs WHERE account=? ORDER BY id DESC LIMIT ?",
            (account, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def claim_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Take the oldest queued job, atomically.

    The UPDATE's WHERE clause is the lock: two workers racing for the same row
    means one of them updates zero rows and loops.
    """
    while True:
        row = conn.execute(
            "SELECT * FROM jobs WHERE state=? ORDER BY id LIMIT 1", (JOB_QUEUED,)
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE jobs SET state=?, started_at=? WHERE id=? AND state=?",
            (JOB_RUNNING, utcnow(), row["id"], JOB_QUEUED),
        )
        conn.commit()
        if cur.rowcount:
            return get_job(conn, int(row["id"]))


def finish_job(conn: sqlite3.Connection, job_id: int, exit_code: int,
               detail: str | None = None) -> None:
    conn.execute(
        "UPDATE jobs SET state=?, finished_at=?, exit_code=?, detail=? WHERE id=?",
        (JOB_DONE if exit_code in (0, 1) else JOB_FAILED, utcnow(), exit_code,
         detail[:4000] if detail else None, job_id),
    )
    conn.commit()


def release_stale_jobs(conn: sqlite3.Connection) -> int:
    """Fail anything left `running` by a killed server. Called at startup:
    without it the UI would refuse every new job for that account forever."""
    cur = conn.execute(
        "UPDATE jobs SET state=?, finished_at=?, detail=? WHERE state IN (?,?)",
        (JOB_FAILED, utcnow(), "abandoned: the server restarted",
         JOB_RUNNING, JOB_QUEUED),
    )
    conn.commit()
    return cur.rowcount


# --------------------------------------------------------------------------
# runs and counts
# --------------------------------------------------------------------------

def start_run(conn: sqlite3.Connection, account: str, run_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO runs (account, run_id, started_at, discovered,"
        " downloaded, uploaded, failed, exit_code) VALUES (?,?,?,0,0,0,0,NULL)",
        (account, run_id, utcnow()),
    )
    conn.commit()


def finish_run(
    conn: sqlite3.Connection,
    account: str,
    run_id: str,
    discovered: int,
    downloaded: int,
    uploaded: int,
    failed: int,
    exit_code: int,
) -> None:
    conn.execute(
        "INSERT INTO runs (account, run_id, started_at, finished_at, discovered,"
        " downloaded, uploaded, failed, exit_code) VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(account, run_id) DO UPDATE SET finished_at=excluded.finished_at,"
        " discovered=excluded.discovered, downloaded=excluded.downloaded,"
        " uploaded=excluded.uploaded, failed=excluded.failed,"
        " exit_code=excluded.exit_code",
        (account, run_id, utcnow(), utcnow(), discovered, downloaded, uploaded,
         failed, exit_code),
    )
    conn.commit()


def counts(conn: sqlite3.Connection, account: str) -> dict[str, int]:
    out = {s: 0 for s in ALL_STATUSES}
    for row in conn.execute(
        "SELECT status, COUNT(*) c FROM assets WHERE account=? GROUP BY status",
        (account,),
    ):
        out[row["status"]] = row["c"]
    out["total"] = sum(out[s] for s in ALL_STATUSES)
    return out


def backlog(conn: sqlite3.Connection, account: str) -> int:
    """Everything seen but not yet safely in Immich."""
    row = conn.execute(
        "SELECT COUNT(*) c FROM assets WHERE account=? AND status IN (?,?,?,?,?)",
        (account, DISCOVERED, DOWNLOADING, DOWNLOADED, UPLOADING, FAILED),
    ).fetchone()
    return row["c"]


def uploaded_total(conn: sqlite3.Connection, account: str) -> int:
    """Everything that reached Immich and stayed there, across all runs."""
    row = conn.execute(
        "SELECT COUNT(*) c FROM assets WHERE account=? AND status IN (?,?,?)",
        (account, UPLOADED, VERIFIED, PURGED),
    ).fetchone()
    return row["c"]


def last_run(conn: sqlite3.Connection, account: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE account=? ORDER BY started_at DESC LIMIT 1",
        (account,),
    ).fetchone()


def last_success(conn: sqlite3.Connection, account: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE account=? AND exit_code=0"
        " ORDER BY started_at DESC LIMIT 1",
        (account,),
    ).fetchone()


def recent_runs(conn: sqlite3.Connection, account: str,
                limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs WHERE account=? ORDER BY started_at DESC LIMIT ?",
        (account, limit),
    ).fetchall()


PROBLEM_STATUSES = (FAILED, QUARANTINED, DELETE_FAILED)


def recent_problems(conn: sqlite3.Connection, account: str,
                    limit: int = 50) -> list[sqlite3.Row]:
    """Assets carrying an error, newest first.

    `last_error` is written for every failure and, until now, was readable
    only by opening the database. A hundred failures are almost always one
    cause repeated, which is what `problem_summary` is for -- this is the
    detail behind it.
    """
    placeholders = ",".join("?" * len(PROBLEM_STATUSES))
    return conn.execute(
        f"SELECT node_id, remote_path, remote_name, status, attempts,"
        f" last_attempt, last_error FROM assets"
        f" WHERE account=? AND status IN ({placeholders})"
        f" ORDER BY last_attempt DESC, node_id LIMIT ?",
        (account, *PROBLEM_STATUSES, limit),
    ).fetchall()


def problem_summary(conn: sqlite3.Connection, account: str,
                    limit: int = 10) -> list[dict[str, Any]]:
    """The distinct errors and how many assets each hit.

    Grouped on the message with the varying tail cut off, because "126 failed"
    is not actionable and "126 x size mismatch" is. Grouping happens in SQL on
    a prefix so one bad night does not pull 25k rows into memory.
    """
    placeholders = ",".join("?" * len(PROBLEM_STATUSES))
    rows = conn.execute(
        f"SELECT substr(last_error, 1, 120) AS reason, status,"
        f"       COUNT(*) AS assets, MAX(last_attempt) AS latest"
        f"  FROM assets"
        f" WHERE account=? AND status IN ({placeholders})"
        f"   AND last_error IS NOT NULL"
        f" GROUP BY reason, status"
        f" ORDER BY assets DESC LIMIT ?",
        (account, *PROBLEM_STATUSES, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def accounts(conn: sqlite3.Connection) -> list[str]:
    """Accounts the database has actually seen. The config is authoritative;
    this is for spotting rows left behind by a renamed account."""
    return [row["account"] for row in conn.execute(
        "SELECT DISTINCT account FROM assets ORDER BY account")]
