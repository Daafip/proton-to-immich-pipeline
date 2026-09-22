# Running it

Install, sign in, work through the phases, then hand it to systemd.

---

## Verified on

This ran end to end on **2026-09-21**, real photos out of Proton Drive and
into Immich:

| | |
|---|---|
| VM | Debian on Proxmox, bridged to the LAN |
| Proton CLI | `cli-drive@0.8.0`, `linux-x64` build at `/usr/bin/proton-drive` |
| Immich | `ghcr.io/immich-app/immich-server:v3` in Docker, `0.0.0.0:2283->2283/tcp` |
| Uploader | `ghcr.io/immich-app/immich-cli:latest`, `upload_mode: cli`, `--network host` |
| Service account | `protonsync` |

`login`, `pull`, `download` and `push` all completed against those services.
What that run did **not** cover — `verify`, `reap`, the REST upload path, MQTT
— is listed in
[known-issues.md](known-issues.md#3-what-has-and-has-not-run-live). Every
version-specific detail below is what that stack actually wanted, not what the
documentation of any single release claims.

---

## Install on the VM

The unit runs as an unprivileged service account of its own. **The Immich
docker stack does not create a host user**, so make one — it owns staging and
nothing else:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin protonsync
sudo install -d -o protonsync -g protonsync /mnt/immich/staging
```

Any existing account does just as well: point `User=`/`Group=` in the unit at
whoever owns `/mnt/immich/staging`. `immich.upload_mode: cli` additionally
needs the docker socket (`sudo usermod -aG docker protonsync`), which is
root-equivalent on that host — `upload_mode: api` avoids it entirely.

```bash
sudo install -d -o protonsync -g protonsync /opt/proton-to-immich-pipeline
sudo cp -r sync.py src systemd config.example.yaml /opt/proton-to-immich-pipeline/

sudo install -d /etc/proton-to-immich-pipeline
sudo cp config.example.yaml /etc/proton-to-immich-pipeline/config.yaml
sudo cp systemd/proton-to-immich-pipeline-env.example /etc/proton-to-immich-pipeline/env
sudo chown root:protonsync /etc/proton-to-immich-pipeline/env
sudo chmod 640 /etc/proton-to-immich-pipeline/env      # holds IMMICH_API_KEY
```

PyYAML is used if present but is **not required** — a built-in parser handles
the config format. No other dependencies. For MQTT you want
`apt install mosquitto-clients` (or `paho-mqtt`); without either, reporting
degrades to writing `status.json` only.

Install the Proton CLI from
[proton.me/download/drive/cli](https://proton.me/download/drive/cli/index.html).
Run `grep avx2 /proc/cpuinfo` first and take the
[`linux/x64-baseline`](https://proton.me/download/drive/cli/0.8.0/linux-x64/proton-drive)
build if that comes back empty. **0.8.0 is the version this was run against:**

```bash
sudo wget -O /usr/bin/proton-drive \
  https://proton.me/download/drive/cli/0.8.0/linux-x64/proton-drive
sudo chmod 755 /usr/bin/proton-drive   # wget leaves it 644 -- exec fails even for root
proton-drive --version
```

Flags are not stable between CLI releases; 0.6.0 and 0.8.0 already disagree.
[proton-drive-cli.md](proton-drive-cli.md) records the differences and how the
backend absorbs them.

With `upload_mode: cli`, pulling the uploader image once keeps it out of the
first push, where a registry failure is reported as an upload failure:

```bash
sudo -u protonsync docker pull ghcr.io/immich-app/immich-cli:latest
```

Get the Immich API key from **Account Settings → API Keys** and put it in
`/etc/proton-to-immich-pipeline/env`. Keep it out of `config.yaml`.

---

## Configure

Everything in `config.example.yaml` is optional; omitted keys fall back to the
defaults in `src/config.py`. The three you must set:

```yaml
proton:
  roots: ["/my-files/Photos"]    # a list; each entry is walked recursively
immich:
  url: http://<vm-ip>:2283/api   # the /api suffix is mandatory
  api_key: ""                    # leave empty; use IMMICH_API_KEY instead
```

Worth a look before the first real run:

| Key | Why |
|---|---|
| `immich.album_strategy` | `flat` (one `album_name`), `folder` (album per source folder) or `none`. **Decide before the first push** — changing it later means re-tagging. |
| `reap.keep_days` | How long verified originals linger in staging. Starts at 7; set to 0 once you trust it. |
| `limits.max_files` / `max_bytes` | Per-run caps. Staging shares the SSD with Immich. |
| `staging.min_free_gb` | Hard floor; downloads abort below it. |
| `immich.upload_mode` | `cli` runs the immich-cli container (needs the service account in the `docker` group); `api` uploads over REST with no Docker. A loopback `immich.url` works in both: the container is run with `--network host`. |
| `immich.precheck_claimed_digests` | See [below](#skipping-what-immich-already-has). |

Finding the right `roots` is covered in
[proton-drive-cli.md](proton-drive-cli.md#paths).

---

## Signing in

There is no loopback callback and no port to forward — the CLI polls Proton
while you sign in on whatever device you like. The only real problem is getting
a 200-character URL onto a phone, which `sync.py login` solves by serving it as
a redirect on a LAN port.

Run it **as the service account** — the Proton session is written into
`staging/.proton` at mode 700, and the timer has to be able to read it back:

```bash
sudo -u protonsync bash                  # nologin shell, so name bash explicitly
export PIS_CONFIG=/etc/proton-to-immich-pipeline/config.yaml

python3 sync.py login                    # port 8399, binds 0.0.0.0
python3 sync.py login --port 9000        # any port, just not Immich's 2283
python3 sync.py login --bind 127.0.0.1   # local only
python3 sync.py login --no-serve         # just print the URL
```

```
  Open this on the device you want to sign in with:

      http://192.168.68.52:8399/          <-- phone-friendly
      http://localhost:8399/
```

Open it on the phone and it redirects to Proton; if `qrencode` is installed a
scannable QR is printed too. The command waits for the sign-in and reports the
result.

Two caveats: the page must be reachable from whatever VLAN the phone is on, and
while it is up (5 minutes by default) anyone on that network who opens it gets
a Proton sign-in page. `--bind 127.0.0.1` is there if you would rather not.

**Acceptance:** sign-in must survive a non-interactive shell *and* a reboot.

```bash
ssh you@vm 'sudo -u protonsync env \
  PROTON_DRIVE_CACHE_DIR=/mnt/immich/staging/.proton \
  PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file \
  proton-drive filesystem list / --json'
```

If this fights back for more than an evening, switch to rclone rather than
drifting: set `proton.backend: rclone` and configure an `rclone.conf` remote.
Credentials live in that file, no keyring is involved, and everything
downstream — state, dedupe, verify, reap — is unchanged.

---

## Work through the phases

Check each before moving on:

```bash
sudo -u protonsync bash             # not root: state and staging stay service-owned
cd /opt/proton-to-immich-pipeline
export PIS_CONFIG=/etc/proton-to-immich-pipeline/config.yaml
export IMMICH_API_KEY=...

python3 sync.py pull --dry-run      # counts only, writes nothing
python3 sync.py pull                # a second run must report zero new
python3 sync.py download --limit 20
python3 sync.py push
python3 sync.py verify
python3 sync.py reconcile            # stages nothing unless Immich's trash has
python3 sync.py status               # something this account uploaded
```

After `push`, the 20 files should appear in the Immich UI *and* under
`/mnt/immich/data/library/<user>/...` — the storage template is on, so that
tree is human-readable; use it.

Then hand it to systemd:

```bash
sudo cp systemd/proton-to-immich-pipeline.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proton-to-immich-pipeline.timer
sudo systemctl start proton-to-immich-pipeline.service    # run once, now
journalctl -u proton-to-immich-pipeline -f
```

The timer fires nightly at 03:15 with 30 minutes of jitter and
`Persistent=true`, so a run missed while the VM was off happens at next boot.
The unit runs as **`protonsync`, which must own `/mnt/immich/staging`** — see
[known-issues.md](known-issues.md#4-operational-assumptions).

### The backfill

Only once the nightly cycle has been green for a few days:

```bash
python3 sync.py run --backfill      # uses backfill.max_files / max_bytes
watch df -h /mnt/immich             # and watch dmesg for USB resets
journalctl -u proton-to-immich-pipeline -f | grep -E 'download.batched|circuit'
```

Two settings exist for exactly this run, both covered in
[known-issues.md](known-issues.md) items 1 and 2:

- **`proton.download_batch_size`** (default 25). The CLI costs ~1.2 s of
  startup per invocation, so one call per file is about **8 hours of process
  startup** for a 25k-file library before a byte moves; batching makes that
  ~21 minutes. Batching has never run against real Proton — if the first night
  misbehaves, set it to **1** to restore the old path exactly and
  `sync.py requeue` to clear the failures. A file is size-checked and sha1'd
  before it leaves the scratch folder either way.
- **`limits.consecutive_failures`** (default 25). Stops the pass if that many
  files fail in a row, so a Proton outage at 2 a.m. cannot burn an attempt on
  every remaining file and quarantine the library. Look for `circuit.tripped`.

A tripped pass exits **1** and leaves the rows it never reached untouched, so
the next run simply carries on.

### Skipping what Immich already has

Proton reports a sha1 for every file at discovery and Immich dedupes on sha1,
so the two can be matched *before* anything transfers:

```bash
python3 sync.py precheck          # mark them
python3 sync.py run --precheck    # or fold it into a run
```

Matching rows are marked `uploaded` with the existing asset id and
`is_duplicate`, and never downloaded. They still pass `verify` against the
server, and `reap` closes them out — there is simply no local file to delete.
Replayed against a real 2,109-file listing with half the library already in
Immich, this skips 1,055 downloads and 883 MB.

Off by default, because Proton reports the digest as `sha1Verified: false` — it
is the uploader's claim. A wrong claim matching nothing simply downloads as
normal; the theoretical bad case is a wrong claim matching a *different* asset
already in Immich, which would skip a file that never arrived. Against an empty
Immich it saves nothing, so there is no reason to enable it for a first
backfill.

---

## The delete queue

Deleting a photo in Immich does nothing to Proton, so the copy in Proton comes
straight back into view the next time you look. `reconcile` closes that loop —
in two halves, deliberately: **scanning is automatic, deleting is not.**

```
pull → download → push → verify → reap → reconcile
```

`reconcile` runs as the last step of every `run`. It asks Immich what is in its
trash, matches those asset ids against the rows this account uploaded, and
records each match in `staged_deletes`. **It never calls a Proton mutation.**
The only thing it can do is add to a list.

```bash
python3 sync.py reconcile          # or just let `run` do it
python3 sync.py staged             # what is waiting
python3 sync.py staged --csv       # the same, for a spreadsheet or xargs
```

### Why the list lives in our database

**Immich empties its own trash after about 30 days.** A list recomputed from
the server on each view would silently lose anything you had not got to, while
the file sat in Proton with nothing left to say it should not. So the row is
written on first sighting and stays until you act on it.

Extend or disable the auto-empty under **Administration → Settings → Trash**,
so a staged photo can still be looked up in Immich before you decide.

### Executing it

`delete.action` decides what "execute" means, per account:

| `delete.action` | What happens |
|---|---|
| `mark_only` (default) | Nothing is sent to Proton. You delete the files there yourself, then close the rows out. `staged --csv` gives you the paths. |
| `execute` | `proton-drive filesystem trash` — **reversible**; Proton's trash is the undo. |

`mark_only` is the default because Proton's support docs say items in the
**Photos** section cannot be deleted from desktop apps, and that claim is
untested from the CLI. `cli-drive@0.8.0` does expose `filesystem trash`,
`restore`, `delete` and `empty-trash`, so a `/my-files/...` root — which is
what `proton.roots` ships with — is the `execute` case.

**Without `--yes` this is a dry run, whatever else you pass.**

```bash
python3 sync.py delete-staged                 # dry run, oldest batch_cap rows
python3 sync.py delete-staged 14 15 --yes     # trash exactly those two
python3 sync.py delete-staged --limit 5 --yes # the oldest five
python3 sync.py unstage 14                    # take a row back off the queue
```

Every rule in that path exists because a mistake is a lost photo:

- **Trash, never permanent.** `filesystem delete` and `empty-trash` are never
  invoked. The rclone backend has no trash at all, so it refuses.
- **Ids, never paths.** What to delete is resolved out of `staged_deletes`. A
  path never comes from the caller, and an id can only name a row belonging to
  the account being worked on.
- **Re-resolved first.** The node id currently at the staged path must equal
  the node id that was staged. If the path now holds a different file it is
  skipped and flagged, not deleted.
- **Already gone counts as done.** If the node has vanished, the desired end
  state is the actual one.
- **Verified after.** If `trash` exits 0 but the node is still there, the row
  becomes `delete_failed` rather than claiming success.
- **Capped** at `delete.batch_cap` (50), whatever is asked for.
- **Audited.** Every attempt appends a row to `deletions`, which is never
  updated or deleted.

```bash
sqlite3 /mnt/immich/staging/.state/state.sqlite \
  "SELECT executed_at, result, remote_path, error FROM deletions
   ORDER BY id DESC LIMIT 20;"
```

### It does not come back

The puller diffs on **presence in state**, not on "exists in Immich", and every
delete status is terminal for it. A staged row is not re-downloaded in the
window between staging and deletion — not even if its reported size or mtime
changes, which would normally reset a row to `discovered`. Its recorded path is
frozen too, since that is what the execute path compares against.

If you trashed something in Immich by accident: restore it there, then
`sync.py unstage <id>`. The row becomes an ordinary completed asset again
rather than a fresh download.

---

## The web UI

`sync.py serve` — status per account, a force-sync button, and the delete
queue. Standard library only: no framework, no Node, nothing to build.

```bash
python3 sync.py web-password        # prints a web.password_hash line
python3 sync.py serve               # http://127.0.0.1:8080
```

Then:

```bash
sudo cp systemd/proton-to-immich-pipeline-web.service /etc/systemd/system/
sudo cp systemd/proton-to-immich-pipeline-env.web.example \
        /etc/proton-to-immich-pipeline/env.web
sudo chown root:protonsync /etc/proton-to-immich-pipeline/env.web
sudo chmod 640 /etc/proton-to-immich-pipeline/env.web
sudo systemctl enable --now proton-to-immich-pipeline-web
```

**Set a password before binding anywhere but loopback.** Mirjam uses this, so
it is not a localhost tool — and it can trash files in Proton. `serve` refuses
a non-loopback bind with no password configured rather than publishing the
delete queue to the LAN. `--no-auth` is for localhost development only.

Immich API keys never leave the server: no endpoint returns one, and there is
no build step that could inline one into the page.

### Force sync never runs inside a request

A handler that shelled out to a download would time out, and a page refresh
would start a second one. A click becomes a row in `jobs`; a worker runs them
one at a time. A second click while one is active is rejected with the reason.

| `web.job_runner` | How |
|---|---|
| `subprocess` (default) | One worker thread in the serve process spawns `sync.py run`. No sudo, works from a checkout. |
| `systemd` | `systemctl start proton-to-immich-pipeline@<account>.service` via a narrow NOPASSWD sudoers entry. systemd then owns the lock, the logging and the exit code. |

For the `systemd` runner, install [`systemd/sudoers.example`](../systemd/sudoers.example)
— one line per account, naming the exact unit, never a wildcard.

The existing `flock` still applies either way: a forced run during a scheduled
one exits 3 cleanly rather than racing.

### Endpoints

```
GET  /api/config                    poll intervals, accounts, whether to log in
GET  /api/accounts                  per account: last run, backlog, staged, …
GET  /api/runs?account=             recent rows from `runs`
GET  /api/jobs?account=             recent jobs   ·  GET /api/jobs/<id>
POST /api/jobs                      {type: sync|reconcile|delete, account}
GET  /api/staged-deletes?account=   the staged list + recent deletions
GET  /api/staged-deletes.csv        the same as a download
POST /api/staged-deletes/execute    {ids: [...], dry_run: bool}
POST /api/staged-deletes/unstage    {ids: [...]}
POST /api/login  ·  /api/logout
```

Everything but `/api/config` needs the session cookie. Bodies are JSON only,
which together with `SameSite=Strict` is the CSRF defence.

---

## Two accounts

One VM, one Immich, one `state.sqlite`, two Proton logins. The plan's hard
rule: **the Proton session, the staging subtree and the Immich API key travel
as one object.** The failure mode is uploading one person's photos into the
other's library, which is tedious to unpick afterwards, so three things must
differ per account and `sync.py status` refuses the config if any collides:

| Must differ | Why |
|---|---|
| `staging_dir` | Never a shared `ready/` — the reaper works per account. |
| `proton_cache_dir` | One Proton session per directory. `credentials_store: unsafe_file` keeps it there; the keyring path uses a single fixed service name, so two accounts sharing a keyring invalidate each other. |
| `immich_api_key_file` | A separate Immich **user** per person. |

`state.sqlite` is deliberately *shared*: rows are scoped by an `account`
column, which is what lets the UI show both at once and stops a node id ever
being read against the wrong volume. `node_id` is unique per Proton volume,
not globally.

### Setting it up

Start from [`config.accounts.example.yaml`](../config.accounts.example.yaml).
That form **needs PyYAML** (`apt install python3-yaml`) — the built-in fallback
parser cannot read maps inside a list, and says so rather than guessing.

**Test the session isolation by hand before anything else.** Sign both in,
then list from each within the same minute and check neither session died:

```bash
export PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file
PROTON_DRIVE_CACHE_DIR=/mnt/immich/staging/.proton/david  proton-drive auth login
PROTON_DRIVE_CACHE_DIR=/mnt/immich/staging/.proton/mirjam proton-drive auth login
PROTON_DRIVE_CACHE_DIR=/mnt/immich/staging/.proton/david  proton-drive filesystem list /
PROTON_DRIVE_CACHE_DIR=/mnt/immich/staging/.proton/mirjam proton-drive filesystem list /
PROTON_DRIVE_CACHE_DIR=/mnt/immich/staging/.proton/david  proton-drive filesystem list /
```

The last line is the test: if David's session was invalidated by Mirjam's
login, the cache dirs are not isolating anything and the rest will not work.

Then, per account:

```bash
python3 sync.py --account david login
python3 sync.py --account david pull --dry-run
python3 sync.py --account david run
python3 sync.py status                       # no --account: reports on both
```

### Nightly

One templated timer per account, **staggered** — each holds only its own lock,
so nothing else would stop them competing for Proton bandwidth and the staging
free-space floor:

```bash
sudo cp systemd/proton-to-immich-pipeline@.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proton-to-immich-pipeline@david.timer
sudo systemctl enable --now proton-to-immich-pipeline@mirjam.timer

sudo systemctl edit proton-to-immich-pipeline@mirjam.timer
# [Timer]
# OnCalendar=
# OnCalendar=*-*-* 04:45:00
```

The empty `OnCalendar=` first is required — systemd *appends* otherwise, and
the unit would fire at both times.

**Disable the single-account unit** once accounts are listed
(`systemctl disable --now proton-to-immich-pipeline.timer`), or it will also
try to process an account called `default`.

**And delete `IMMICH_API_KEY` from the shared env file.** It applies to the
base config and therefore to every account. That is the one mistake the
config check cannot save you from silently, so it refuses to run at all when
two accounts end up with the same key.

### Upgrading an existing single-account install

Nothing to do first: the schema migration runs on the next invocation of any
command. It backs `state.sqlite` up beside itself (`state.sqlite.pre-v3-…`),
rebuilds `assets` and `runs` with an `account` column, and assigns every
existing row to `account.name` — `default` unless you change it. The old tables
are kept as `assets_v2` / `runs_v2`, which is the rollback.

```
{"event": "schema.migrated", "to_version": 3, "detail": "backup=… assets runs"}
```

Keep `account.name: default` and the lock and `status.json` keep their v1
paths, so existing Home Assistant sensors carry on working. Rename it and they
become `sync-<name>.lock` and `status-<name>.json`.

---

## Home Assistant

Every run writes `staging/.state/status.json`:

```json
{"last_run": "...", "last_success": "...", "new": 12, "uploaded": 12,
 "failed": 0, "backlog": 0, "auth_ok": true, "stale": false, "...": "..."}
```

Set `mqtt.enabled: true` to publish MQTT discovery messages (via `paho-mqtt` if
installed, otherwise `mosquitto_pub`). That gives sensors for backlog,
uploaded, new, failed, quarantined, staging free space, last run and last
success, plus two `problem` binary sensors:

- **Proton auth** — on when `auth_ok` is false. The failure most likely to
  stall the pipeline silently; alert on this one.
- **Sync stale** — on when the last success is older than
  `report.stale_success_hours` (48 by default).

`mqtt.host` defaults to `127.0.0.1`, which is the VM itself — point it at the
broker (the Home Assistant host, if you run the Mosquitto add-on) and prove it
is reachable before trusting it, because a broken publish is only a warning:

```bash
mosquitto_pub -d -h <broker> -p 1883 -u USER -P PASS -t proton_immich_sync/test -m hello
```

Publishing happens at the end of any non-dry-run phase, not from
`sync.py status`. None of this has been tested against a real broker yet.

Without MQTT, point a `command_line` sensor at `sync.py status --json`.

---

## How state works

SQLite at `staging/.state/state.sqlite`, schema version 3. Five tables:
`assets`, `runs`, `staged_deletes`, `deletions`, `jobs`. **Every row is scoped
by an `account` column**, and `(account, node_id)` is the primary key of
`assets` — a Proton node id is unique within one volume, not globally.

```
discovered ─► downloading ─► downloaded ─► uploading ─► uploaded ─► verified ─► purged
                   │                            │                                 │
                   └──────────► failed ◄────────┘   attempts++, backoff           │
                                  │                                               │
                                  └─► quarantined (attempts >= 5)                 │
                                                                                  ▼
                        remote_trashed ◄─── deleting ◄─── staged_for_delete ◄─────┘
                                                │                    ▲
                             delete_failed ◄────┘         reconcile stages here
```

- **Resume:** on startup every `-ing` state is reset to the previous stable
  state. In-flight state from a crashed run is never trusted. `deleting`
  rewinds to `staged_for_delete`, which is safe because the execute path
  re-resolves every node before touching it.
- **Retry stage** is derived, not stored: a `failed` row with no `sha1` never
  finished downloading and retries there; one with a `sha1` retries at upload.
- **Duplicates** are recorded (`is_duplicate`), never retried. `push` asks the
  server what it already holds *before* sending anything.
- **`last_attempt`** doubles as the time of the last state change; the reaper's
  grace period is measured from it.
- **The four delete states are terminal for the puller.** Neither a `pull` nor
  a `requeue` will move a row out of them — that is what stops a staged file
  being re-downloaded before you get round to deleting it.

```bash
DB=/mnt/immich/staging/.state/state.sqlite
sqlite3 $DB "SELECT account, status, COUNT(*) FROM assets GROUP BY 1, 2;"
sqlite3 $DB "SELECT remote_path, attempts, last_error FROM assets
             WHERE status='quarantined';"
sqlite3 $DB "SELECT id, account, remote_path, staged_at FROM staged_deletes
             WHERE state='staged';"
sqlite3 $DB "SELECT executed_at, result, remote_path, error FROM deletions
             ORDER BY id DESC LIMIT 20;"
```

The migration from v1/v2 keeps the old tables as `assets_v2` and `runs_v2`, and
writes `state.sqlite.pre-v3-<timestamp>` next to the database first. To roll
back, stop everything, restore that file, and downgrade the code.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `217/USER` at unit start | The `User=` account does not exist. Create it, or point the unit at one that does. |
| `Permission denied` running `proton-drive` | Missing execute bit — a download arrives `644`, and exec fails for root too. `sudo chmod 755 /usr/bin/proton-drive`. |
| `Cannot autolaunch D-Bus without X11 $DISPLAY` | The CLI fell back to its `keychain` credentials store. Use `sync.py login`, which sets `PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file`, or export that before calling `proton-drive` by hand. |
| Files under staging owned by `root` | A phase was run as root. `sudo chown -R protonsync:protonsync /mnt/immich/staging` — that also catches `.state/*-wal` and the `.proton` session, which fail separately. |
| `Unable to find image ... locally` then exit 1 | The first push pulls immich-cli and the pull failed (DNS, registry, disk). Pre-pull it as the service account to see the real error. |
| exit 2, `auth.failed` | Proton session gone. Re-run `sync.py login`. Signing in as the wrong user looks identical — `staging/.proton` is mode 700. |
| exit 3, `lock.held` | A previous run is still going. Normal during a backfill. |
| exit 4, `config.invalid` | Setup is wrong and every run will fail the same way. No attempts are charged, so just fix it and re-run — no `requeue` needed. |
| `Unknown option '-c'` on every download | A config pinned to the old alias. 0.8.0 wants `--conflict-strategy skip`; fix `proton.cmd.download` in `config.yaml`, then `sync.py requeue`. |
| `cannot create /mnt/immich/...` | The SSD is not mounted. The unit has `RequiresMountsFor` for exactly this. |
| `download.aborted_low_space` | Free space below `staging.min_free_gb`. Reap, or lower the caps. |
| `circuit.tripped` | Too many consecutive failures — usually Proton or Immich being down, not your files. The pass stopped on purpose; rows it never reached kept their attempts. Fix the cause and re-run. |
| `download.batch_failed_retrying_singly` | One file in a batch failed, so the rest were retried individually. Normal and self-correcting; only worrying if it happens on every batch. |
| Downloads suddenly much slower than expected | `proton.download_batch_size: 1` somewhere, or every batch failing and falling back to single calls — grep for `download.batch_failed_retrying_singly`. |
| `immich.url_missing_api_suffix` | Add `/api`. It is appended automatically, but fix the config. |
| `ECONNREFUSED 127.0.0.1:2283` from immich-cli | A loopback URL now gets `--network host` automatically; you see this only if `immich.docker_args` pins a different `--network`. |
| `download.digest_mismatch` | The downloaded bytes differ from Proton's claimed sha1. Unverified claims make this possible; the local digest is used regardless. |
| Rows stuck in `quarantined` | `sync.py requeue` after fixing the cause; `--now` also ignores backoff. |
| USB resets under load | ASMedia bridge. Boot with `usb-storage.quirks=174c:225c:u` to disable UAS. |
| `config error: this config has 2 accounts` | A command that acts on one account was run without `--account`. Only `status` and `serve` span all of them. |
| `accounts 'a' and 'b' share …` | The hard rule. Give each account its own staging dir, Proton cache dir and Immich key — and remove `IMMICH_API_KEY` from the shared env file. |
| `maps inside lists are not supported` | An `accounts:` list without PyYAML. `apt install python3-yaml`. |
| `schema.migration_failed` (exit 4) | The pre-migration backup could not be written — usually a full or read-only `.state`. Nothing was changed. |
| `reconcile.unavailable` | Immich's `/search/metadata` was unreachable. The sync still succeeded; the queue is just not updated this pass. |
| `delete.failed`, "path now holds a different node" | The staged path has been reused since it was staged. Nothing was deleted. Check it in Proton, then `unstage` the row or re-stage it. |
| `delete.failed`, "still at that path" | `filesystem trash` exited 0 but the node is still there. Try it by hand with `-v`; the row stays visible as `delete_failed`. |
| `delete-staged` reports only dry runs | `--yes` was omitted. Without it the command is a dry run whatever else is passed. |
| Nothing ever appears in `staged` | Immich's trash was auto-emptied before a sync saw it (default ~30 days), or `reconcile.enabled: false`. |
| `web.refusing_unauthenticated_bind` (exit 4) | `web.bind` is not loopback and no password is set. `sync.py web-password`, or bind to 127.0.0.1. |
| The UI rejects every sync click | A job is stuck `running` from a killed server. Restarting the web service releases them; `serve` does that at startup. |
| `sudo: a password is required` in a job | The `systemd` job runner without the sudoers entry. Install `systemd/sudoers.example`, or use `job_runner: subprocess`. |

Logs are one JSON object per line, one per state transition. `--human-logs`
makes them readable interactively; `-v` adds the executed commands.
