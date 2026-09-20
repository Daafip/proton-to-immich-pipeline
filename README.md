# proton-immich-sync

One-way, incremental, resumable sync: **Proton Drive → staging → Immich**.
Python 3.11+, standard library only. Runs unattended under systemd and reports
its health to Home Assistant.

Built from `proton-immich-sync-build-plan.md`. Phases 1–6 are implemented;
**Phase 0 (Proton sign-in on the headless VM) is the part you still have to do
by hand**, and section [Phase 0](#phase-0-proton-sign-in) below is now a lot
shorter than the plan assumed — see [What changed](#what-changed-against-the-plan).

---

## How it works

```
 phone ─► Proton Drive ─► [pull] ─► [download] ─► staging/ready ─► [push] ─► Immich
                             │           │                          │
                             └────► state.sqlite ◄──────────────────┘
                                         │
                                   [verify] → [reap] purges staged files
                                         │
                                   status.json ─► Home Assistant
```

Nothing is deleted locally until the asset is confirmed **server-side by
checksum**, and nothing is transferred twice: every node is tracked by its
Proton node id in SQLite.

| Subcommand | What it does |
|---|---|
| `pull` | Walk the configured roots, record new/changed nodes. No transfers. |
| `precheck` | Mark files Immich already holds, so they are never downloaded. |
| `download` | Fetch `discovered` nodes into `staging/ready/<yyyy-mm>/`, sha1-checked. |
| `push` | Upload to Immich, record asset ids, flag server-side duplicates. |
| `verify` | Confirm each asset exists server-side and its checksum matches. |
| `reap` | Delete verified local files once past the retention grace period. |
| `run` | All of the above, in order. This is what the timer runs. |
| `login` | Sign in to Proton, serving a phone-friendly redirect link. |
| `status` | Print pipeline health (`--json` for machine output). |
| `requeue` | Put `failed` / `quarantined` rows back in play. |

Exit codes: **0** ok · **1** partial failure · **2** auth failure · **3** lock held.

---

## Install on the VM

```bash
sudo install -d -o immich -g immich /opt/proton-immich-sync
sudo cp -r sync.py src systemd config.example.yaml /opt/proton-immich-sync/

sudo install -d /etc/proton-immich-sync
sudo cp config.example.yaml /etc/proton-immich-sync/config.yaml
sudo cp systemd/proton-immich-sync-env.example /etc/proton-immich-sync/env
sudo chown root:immich /etc/proton-immich-sync/env
sudo chmod 640 /etc/proton-immich-sync/env      # holds IMMICH_API_KEY
```

PyYAML is used if present but is **not required** — a built-in parser handles
the config format. No other dependencies.

Get the Immich API key from **Account Settings → API Keys** and put it in
`/etc/proton-immich-sync/env`. Keep it out of `config.yaml`.

---

## Phase 0: Proton sign-in

The one genuinely uncertain step, and the likeliest thing to stall the pipeline
later. Install the CLI from proton.me/download/drive/cli (check `grep avx2
/proc/cpuinfo`; use the `linux/x64-baseline` build if absent).

**Credentials do not have to go through a keyring.** `cli-drive@0.6.0` reads
`PROTON_DRIVE_CREDENTIALS_STORE`, which accepts exactly:

| Value | Where the session lives | Headless? |
|---|---|---|
| `keychain` (default) | libsecret / Secret Service | needs an unlocked keyring |
| `unsafe_file` | plaintext file in the cache dir | **yes — no keyring at all** |
| `pass` | the Unix `pass` store (GPG) | yes |

`config.example.yaml` ships `credentials_store: unsafe_file`, so you should not
need `gnome-keyring` on the VM. The session token is then a plaintext file in
`staging/.proton`, which the tool creates `chmod 700` — leave it that way, and
remember it is a live credential when you back that disk up.

### Signing in, from a phone

**There is no loopback callback.** `auth login --json` prints
`{"signInUrl": "https://account.proton.me/desktop/login?...#payload=..."}` and
then waits. The payload is in the URL *fragment*, which never leaves the
browser — the account page hands the session to Proton's API and the CLI polls
for it. So the browser never calls back to the VM, nothing listens on a local
port, and **no `ssh -L` forwarding is needed**. The CLI's own help says it:
*"you can use different device to sign in"*.

That leaves one problem: getting a 200-character URL onto a phone. `sync.py
login` solves it by serving that URL as a redirect on a LAN port:

```bash
python3 sync.py login              # default port 8399, binds 0.0.0.0
```

```
  Open this on the device you want to sign in with:

      http://192.168.68.52:8399/          <-- phone-friendly
      http://localhost:8399/

  Or paste the full URL directly:

      https://account.proton.me/desktop/login?app=drive&pv=3#payload=...
```

Open that on the phone and it redirects to Proton. If `qrencode` is installed
it also prints a scannable QR. The command waits for the sign-in to finish and
reports the result.

```bash
python3 sync.py login --port 9000    # any port you like, just not Immich's 2283
python3 sync.py login --bind 127.0.0.1   # local only
python3 sync.py login --no-serve         # just print the URL
```

Two things worth knowing: the redirect page has to be reachable from whatever
VLAN the phone is on, and while it is up (5 minutes by default) anyone on that
network who opens it gets a Proton sign-in page. It is a short window on a home
LAN, but `--bind 127.0.0.1` is there if you would rather not.

**Acceptance:** this must work from a *non-interactive* SSH command, and still
work after `sudo reboot`. This is the step that actually bites: on a desktop
with D-Bus running and a session already signed in, a non-interactive shell can
still get `You need to login first`, because the keyring collection is locked.
That is the `keychain` store failing exactly where an unattended timer needs it
to work — hence `unsafe_file` on the VM.

```bash
ssh you@vm 'PROTON_DRIVE_CACHE_DIR=/mnt/immich/staging/.proton \
  PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file \
  proton-drive filesystem list / --json'
```

### Choose what to sync

Proton's own root is `/my-files`, and `/` lists the top-level sections:

```bash
proton-drive filesystem list /                 # sections
proton-drive filesystem list /my-files/Photos  # year folders live here
```

`proton.roots` is a list, and **every entry is walked recursively**. So one
parent folder takes everything beneath it:

```yaml
proton:
  roots:
    - /my-files/Photos
```

Or name folders individually to sync a subset — one year per night keeps a
backfill inside the per-run caps and the free-space floor:

```yaml
proton:
  roots:
    - "/my-files/Photos/Photos from 2025"
    - "/my-files/Photos/Photos from 2026"
    - "/my-files/Photos/Albums 2019 - 2026 google"
```

Spaces and hyphens in folder names are fine and need no quoting (quote only if
a name contains a colon). Nothing is ever passed through a shell, so a name
like `Photos from 2024` stays a single argument.

With `album_strategy: folder`, these folder names become the Immich album
names — `Photos from 2024` and so on — which is worth knowing before you pick
between that and one flat album (open decision 1).

### If Phase 0 fights back

Switch to rclone rather than drifting: set `proton.backend: rclone` and
configure an `rclone.conf` remote. Credentials live in that file, no keyring is
involved, and everything downstream (state, dedupe, verify, reap) is unchanged.

---

## Configure

Everything in `config.example.yaml` is optional; omitted keys fall back to the
defaults in `src/config.py`. The three you must set:

```yaml
proton:
  roots: ["/my-files/Photos"]   # a list; each entry is walked recursively
immich:
  url: http://<vm-ip>:2283/api   # the /api suffix is mandatory
  api_key: ""                    # leave empty; use IMMICH_API_KEY instead
```

Worth a look before the first real run:

- `immich.album_strategy` — `flat` (one `album_name`), `folder` (album per
  source folder) or `none`. **Decide before Phase 3**; changing it later means
  re-tagging. This is open decision 1.
- `reap.keep_days` — how long verified originals linger in staging. Starts at
  7; set it to 0 once you trust the pipeline. This is open decision 4.
- `limits.max_files` / `max_bytes` — per-run caps. Staging shares the SSD with
  Immich, so a backfill must not be allowed to fill it.
- `staging.min_free_gb` — hard floor; downloads abort below it.
- `immich.upload_mode` — `cli` runs the immich-cli container (the plan's
  route); `api` uploads over REST with no Docker involved.

---

## Run it

Work through the phases in order, checking each before moving on:

```bash
cd /opt/proton-immich-sync
export PIS_CONFIG=/etc/proton-immich-sync/config.yaml
export IMMICH_API_KEY=...

python3 sync.py pull --dry-run      # counts only, writes nothing
python3 sync.py pull
python3 sync.py download --limit 20
python3 sync.py push
python3 sync.py verify
python3 sync.py status
```

Then hand it to systemd:

```bash
sudo cp systemd/proton-immich-sync.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proton-immich-sync.timer
sudo systemctl start proton-immich-sync.service    # run once, now
journalctl -u proton-immich-sync -f
```

The timer fires nightly at 03:15 with a 30-minute jitter and `Persistent=true`,
so a run missed while the VM was off happens at next boot.

### The backfill (Phase 7)

Only once the nightly cycle has been green for a few days:

```bash
python3 sync.py run --backfill      # uses backfill.max_files / max_bytes
watch df -h /mnt/immich             # and keep an eye on dmesg for USB resets
```

---

## Home Assistant

Every run writes `staging/.state/status.json`:

```json
{"last_run": "...", "last_success": "...", "new": 12, "uploaded": 12,
 "failed": 0, "backlog": 0, "auth_ok": true, "stale": false, ...}
```

Set `mqtt.enabled: true` to publish MQTT discovery messages (via `paho-mqtt` if
installed, otherwise `mosquitto_pub`). You get sensors for backlog, uploaded,
new, failed, quarantined, staging free space, last run and last success, plus
two `problem` binary sensors:

- **Proton auth** — on when `auth_ok` is false. This is the failure most likely
  to stall the pipeline silently; alert on it.
- **Sync stale** — on when the last success is older than
  `report.stale_success_hours` (48 by default).

Without MQTT, point a `command_line` sensor at `sync.py status --json`.

---

## How state works

SQLite at `staging/.state/state.sqlite`.

```
discovered ─► downloading ─► downloaded ─► uploading ─► uploaded ─► verified ─► purged
                   │                            │
                   └──────────► failed ◄────────┘   attempts++, exponential backoff
                                  │
                                  └─► quarantined (attempts >= 5)
```

- **Resume:** on startup every `-ing` state is reset to the previous stable
  state. In-flight state from a crashed run is never trusted.
- **Retry stage** is derived, not stored: a `failed` row with no `sha1` never
  finished downloading and retries there; one with a `sha1` retries at upload.
- **Duplicates** are recorded (`is_duplicate`), never retried. Immich dedupes
  server-side by hash, so a re-upload is safe but wasteful — `push` asks the
  server what it already has *before* sending anything.
- **`last_attempt`** doubles as the time of the last state change; the reaper's
  grace period is measured from it.

Useful queries:

```bash
sqlite3 /mnt/immich/staging/.state/state.sqlite \
  "SELECT status, COUNT(*) FROM assets GROUP BY status;"
sqlite3 /mnt/immich/staging/.state/state.sqlite \
  "SELECT remote_path, attempts, last_error FROM assets WHERE status='quarantined';"
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| exit 2, `auth.failed` | Proton session gone. Re-run `proton-drive auth login`. |
| exit 3, `lock.held` | A previous run is still going. Normal during a backfill. |
| `cannot create /mnt/immich/...` | The SSD is not mounted. The unit has `RequiresMountsFor` for exactly this. |
| `download.aborted_low_space` | Free space below `staging.min_free_gb`. Reap, or lower the caps. |
| `immich.url_missing_api_suffix` | Add `/api`. It is appended automatically, but fix the config. |
| Rows stuck in `quarantined` | `sync.py requeue` after fixing the cause; `--now` also ignores backoff. |
| USB resets under load | ASMedia bridge. Boot with `usb-storage.quirks=174c:225c:u` to disable UAS. |

Logs are one JSON object per line, one per state transition. `--human-logs`
makes them readable interactively; `-v` adds the executed commands.

---

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

165 tests, no network and no Docker: the Proton backend and Immich server are
faked in-process, so `pull → download → push → verify → reap` runs end to end,
including the failure paths (truncated transfers, checksum mismatches, expired
sessions mid-run, killed runs resuming, quarantine after repeated failures).
`tests/fixtures/` holds captured JSON shapes.

---

## What changed against the plan

Verified locally against `cli-drive@0.6.0` / SDK `js@0.19.2`, which settles
several things the plan had to leave open:

1. **`PROTON_DRIVE_UNSAFE_SECRETS` does not exist in 0.6.0.** The real knob is
   `PROTON_DRIVE_CREDENTIALS_STORE=keychain|unsafe_file|pass`, so headless
   operation is supported outright and `gnome-keyring` should be unnecessary.
   The old name was real, though: `LouisBrunner/ha-proton-drive` sets
   `PROTON_DRIVE_UNSAFE_SECRETS=true` and pins `CLI_VERSION = "0.5.0"`, so the
   variable was renamed between 0.5 and 0.6. That integration is where the
   build plan's claim came from.
2. **The sign-in has no loopback callback**, so no port forwarding is needed —
   see [Signing in, from a phone](#signing-in-from-a-phone). `auth login --json`
   emits a single `signInUrl` line and waits while it polls. (ha-proton-drive
   drives it the same way: read the first stdout line, take `signInUrl`, show
   it to the user.)
3. **`filesystem download` takes a destination *folder*, not a file path**, and
   prompts unless `-c skip` is given — a prompt would hang an unattended run
   forever. Each download gets its own scratch folder so a skipped conflict can
   never promote another node's leftover file.
4. **There is no `auth status` subcommand.** The session probe is
   `filesystem list /`.
5. **An expired session prints `You need to login first`** on stdout, exit 1,
   not JSON even under `--json`. Detecting that string is what turns a dead
   session into `auth_ok: false` instead of a generic error. ha-proton-drive
   matches on the same string, which is reassuring.
6. **`filesystem list` has no recursive flag**, so discovery is a breadth-first
   walk, depth-capped by `proton.max_depth`.
7. **Remote paths start at `/my-files`**, not `/Photos`, and `/` lists the
   top-level sections. On this account the photos live in `/my-files/Photos`,
   one folder per year plus an imported-albums folder.
8. **`name` is a `{"ok": bool, "value": str}` envelope, not a string** —
   Proton names are encrypted, and decryption can fail. A naive parser puts
   `{'ok': True, ...}` on disk as the filename. Everything derived from
   encrypted metadata arrives this way, so unwrapping is applied to every
   field lookup. When `ok` is false the node falls back to its uid, which the
   CLI accepts in paths; local filenames are sanitised because a uid is base64
   and can contain `/`.
9. **There is no `path` field**, and the node id is `uid`. Paths are built from
   the parent path plus the name. Confirmed field set for a listing entry:
   `uid`, `parentUid`, `name`, `type` (`folder`), `folder.isImported`,
   `creationTime`, `modificationTime` (`2026-02-15T16:02:56.000Z`),
   `isShared`, `isSharedPublicly`, `directRole`, `ownedBy`, `keyAuthor`,
   `nameAuthor`, `treeEventScopeId`. A file entry adds `mediaType`,
   `totalStorageSize` and `activeRevision`.
10. **`totalStorageSize` is the encrypted size, not the file size.** The
    content size is `activeRevision.value.claimedSize`, and the encrypted one
    runs ~25% larger (604,740 vs 763,203 bytes on a sample photo). Taking the
    wrong one fails the post-download size check on *every* file, and silently
    inflates the `max_bytes` accounting by a fifth.
11. **Proton already stores a sha1 per file** in
    `activeRevision.value.claimedDigests.sha1` — the same algorithm Immich
    dedupes with, though `sha1Verified` is false, so it is the uploader's claim
    rather than a server guarantee. It is recorded as `claimed_sha1` and a
    mismatch after download is logged, but the locally computed digest is
    what Immich is given.
12. **Timestamps describe the import, not the photo.** A bulk migration stamps
    `creationTime`, `modificationTime` *and* `claimedModificationTime` with the
    migration date; the real date is
    `claimedAdditionalMetadata.Camera.CaptureTime`. Staging buckets therefore
    use capture time — on one sample folder that is 13 directories instead of
    one holding 2,109 files. `modificationTime` is still what change detection
    compares, which is correct: it tracks the node, not the photo.
13. **`mediaType` is reported** (`image/jpeg`, `video/mp4`), so filtering
    prefers it over file extensions and falls back to extensions only when it
    is absent.

Two smaller departures from the plan's design:

- **Asset ids come from the REST API, not from parsing CLI stdout**, which has
  no stable machine-readable form. `push` calls `/assets/bulk-upload-check` (the
  endpoint the CLI itself uses for dedupe) before and after uploading: before,
  it identifies true duplicates and skips sending them; after, it confirms what
  landed and yields the asset id. Checksums go out as sha1 and the client works
  out whether the server wants hex or base64, then remembers.
- **`src/` has three modules the plan did not list** — `config.py`, `log.py` and
  `pipeline.py` — so that the orchestration can be tested against fakes.

`tests/fixtures/proton_list_real_folders.json` is captured from a real
authenticated listing, with only uids and emails redacted, so the folder path
through discovery is covered by a regression test rather than by guesswork.

`proton_list_real_files.json` does the same for file entries. Replaying a real
2,109-entry listing through `pull` discovers all of them, records content sizes
(1.64 GB, where the encrypted sizes would have claimed 1.98 GB), captures a
sha1 for every file, spreads them over 13 capture-date buckets, and reports
zero new on a second pass — the Phase 1 acceptance test, against real data.

Still untested here, because it needs the VM: the sign-in completing end to
end, the download and upload paths against live services, and the immich-cli
container invocation.

### Skipping what Immich already has

Proton reports a sha1 for every file at discovery, and Immich dedupes on sha1,
so the two can be matched *before* anything is transferred:

```bash
python3 sync.py precheck          # mark them
python3 sync.py run --precheck    # or fold it into a run
```

Rows that match are marked `uploaded` with the existing asset id and
`is_duplicate`, and never downloaded. They still pass through `verify` against
the server, and `reap` closes them out — there is simply no local file to
delete. Replayed against a real 2,109-file listing with half the library
already in Immich, that skips 1,055 downloads and 883 MB.

Enable it with `immich.precheck_claimed_digests: true`, or per-run with
`--precheck`. It is off by default because Proton reports the digest as
`sha1Verified: false` — it is the uploader's claim. A wrong claim that matches
nothing simply downloads as normal; the theoretical bad case is a wrong claim
that happens to match a *different* asset already in Immich, which would skip a
file that never actually arrived. Against an empty Immich it saves nothing, so
there is no reason to turn it on for a first backfill.

---

## Out of scope, worth scheduling separately

The SSD is a single copy, and Immich's own docs are explicit that this is not a
backup. Postgres lives on the VM disk and holds faces, embeddings and albums —
expensive to rebuild. Plan a `pg_dump` plus a second copy of `data/` elsewhere,
independently of this pipeline.
