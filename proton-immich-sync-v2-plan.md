# proton-immich-sync v2 — web UI, delete reconciliation, multi-account

Extension plan. Assumes v1 is running: single account, staging under
`/mnt/immich/staging`, Immich at `http://<vm-ip>:2283`, SQLite state, systemd
timer.

Three features. **Multi-account lands last** — it is the only one that needs a
second Proton login, a second Immich user and a changed systemd layout, and
none of that is required to get the UI and the delete queue useful.

The one part of multi-account that *cannot* wait is its schema. `node_id` is
unique only within an account's volume, so every table the UI and the reconcile
work touch needs an `account` column from the start. That migration therefore
runs first, against a single account named in config, and Phase A later only
turns one account into a list. Writing B and C account-blind and retrofitting
would mean rewriting every query twice.

```
Phase 0  schema + account-aware queries   (was A3; lands first)
Phase C  delete reconciliation
Phase B  web UI
Phase A  multi-account for real           (auth, config list, systemd)
```

---

## Phase 0 — Schema, one account, account-aware everywhere

`node_id` is unique per volume, not globally:

```sql
ALTER TABLE assets RENAME TO assets_v2;

CREATE TABLE assets (
  account         TEXT NOT NULL,
  node_id         TEXT NOT NULL,
  remote_path     TEXT,
  remote_name     TEXT NOT NULL,
  remote_size     INTEGER,
  remote_modified TEXT,
  capture_time    TEXT,
  local_path      TEXT,
  sha1            TEXT,
  claimed_sha1    TEXT,
  status          TEXT NOT NULL,
  immich_asset_id TEXT,
  immich_checksum TEXT,
  is_duplicate    INTEGER DEFAULT 0,
  attempts        INTEGER DEFAULT 0,
  first_seen      TEXT NOT NULL,
  last_attempt    TEXT,
  last_error      TEXT,
  PRIMARY KEY (account, node_id)
);

CREATE INDEX idx_assets_status ON assets(account, status);
CREATE INDEX idx_assets_immich ON assets(immich_asset_id);

INSERT INTO assets SELECT '<configured account>', node_id, ... FROM assets_v2;
```

SQLite cannot alter a primary key, so this is the rename-create-copy dance, not
an `ADD COLUMN`. **Back `state.sqlite` up first** — the migration does it
itself, next to the DB, and refuses to run if it cannot.

`runs` gains `account`. `assets_v2` is left in place, not dropped: it is the
rollback.

Config grows a single account identity, which Phase A turns into a list:

```yaml
account:
  name: david
```

Everything downstream — `state.py` selects, the reconcile tables, the API, the
UI — takes `account` as a first-class argument from here on, even while there
is exactly one of them.

**Acceptance:** an existing `state.sqlite` migrates with every row preserved and
`assets_v2` intact; `sync.py run` behaves identically afterwards; a fresh DB is
created at the new schema without going through the migration.

---

## Phase C — Delete reconciliation

Scanning is automatic; deletion is manual and fired from the UI.

**Open question 4 is answered.** `proton-drive` 0.8.0 exposes the full set:

```
filesystem trash path...      # move to trash (reversible)
filesystem restore path...    # undo a trash
filesystem delete path...     # permanent, trashed items only
filesystem empty-trash
filesystem info path          # re-resolve a node before touching it
```

So the execute path is real code, not a to-do list. `delete_action` stays in
config anyway: Proton's support docs say items in the **Photos** section cannot
be deleted from desktop apps, and that claim is untested from the CLI. Default
`mark_only`; `/my-files/...` roots — which is what `proton.roots` ships — are
the `execute` case.

### C1. Reconcile runs as the last step of every sync

```
pull → download → push → verify → reap → reconcile
```

1. `POST /api/search/metadata` with `isTrashed: true`, paginated. The endpoint
   takes one `type` per request, so query `IMAGE` and `VIDEO` and dedupe.
2. Join to `assets` on `immich_asset_id`.
3. Matches → `staged_for_delete`, recording `node_id`, `remote_path`,
   `remote_name` and `staged_at`.

Reconcile never calls a Proton mutation. It only ever adds rows to the staged
list.

### C2. Record on first sighting

**Immich empties its trash after ~30 days by default.** Once purged, the asset
vanishes from the trash query. The staged list must therefore live in your own
DB from first sighting, never be computed live from Immich — otherwise anything
you didn't get to silently drops off the list while the file stays in Proton.

Also extend or disable the auto-empty in Administration → Settings → Trash, so
you can still look a staged photo up in Immich before deciding.

Two tables. The staged list carries the row ids the execute path takes:

```sql
CREATE TABLE staged_deletes (
  id              INTEGER PRIMARY KEY,
  account         TEXT NOT NULL,
  node_id         TEXT NOT NULL,
  remote_path     TEXT,
  remote_name     TEXT,
  immich_asset_id TEXT,
  capture_time    TEXT,
  staged_at       TEXT NOT NULL,
  state           TEXT NOT NULL,   -- staged|deleting|trashed|failed|cancelled
  executed_at     TEXT,
  error           TEXT,
  UNIQUE (account, node_id)
);

CREATE TABLE deletions (            -- append-only audit
  id           INTEGER PRIMARY KEY,
  account      TEXT NOT NULL,
  node_id      TEXT NOT NULL,
  remote_path  TEXT,
  staged_at    TEXT,
  executed_at  TEXT NOT NULL,
  result       TEXT NOT NULL,
  error        TEXT
);
```

