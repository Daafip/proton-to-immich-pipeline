# Running it without Docker

The install by hand: a service account, the CLI binary, an env file and systemd
units. [Docker](operations.md#install-with-docker) does all of this for you and
is the better choice for more than one pipeline — this page is here for a
single pipeline on a box where you already have systemd timers you like, or
where a container runtime is not welcome.

It keeps the single-binary, no-daemon shape. What it costs you is in
[What the container layout fixes for free](operations.md#what-the-container-layout-fixes-for-free).

> **This page covers installing and scheduling only.** Everything after that —
> the phases, the backfill, the delete queue, the web UI, migrations, Home
> Assistant, troubleshooting — is in [operations.md](operations.md) and is the
> same either way. Only the prefix differs:
>
> | operations.md says | here, run |
> |---|---|
> | `docker compose run --rm david status` | `python3 sync.py --account david status` |
> | `docker compose logs -f david` | `journalctl -u proton-to-immich-pipeline@david -f` |
> | `docker compose restart david` | nothing — the config is read at each run |
>
> With one pipeline the `--account` is implied and can be left off.

Once the install below is done, **this is the shell every `python3 sync.py`
command on this page and in operations.md assumes** — the service account, not
root, because state and staging have to stay service-owned:

```bash
sudo -u protonsync bash             # nologin shell, so name bash explicitly
cd /opt/proton-to-immich-pipeline
export PIS_CONFIG=/etc/proton-to-immich-pipeline/config.yaml
export IMMICH_API_KEY=...
```

---

## Installing

### On the VM

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

### Configure

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
| `immich.precheck_claimed_digests` | See [operations.md](operations.md#skipping-what-immich-already-has). |

Finding the right `roots` is covered in
[proton-drive-cli.md](proton-drive-cli.md#paths).

---

### Signing in

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

### Work through the phases

Check each before moving on, in the service-account shell from the top of this
page:

```bash
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

Then hand it to systemd — the single-account units, for one pipeline. Two
pipelines use the templated ones instead, see
[Two pipelines under systemd](#two-pipelines-under-systemd) below:

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

---

## Two pipelines under systemd

Two accounts without containers. The isolation is the same as the container
layout's — separate Proton cache dirs, separate databases, separate Immich
keys — but nothing enforces it for you, so the first step is proving it holds.

### Setting it up by hand

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

---

## The web UI

Under Docker the UI is a service of its own and is simply up. By hand it is a
fourth unit. It holds **no Proton session and no Immich key**, so it needs
neither the staging tree nor the API key file:

```bash
python3 sync.py web-password        # prints a web.password_hash line
python3 sync.py serve               # http://127.0.0.1:8080

sudo cp systemd/proton-to-immich-pipeline-web.service /etc/systemd/system/
sudo cp systemd/proton-to-immich-pipeline-env.web.example \
        /etc/proton-to-immich-pipeline/env.web
sudo chown root:protonsync /etc/proton-to-immich-pipeline/env.web
sudo chmod 640 /etc/proton-to-immich-pipeline/env.web
sudo systemctl enable --now proton-to-immich-pipeline-web
```

A password is **required** before it binds anywhere but loopback, and the hash
has a Compose-shaped trap in it whichever way you install — see
[operations.md → Setting the password](operations.md#setting-the-password).

`web.job_runner: systemd` is the bare-metal option for force-sync: the UI
never runs a sync inside a request, so the button hands off to
`systemctl start proton-to-immich-pipeline@<account>.service` through a narrow
NOPASSWD sudoers entry, and systemd owns the lock, the logging and the exit
code. See
[operations.md → Force sync](operations.md#force-sync-never-runs-inside-a-request).

---

## Where to go next

| | |
|---|---|
| The phases, the backfill, the delete queue, the web UI | [operations.md](operations.md) |
| Moving an existing install to a new layout | [operations.md → `sync.py migrate`](operations.md#upgrading-an-existing-install-syncpy-migrate) |
| Going from one account to two | [operations.md](operations.md#going-from-one-account-to-two) |
| What has and has not run against live services | [known-issues.md](known-issues.md) |
| Finding your Proton paths, session stores, CLI flags | [proton-drive-cli.md](proton-drive-cli.md) |
