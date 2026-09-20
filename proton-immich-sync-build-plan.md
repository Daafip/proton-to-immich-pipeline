# proton-immich-sync — build plan (final)

Handoff document for a coding session. Environment below is live and verified;
everything under "Phases" is still to build.

---

## 1. Environment (as built)

**Host:** Proxmox VE on a mini PC, headless.
**VM:** Debian 13 (trixie), Docker Engine from Docker's apt repo.

| Thing | Value |
|---|---|
| SSD | `/dev/sdb`, 233 GB, ext4, label `immich` |
| SSD UUID | `b8df3206-ae91-4871-a206-b0e34b65b6a6` |
| USB bridge | ASMedia `174c:225c` (Ugreen enclosure), passed through as `usb0` |
| Mount point | `/mnt/immich` (fstab: `nofail,_netdev,x-systemd.device-timeout=30`) |
| Immich upload location | `/mnt/immich/data` |
| Pipeline staging | `/mnt/immich/staging` |
| Immich stack | `/opt/immich` (compose + `.env`) |
| Postgres data | `/opt/immich/postgres` (VM disk, deliberately *not* on the SSD) |
| Immich web | `http://<vm-ip>:2283` |
| Immich API base | `http://<vm-ip>:2283/api` |
| Storage template | enabled |

Capacity: ~233 GB total, so roughly 180 GB of originals once thumbnails and
transcodes are accounted for. Staging shares the same disk — cap per-run
downloads so a backfill can't fill it.

---

## 2. Scope

**In:** one-way Proton Drive → staging → Immich, incremental, idempotent,
resumable, unattended, with a health signal into Home Assistant.

**Out:** writing back to Proton, reverse sync, replacing the phone's Proton
backup, external libraries (wrong path — Immich owns the files here).

---

## 3. Architecture

```
 phone ─► Proton Drive ─► [puller] ─► /mnt/immich/staging ─► [pusher] ─► Immich
                             │                │                │
                             └──────► state.sqlite ◄───────────┘
                                          │
                                    [reaper] purges staged files
                                          │
                                    status.json ─► Home Assistant
```

Subcommands of one script: `pull`, `download`, `push`, `verify`, `reap`,
`run` (all of the above), `status`.

---

## 4. Verified tool interfaces

### Proton Drive CLI
- Bun-based standalone binary; download from proton.me/download/drive/cli.
- Sign-in is browser-based; session lands in the **OS secret store** under
  `ch.proton.drive/drive-sdk-cli` — libsecret on Linux.
- `PROTON_DRIVE_CACHE_DIR` redirects cache, app data and logs to one directory.
  **It does not cover credentials.**
- A third-party wrapper documents `PROTON_DRIVE_UNSAFE_SECRETS` for a plaintext
  session file on headless systems without Secret Service. **Unverified — this
  is the first thing Phase 0 checks** (`proton-drive help`, `--verbose`).
- `--json` / `-j` for machine-readable output on every subcommand.
- Fair use: transfer only what changed. The state DB is a requirement, not an
  optimisation.

### Immich CLI (run via Docker, no Node needed)
```bash
docker run --rm \
  -v /mnt/immich/staging:/import:ro \
  -e IMMICH_INSTANCE_URL=http://<vm-ip>:2283/api \
  -e IMMICH_API_KEY=<key> \
  ghcr.io/immich-app/immich-cli:latest \
  upload --recursive /import
```
- `IMMICH_INSTANCE_URL` **must include the `/api` suffix**.
- Server-side dedupe by hash, so re-uploads are safe; record duplicates as
  duplicates, not failures.
- Useful flags: `--recursive`, `--dry-run`, `--album-name`, `--concurrency`,
  `--ignore`. Do **not** use `--delete` — the reaper owns deletion.
- API key from Account Settings → API Keys.

---

## 5. State model

SQLite at `/mnt/immich/staging/.state/state.sqlite`.

```sql
CREATE TABLE assets (
  node_id         TEXT PRIMARY KEY,   -- Proton node id, stable across renames
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
  last_error      TEXT
);

CREATE INDEX idx_assets_status ON assets(status);

CREATE TABLE runs (
  run_id     TEXT PRIMARY KEY,
  started_at TEXT, finished_at TEXT,
  discovered INTEGER, downloaded INTEGER, uploaded INTEGER,
  failed     INTEGER, exit_code INTEGER
);
```

```
discovered ─► downloading ─► downloaded ─► uploading ─► uploaded ─► verified ─► purged
                   │                            │
                   └──────────► failed ◄────────┘   attempts++, backoff
                                  │
                                  └─► quarantined (attempts >= 5)
```

**Resume rule:** on startup, reset any `-ing` state to the previous stable
state. Never trust in-flight state from a crashed run.

---

## 6. Layout

```
proton-immich-sync/
├── README.md
├── config.example.yaml
├── sync.py                 # pull | download | push | verify | reap | run | status
├── src/
│   ├── state.py            # schema, transitions, resume
│   ├── proton.py           # proton-drive --json wrapper
│   ├── immich.py           # docker-run CLI wrapper + REST verify
│   └── report.py           # status.json / MQTT
├── systemd/
│   ├── proton-immich-sync.service
│   └── proton-immich-sync.timer
└── tests/                  # fixtures of captured --json output, no network
```

Python 3.11+, stdlib `sqlite3` + `subprocess`. Keep dependencies near zero —
this runs unattended on a box you can't easily debug.

---

## 7. Phases

Each phase has an acceptance test. Don't start the next one until it passes.