`assets.status` moves in step, so the puller sees the row as terminal (C4).

### C3. The execute path

**Safety rules — the only destructive code in the pipeline:**

- **Trash, never permanent.** `filesystem trash`, never `delete` or
  `empty-trash`. Proton's trash is the undo.
- **Only rows already in `staged_deletes`.** The function takes row ids from
  the DB; paths never come from the request body.
- **Re-resolve the path from `node_id` at delete time** with `filesystem info`
  and compare with what's staged. Mismatch or missing node → skip and flag,
  don't delete what's there now.
- **Batch cap** (default 50 per click) and a **dry-run** mode that logs what it
  would trash.
- **Confirmation count in the UI** — "Trash 14 items in Proton".
- **Verify after**: re-resolve the node; still present and untrashed →
  `delete_failed`.
- **`mark_only` never shells out.** It records that you deleted it yourself.

Status flow:

```
verified/purged ─► staged_for_delete ─► deleting ─► remote_trashed
                                            │
                                            └─► delete_failed
```

### C4. Re-download protection

The puller diffs on **presence in state**, not on "exists in Immich". Every
status above is terminal for the puller, so staged and trashed rows are never
re-downloaded in the window between staging and deletion. Verify this
explicitly — it's the one place the two features interact badly.

The subtlety is `upsert_discovered`'s change detection: a staged row whose
size or mtime appears to change would otherwise be reset to `discovered` and
fetched again. Staged and trashed statuses are exempt from that reset.

### C5. UI

- Count badge per account ("14 staged for deletion").
- Table: filename, capture date, Proton path, `node_id`, staged date.
- Row select + bulk select, dry-run toggle, execute button with confirmation.
- CSV export and copy-to-clipboard of paths, for the `mark_only` case.
- Result panel after execution: trashed / failed, with errors.

**Acceptance:** trash a photo in Immich → next sync stages it → execute from the
UI → it lands in Proton's trash → it does not reappear on the next sync.

---

## Phase B — Web UI

### B1. Shape

**Standard library only, no build step.** The rest of this repo is stdlib-only
Python on a box that is awkward to debug, and that constraint is worth more here
than a framework: a status page for two people does not justify `pip install
fastapi uvicorn` plus a Node toolchain and a committed bundle.

- **Backend:** `http.server.ThreadingHTTPServer` in `src/web.py`, port 8080,
  systemd service. Opens `state.sqlite` with `file:...?mode=ro` in WAL mode so
  reads never block the sync writer.
- **Frontend:** one `web/index.html` — markup, CSS and vanilla JS in a single
  file, served by the same process. One origin, no CORS, nothing to build.
- **Polling, not websockets.** 3s while a job is active, 30s idle.

```
src/web.py        routing, read-only DB access, job control, session auth
web/index.html    the whole frontend
sync.py serve     starts it
```

Going back to FastAPI + React later costs nothing structural: the endpoints
below are the contract, and they don't change.

### B2. Endpoints

```
GET  /api/accounts                    per account: last_run, last_success,
                                      auth_ok, backlog, uploaded_total,
                                      failed, quarantined, staged_deletes
GET  /api/runs?account=               recent rows from runs
POST /api/jobs                        {type: sync|reconcile|delete, account}
GET  /api/jobs/<id>                   queued | running | done | failed
GET  /api/staged-deletes?account=     the staged list
POST /api/staged-deletes/execute      {ids: [...], dry_run: bool}
```

### B3. Force sync — never inside the request

A handler that shells out to a long download times out, and a page refresh
starts a second run. Both designs from the original plan get built, behind
`web.job_runner`, because one of them cannot be tested off the VM:

| `web.job_runner` | Behaviour |
|---|---|
| `systemd` | `systemctl start proton-immich-sync@<account>.service` via a narrow NOPASSWD sudoers entry for that exact command. systemd owns the lock, logging and exit codes. The production choice. |
| `subprocess` (default) | A `jobs` table plus one worker thread that spawns `sync.py run`. No sudo, works from a checkout, containerises later. |

Either way the account name is validated against the config allowlist — **never
interpolated from request input**. The existing `flock` means a forced run
during a scheduled one is refused cleanly rather than racing, and a second click
while a job is active is rejected by the job table before it gets that far.

### B4. Auth

Mirjam uses this, so it isn't a localhost tool. Session login in the server
itself: one shared password, `scrypt`-hashed in config or passed as
`PIS_WEB_PASSWORD`, an HMAC-signed cookie, constant-time compare. No dependency
needed for any of that.

**Immich API keys stay server-side** — never in a response body, never in the
HTML. `/api/accounts` reports `auth_ok`, never the key.

