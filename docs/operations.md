# Running it

Install, sign in, work through the phases, then hand it to systemd.

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

Install the Proton CLI from [proton.me/download/drive/cli/index.html](https://proton.me/download/drive/cli/index.html)— check
`grep avx2 /proc/cpuinfo` and use the [`linux/x64-baseline`](https://proton.me/download/drive/cli/0.8.0/linux-x64/proton-drive) build if absent. Install in /usr/bin after using wget to download. -> This was tested using 0.8.0.

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
| `immich.upload_mode` | `cli` runs the immich-cli container; `api` uploads over REST with no Docker. `cli` also needs `immich.url` to be routable *from a container* — no loopback — and the service account in the `docker` group. |
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
python3 sync.py status
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
```

Read [known-issues.md](known-issues.md) first — throughput and the missing
circuit breaker both matter most at backfill scale.

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
- **Duplicates** are recorded (`is_duplicate`), never retried. `push` asks the
  server what it already holds *before* sending anything.
- **`last_attempt`** doubles as the time of the last state change; the reaper's
  grace period is measured from it.

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
| `217/USER` at unit start | The `User=` account does not exist. Create it, or point the unit at one that does. |
| exit 2, `auth.failed` | Proton session gone. Re-run `sync.py login`. Signing in as the wrong user looks identical — `staging/.proton` is mode 700. |
| exit 3, `lock.held` | A previous run is still going. Normal during a backfill. |
| exit 4, `config.invalid` | Setup is wrong and every run will fail the same way. No attempts are charged, so just fix it and re-run — no `requeue` needed. |
| `Unknown option '-c'` on every download | A config pinned to the old alias. 0.8.0 wants `--conflict-strategy skip`; fix `proton.cmd.download` in `config.yaml`, then `sync.py requeue`. |
| `cannot create /mnt/immich/...` | The SSD is not mounted. The unit has `RequiresMountsFor` for exactly this. |
| `download.aborted_low_space` | Free space below `staging.min_free_gb`. Reap, or lower the caps. |
| `immich.url_missing_api_suffix` | Add `/api`. It is appended automatically, but fix the config. |
| `ECONNREFUSED 127.0.0.1:2283` from immich-cli | Loopback inside the container is the container. Set `immich.url` to the host's LAN IP, or switch to `upload_mode: api`. |
| `download.digest_mismatch` | The downloaded bytes differ from Proton's claimed sha1. Unverified claims make this possible; the local digest is used regardless. |
| Rows stuck in `quarantined` | `sync.py requeue` after fixing the cause; `--now` also ignores backoff. |
| USB resets under load | ASMedia bridge. Boot with `usb-storage.quirks=174c:225c:u` to disable UAS. |

Logs are one JSON object per line, one per state transition. `--human-logs`
makes them readable interactively; `-v` adds the executed commands.
