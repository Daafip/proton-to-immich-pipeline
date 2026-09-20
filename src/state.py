"""SQLite state: schema, transitions, resume.

The state DB is a requirement rather than an optimisation -- Proton's fair use
policy means we transfer only what actually changed.

Schema is the one from the build plan. Two conventions worth knowing:

* `last_attempt` doubles as "time of last state change". The reaper's grace
  period is measured from it, so no extra timestamp column is needed.
* A `failed` row's retry stage is derived, not stored: `sha1 IS NULL` means the
  download never completed, so it retries from download; otherwise the file is
  sitting in ready/ and it retries from upload. `last_error` carries a
  "<stage>: " prefix for humans.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2

DISCOVERED = "discovered"
DOWNLOADING = "downloading"
DOWNLOADED = "downloaded"
UPLOADING = "uploading"
UPLOADED = "uploaded"
VERIFIED = "verified"
PURGED = "purged"
FAILED = "failed"
QUARANTINED = "quarantined"

ALL_STATUSES = [
    DISCOVERED, DOWNLOADING, DOWNLOADED, UPLOADING, UPLOADED,
    VERIFIED, PURGED, FAILED, QUARANTINED,
]

# Reset rule for a crashed run: never trust in-flight state.
RESUME_MAP = {DOWNLOADING: DISCOVERED, UPLOADING: DOWNLOADED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  node_id         TEXT PRIMARY KEY,
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
  capture_time    TEXT
);

CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_sha1 ON assets(sha1);

CREATE TABLE IF NOT EXISTS runs (
  run_id     TEXT PRIMARY KEY,
  started_at TEXT, finished_at TEXT,
  discovered INTEGER, downloaded INTEGER, uploaded INTEGER,
  failed     INTEGER, exit_code INTEGER
);
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


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add columns a database created by an older version is missing."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(assets)")}
    added = []
    for column, ddl in (("claimed_sha1", "TEXT"), ("capture_time", "TEXT")):
        if column not in existing:
            conn.execute(f"ALTER TABLE assets ADD COLUMN {column} {ddl}")
            added.append(column)
    if added:
        conn.commit()
    return added


def resume(conn: sqlite3.Connection) -> dict[str, int]:
    """Reset every in-flight row to its previous stable state."""
    reset: dict[str, int] = {}
    for unstable, stable in RESUME_MAP.items():
        if unstable == DOWNLOADING:
            cur = conn.execute(
                "UPDATE assets SET status=?, local_path=NULL, last_attempt=? "
                "WHERE status=?",
                (stable, utcnow(), unstable),
            )
        else:
            cur = conn.execute(
                "UPDATE assets SET status=?, last_attempt=? WHERE status=?",
                (stable, utcnow(), unstable),
            )
        if cur.rowcount:
            reset[unstable] = cur.rowcount
    conn.commit()
    return reset


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def upsert_discovered(
    conn: sqlite3.Connection,
    node_id: str,
    remote_path: str,
    remote_name: str,
    remote_size: int | None,
    remote_modified: str | None,
    now: str | None = None,
    claimed_sha1: str | None = None,
    capture_time: str | None = None,
) -> str:
    """Insert or refresh one node. Returns 'new', 'changed' or 'unchanged'."""
    now = now or utcnow()
    row = conn.execute(
        "SELECT * FROM assets WHERE node_id=?", (node_id,)
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO assets (node_id, remote_path, remote_name, remote_size,"
            " remote_modified, status, first_seen, last_attempt, claimed_sha1,"
            " capture_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (node_id, remote_path, remote_name, remote_size, remote_modified,
             DISCOVERED, now, now, claimed_sha1, capture_time),
        )
        return "new"

    content_changed = (
        (row["remote_size"] or 0) != (remote_size or 0)
        or (row["remote_modified"] or "") != (remote_modified or "")
    )

    if content_changed:
        # The bytes moved under us: start this node over.
        conn.execute(
            "UPDATE assets SET remote_path=?, remote_name=?, remote_size=?,"
            " remote_modified=?, local_path=NULL, sha1=NULL, status=?,"
            " immich_asset_id=NULL, is_duplicate=0, attempts=0,"
            " last_attempt=?, last_error=NULL, claimed_sha1=?, capture_time=?"
            " WHERE node_id=?",
            (remote_path, remote_name, remote_size, remote_modified,
             DISCOVERED, now, claimed_sha1, capture_time, node_id),
        )
        return "changed"

    if row["remote_path"] != remote_path or row["remote_name"] != remote_name:
        # A rename is metadata only -- node_id is stable, the bytes are not new.
        conn.execute(
            "UPDATE assets SET remote_path=?, remote_name=? WHERE node_id=?",
            (remote_path, remote_name, node_id),
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
    limit: int | None = None,
    max_bytes: int | None = None,
    max_attempts: int = 5,
    backoff_base_sec: int = 300,
    backoff_cap_sec: int = 86400,
    now: datetime | None = None,
) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM assets WHERE status=? OR (status=? AND sha1 IS NULL AND attempts<?)"
        " ORDER BY remote_modified IS NULL, remote_modified, node_id",
        (DISCOVERED, FAILED, max_attempts),
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
    limit: int | None = None,
    max_attempts: int = 5,
    backoff_base_sec: int = 300,
    backoff_cap_sec: int = 86400,
    now: datetime | None = None,
) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM assets WHERE status=?"
        " OR (status=? AND sha1 IS NOT NULL AND local_path IS NOT NULL AND attempts<?)"
        " ORDER BY last_attempt IS NULL, last_attempt, node_id",
        (DOWNLOADED, FAILED, max_attempts),
    ).fetchall()
    rows = _eligible_failed(rows, "upload", backoff_base_sec, backoff_cap_sec, now)
    return rows[:limit] if limit is not None else rows


def select_for_precheck(
    conn: sqlite3.Connection,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Discovered rows carrying a claimed digest, so Immich can be asked
    whether the file is already there before a byte is transferred."""
    rows = conn.execute(
        "SELECT * FROM assets WHERE status=? AND claimed_sha1 IS NOT NULL"
        " ORDER BY node_id",
        (DISCOVERED,),
    ).fetchall()
    return rows[:limit] if limit is not None else rows