**Acceptance:** both accounts' status visible; force-sync reflects job state
within ~5s; a second click while running is rejected with a clear message; an
unauthenticated request to any `/api/*` route gets 401.

---

## Phase A — Multi-account

### A1. Auth isolation (resolve before any code)

The Proton CLI stores one session keyed by a fixed service name
(`ch.proton.drive/drive-sdk-cli`). Two accounts on one machine collide unless
isolated. v1 settled on `PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file` with
`PROTON_DRIVE_CACHE_DIR` pointing into staging, which is the easy case:

| Credentials store | Isolation |
|---|---|
| `unsafe_file` (what v1 ships) | One `PROTON_DRIVE_CACHE_DIR` per account; the session file lives in it. Trivial — the env var is already per-invocation. |
| `keychain` | One Linux system user per account, each with its own keyring and D-Bus session. Changes the systemd layout. |
| `pass` | One `PASSWORD_STORE_DIR` per account. Trivial. |
| rclone fallback | One remote per account in `rclone.conf`. Trivial. |

**Acceptance:** both accounts can `list` within the same minute without either
session being invalidated. Test manually before writing code.

### A2. Config becomes a list of accounts

```yaml
accounts:
  - name: david
    proton_cache_dir: /mnt/immich/staging/.proton/david
    proton_roots: ["/my-files/Photos"]
    staging_dir: /mnt/immich/staging/david
    immich_api_key_file: /etc/proton-to-immich-pipeline/david.key
    album_name: "Proton Import"
    delete_action: mark_only      # or: execute  (see Phase C)
  - name: mirjam
    ...
```

Phase 0's `account.name` is the one-element form of this; both parse to the
same internal list.

Going from one to two on a live install is not just a config edit: existing
rows carry the old account name, and renaming it here without renaming it in
the database re-downloads the whole library. The runbook is
[operations.md → Going from one account to two](docs/operations.md#going-from-one-account-to-two).

**Hard rule: Proton session, staging dir and Immich API key travel as one
object, never as separate globals.** The failure mode is uploading one person's
photos into the other's library — tedious to unpick across two accounts. No code
path may combine a staging path from one account with a key from another.

- Separate Immich **user** per person, each with their own API key.
- Separate staging subtree per account. Never a shared `ready/`.
- Accounts run **sequentially**, one lock each, timers staggered.

### A3. Templated systemd

```
proton-to-immich-pipeline@david.service   + @david.timer
proton-to-immich-pipeline@mirjam.service  + @mirjam.timer
```

Named for the repo rather than the plan's shorthand, to match the existing
`proton-to-immich-pipeline.service`. Staggered timers, set per instance with
`systemctl edit`, so two accounts never download concurrently — each holds
only its own lock.

**Acceptance:** a nightly run processes both accounts; each person's photos
appear only under their own `/mnt/immich/data/library/<user-id>/`.

---

## Build order

| | | |
|---|---|---|
| 1 | **Phase 0** migration + account-aware queries | built |
| 2 | C1–C2 reconcile as a CLI subcommand, no UI | built |
| 3 | B1–B2 server + status UI, read-only | built |
| 4 | B3 force sync, both runners | built (`systemd` runner untested) |
| 5 | C3 execute path | built (**never run against real Proton**) |
| 6 | C5 staged-deletes UI | built |
| 7 | B4 auth | built |
| 8 | **Phase A** A2 config + A3 systemd | built |
| — | **A1 auth isolation, by hand on the VM** | **not done — do this first** |

Everything above is code with tests and no live exercise. Two things have to
happen on the VM before any of it is trusted, and neither is code:

1. **A1, the two-session test.** Five commands, in
   [bare-metal.md](docs/bare-metal.md#setting-it-up-by-hand). If the cache dirs do not
   isolate the Proton sessions, Phase A does not work and no amount of config
   fixes it.
2. **The delete path, on junk files.** `--dry-run`, then `--yes` on two or
   three throwaway files in `/my-files`, then read the `deletions` table.
   [known-issues.md item 5](docs/known-issues.md) lists exactly what is still
   guesswork about it.

Deployment order on the VM: upgrade the code (the schema migrates itself and
backs up first), leave `delete.action: mark_only`, run a night or two, then
bring up the UI on loopback, then set a password and move it to the LAN, then
do the two tests above.

---

## Open questions

1. Does Mirjam's phone back up to her own Proton account, or a shared folder in
   yours? If the latter, Phase A collapses to a config change with two source
   paths.
2. Separate Immich user for her, or a shared library? Separate is cleaner for
   storage and privacy; shared is easier for browsing together.
3. LAN-only UI, or reachable from outside? The latter needs real auth and TLS.
   B4 gives you the auth; TLS is a reverse proxy's job, not this process's.
   `serve` refuses a non-loopback bind with no password, so the unsafe version
   of this decision is not reachable by accident.
4. ~~Does `proton-drive` expose deletion for `/photos`?~~ **Answered:** 0.8.0
   has `trash`, `restore`, `delete` and `empty-trash`. Whether they work on the
   Photos section specifically is still unverified — hence `delete_action`.