### Phase 0 — Proton auth (do this first, alone)
The only genuinely uncertain part. Everything else is mechanical.

- Install the CLI on the VM. Check AVX2 (`grep avx2 /proc/cpuinfo`); if absent,
  use the `linux/x64-baseline` build. CPU type is `host` on the VM, so this
  should be fine.
- `proton-drive auth login` over SSH. The callback is a loopback listener —
  use `ssh -L <port>:localhost:<port>` from your laptop and paste the printed
  URL into your own browser. `--verbose` reveals the actual port.
- Determine whether `PROTON_DRIVE_UNSAFE_SECRETS` exists and works. If it
  does, the pipeline is trivially containerisable later. If it doesn't, install
  `gnome-keyring` + libsecret on the VM and unlock at boot.
- Set `PROTON_DRIVE_CACHE_DIR=/mnt/immich/staging/.proton`.

**Acceptance:** `proton-drive filesystem list /Photos --json` works from a
non-interactive SSH command, *and* still works after `sudo reboot`.

**If this phase fights back for more than an evening**, switch to rclone's
`protondrive` backend — credentials in `rclone.conf`, no keyring, headless by
design. It's reverse-engineered rather than official, but `rclone copy` with
`--max-age` replaces most of Phases 1–2. Decide, don't drift.

### Phase 1 — Puller (discovery only)
- Recursive walk via `proton-drive filesystem list --json`.
- Upsert nodes as `discovered`. No downloads.
- Diff = new `node_id`, or changed `remote_size` / `remote_modified`.

**Acceptance:** `sync.py pull --dry-run` prints new/changed/unchanged counts;
a second consecutive run reports zero new.

### Phase 2 — Download
- Only `discovered` rows. `--conflict-strategy skip`.
- Land in `staging/incoming/<run_id>/`, compute sha1, atomically move to
  `staging/ready/<yyyy-mm>/`.
- Per-run caps: `--max-files`, `--max-bytes`. Check free space before starting;
  abort below a configurable floor (default 20 GB).

**Acceptance:** `sync.py download --limit 20` pulls 20 files, rows go to
`downloaded`, a re-run downloads nothing.

### Phase 3 — Push
- `immich upload --recursive` over `staging/ready/` via the Docker invocation
  above. Capture output, map file → asset id, mark duplicates.
- `--dry-run` supported end-to-end via a config flag.

**Acceptance:** 20 test files appear in the Immich UI and under
`/mnt/immich/data/library/<user>/2026/...` (storage template is on, so the tree
is human-readable — use it).

### Phase 4 — Verify & reap
- Confirm each `uploaded` asset exists server-side via the API and its checksum
  matches the local sha1 → `verified`.
- Delete local file → `purged`. Retention grace period `--keep-days` (default 7,
  settable to 0 once trusted).

**Acceptance:** staging shrinks back after a run; `df -h /mnt/immich` stable
across three consecutive runs.

### Phase 5 — Orchestration
- `flock` single-instance guard.
- systemd service + timer, nightly. Manual `--now`.
- Exponential backoff per asset, `attempts >= 5` → `quarantined`.
- Structured JSON logs, one line per state transition.
- Exit codes: 0 ok, 1 partial failure, 2 auth failure, 3 lock held.

**Acceptance:** timer fires, run completes unattended, `journalctl -u` is
readable, a killed run resumes cleanly on the next tick.

### Phase 6 — Home Assistant
- Write `status.json`: `last_run`, `last_success`, `new`, `uploaded`, `failed`,
  `backlog`, `auth_ok`.
- Surface via MQTT discovery (preferred) or a REST sensor.
- Automations: notify when `auth_ok` is false (the Proton session expiry case —
  the failure most likely to stall silently), when `failed > 0`, or when
  `last_success` is older than 48h.
- Pair with the core Immich integration for library-side counts.

**Acceptance:** one dashboard card shows pipeline health; pulling the Proton
session triggers a notification within a day.

### Phase 7 — Backfill
Only after Phases 0–6 are green. Run with caps, over several nights, monitoring
free space. Separate `--backfill` mode with different limits from the nightly
incremental.

---

## 8. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Proton session expiry / CAPTCHA on re-login | High | `auth_ok` flag + HA notification; documented manual re-login |
| Phase 0 auth turns out to be a dead end | Medium | rclone fallback, decided within one evening |
| ASMedia bridge USB resets under sustained load | Medium | Watch `dmesg` during backfill; `usb-storage.quirks=174c:225c:u` disables UAS if needed |
| Staging fills the shared SSD | Medium | Per-run byte cap + free-space precondition |
| Partial download uploaded as-is | Medium | sha1 + size verified before promoting to `ready/` |
| Immich CLI flags change | Low | Pin the image tag, not `:latest`, once working |
| Duplicate uploads | Low | Server-side hash dedupe; record, don't retry |

---

## 9. Open decisions

1. Album strategy: one flat `Proton Import` album (`--album-name`), or
   folder-derived albums? Pick before Phase 3 — changing it later means
   re-tagging.
2. Which Proton Drive path is the phone's photo backup root?
3. Backfill size (rough asset count) and how many nights it may spread over.
4. Keep staged originals as a second copy, or purge immediately after verify?

---

## 10. Out of scope but worth scheduling

The SSD is a single copy. Immich's own docs are explicit that this is not a
backup. Postgres lives on the VM disk and holds faces, embeddings and albums —
expensive to rebuild. Plan a `pg_dump` + a second copy of `data/` somewhere
else, separately from this pipeline.