def select_for_verify(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM assets WHERE status=? ORDER BY last_attempt", (UPLOADED,)
    ).fetchall()
    return rows[:limit] if limit is not None else rows


def select_for_reap(
    conn: sqlite3.Connection,
    keep_days: int = 7,
    now: datetime | None = None,
) -> list[sqlite3.Row]:
    """Verified rows whose grace period has elapsed and still hold a file."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=keep_days)
    rows = conn.execute(
        "SELECT * FROM assets WHERE status=? AND local_path IS NOT NULL", (VERIFIED,)
    ).fetchall()
    if keep_days <= 0:
        return list(rows)
    out = []
    for row in rows:
        ts = parse_ts(row["last_attempt"])
        if ts is None or ts <= cutoff:
            out.append(row)
    return out


def select_verified_without_file(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Verified rows that never had a local file -- matched by claimed digest
    and so never downloaded. There is nothing to delete, only to close out."""
    return conn.execute(
        "SELECT * FROM assets WHERE status=? AND local_path IS NULL", (VERIFIED,)
    ).fetchall()


def get(conn: sqlite3.Connection, node_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM assets WHERE node_id=?", (node_id,)).fetchone()


# --------------------------------------------------------------------------
# transitions
# --------------------------------------------------------------------------

def _set_status(conn: sqlite3.Connection, node_id: str, status: str, **cols: Any) -> None:
    cols.setdefault("last_attempt", utcnow())
    assignments = ", ".join(f"{k}=?" for k in cols)
    conn.execute(
        f"UPDATE assets SET status=?, {assignments} WHERE node_id=?",
        (status, *cols.values(), node_id),
    )


def mark_downloading(conn: sqlite3.Connection, node_id: str) -> None:
    _set_status(conn, node_id, DOWNLOADING)
    conn.commit()


def mark_downloaded(conn: sqlite3.Connection, node_id: str, local_path: str, sha1: str) -> None:
    _set_status(conn, node_id, DOWNLOADED, local_path=local_path, sha1=sha1,
                last_error=None)
    conn.commit()


def mark_uploading(conn: sqlite3.Connection, node_id: str) -> None:
    _set_status(conn, node_id, UPLOADING)
    conn.commit()


def mark_uploaded(
    conn: sqlite3.Connection,
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
    _set_status(conn, node_id, UPLOADED, immich_asset_id=asset_id,
                is_duplicate=1 if is_duplicate else 0, last_error=None, **extra)
    conn.commit()


def mark_verified(conn: sqlite3.Connection, node_id: str) -> None:
    _set_status(conn, node_id, VERIFIED, attempts=0, last_error=None)
    conn.commit()


def mark_purged(conn: sqlite3.Connection, node_id: str) -> None:
    _set_status(conn, node_id, PURGED, local_path=None)
    conn.commit()


def mark_failed(
    conn: sqlite3.Connection,
    node_id: str,
    stage: str,
    error: str,
    max_attempts: int = 5,
) -> str:
    """attempts++, then failed or quarantined. Returns the resulting status."""
    row = get(conn, node_id)
    attempts = (row["attempts"] if row else 0) + 1
    status = QUARANTINED if attempts >= max_attempts else FAILED
    message = f"{stage}: {error}"[:2000]
    _set_status(conn, node_id, status, attempts=attempts, last_error=message)
    conn.commit()
    return status


def requeue(conn: sqlite3.Connection, node_ids: Iterable[str] | None = None) -> int:
    """Put quarantined/failed rows back in play (operator escape hatch)."""
    now = utcnow()
    if node_ids:
        ids = list(node_ids)
        placeholders = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE assets SET status=CASE WHEN sha1 IS NULL THEN ? ELSE ? END,"
            f" attempts=0, last_error=NULL, last_attempt=?"
            f" WHERE node_id IN ({placeholders})",
            (DISCOVERED, DOWNLOADED, now, *ids),
        )
    else:
        cur = conn.execute(
            "UPDATE assets SET status=CASE WHEN sha1 IS NULL THEN ? ELSE ? END,"
            " attempts=0, last_error=NULL, last_attempt=?"
            " WHERE status IN (?,?)",
            (DISCOVERED, DOWNLOADED, now, FAILED, QUARANTINED),
        )
    conn.commit()
    return cur.rowcount


# --------------------------------------------------------------------------
# runs and counts
# --------------------------------------------------------------------------

def start_run(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, started_at, discovered, downloaded,"
        " uploaded, failed, exit_code) VALUES (?,?,0,0,0,0,NULL)",
        (run_id, utcnow()),
    )
    conn.commit()


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    discovered: int,
    downloaded: int,
    uploaded: int,
    failed: int,
    exit_code: int,
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, discovered=?, downloaded=?, uploaded=?,"
        " failed=?, exit_code=? WHERE run_id=?",
        (utcnow(), discovered, downloaded, uploaded, failed, exit_code, run_id),
    )
    conn.commit()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {s: 0 for s in ALL_STATUSES}
    for row in conn.execute("SELECT status, COUNT(*) c FROM assets GROUP BY status"):
        out[row["status"]] = row["c"]
    out["total"] = sum(out[s] for s in ALL_STATUSES)
    return out


def backlog(conn: sqlite3.Connection) -> int:
    """Everything seen but not yet safely in Immich."""
    row = conn.execute(
        "SELECT COUNT(*) c FROM assets WHERE status IN (?,?,?,?,?)",
        (DISCOVERED, DOWNLOADING, DOWNLOADED, UPLOADING, FAILED),
    ).fetchone()
    return row["c"]


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()


def last_success(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE exit_code=0 ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
