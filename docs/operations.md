# Running it

Docker: one container per pipeline plus one for the UI, a build and an `up`.
It is the recommended install and the only sane way to run more than one
pipeline.

Installing **without Docker** — a service account, the CLI binary, an env file
and systemd units — is a separate page:
**[bare-metal.md](bare-metal.md)**. Everything on *this* page applies either
way; only the command prefix differs, and that page opens with the
translation table.

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
[known-issues.md](known-issues.md#3-what-has-and-has-not-run-live).

**That run predates the container layout.** The image builds and both roles
start, but no container has completed a Proton sign-in or an upload; the
container path also uses `upload_mode: api`, which is the less-travelled one.
Same list, same caveat. Every
version-specific detail below is what that stack actually wanted, not what the
documentation of any single release claims.

---

## Install with Docker

One image, two roles. A pipeline container runs `sync.py agent`; the UI
container runs `sync.py serve`.

```
┌──────────────┐   reads every *.sqlite, writes only `jobs` rows
│  ui          │   holds NO Proton session and NO Immich key
└──────┬───────┘
       │  state volume: david.sqlite, mirjam.sqlite
┌──────┴───────┬──────────────────┐
│  david       │  mirjam          │   one container per pipeline: own Proton
│              │                  │   login, own staging, own database,
└──────┬───────┴─────────┬────────┘   own Immich API key
       │  key: david     │  key: mirjam
       └────────┬────────┘
         ┌──────┴───────┐
         │   immich     │   one instance, one user per person.
         └──────────────┘   The key decides whose library it lands in.
```

**The UI executes nothing.** Clicking *Sync now* writes a row into that
pipeline's `jobs` table; the pipeline's own container picks it up. That is the
only design that crosses a container boundary without mounting the docker
socket — which would be a root-equivalent mount, for an upload.

Each pipeline container runs `sync.py agent`: a loop that owns both the daily
schedule and the job queue. No cron container, no host timer.

### Setting it up

```bash
cp .env.example .env               # uid/gid, paths, Immich URL, web password
cp config.docker.yaml config.yaml  # the accounts, roots, schedule
$EDITOR .env config.yaml
```

**Create the host directories yourself, owned by `PIS_UID`.** This is the
first thing that goes wrong otherwise: a bind mount whose source does not
exist is created *by the docker daemon, as root*, and the container does not
run as root — every service then dies on `Permission denied`.

Use the **numeric uid from `.env`**, not `$(id -u)`:

```bash
# PIS_UID / PIS_GID in .env -- 1000:1000 unless you changed them.
sudo install -d -o 1000 -g 1000 /mnt/immich/pis/{state,staging,secrets}
sudo chmod 700 /mnt/immich/pis/secrets
```

> **Do not write `sudo chown "$(id -u)"` here.** If you are already root —
> and on a box like this you very likely are — `id -u` is **0**, so it
> chowns everything to root: exactly the state you were trying to fix. Worse,
> `chown -R` on a parent like `/mnt/immich` will also take ownership of
> Immich's own `data/` and any bare-metal `staging/`. Name the uid.

One key file per person, from that person's own Immich user:

```bash
printf '%s' 'THE-KEY' | sudo tee /mnt/immich/pis/secrets/default.key >/dev/null
printf '%s' 'HER-KEY' | sudo tee /mnt/immich/pis/secrets/mirjam.key  >/dev/null
sudo chown 1000:1000 /mnt/immich/pis/secrets/*.key
sudo chmod 600 /mnt/immich/pis/secrets/*.key
```

Then build, set the web password, sign each pipeline in, and start:

```bash
docker compose build
docker compose run --rm ui web-password    # prints the PIS_WEB_PASSWORD_HASH line
$EDITOR .env                               # paste it in

# Sign each pipeline in. -p publishes the redirect page so a phone on the
# LAN can reach it; without it the link printed is container-internal.
docker compose run --rm -p 8399:8399 default login
docker compose run --rm -p 8399:8399 mirjam  login

docker compose up -d
docker compose logs -f
```

`login` prints a LAN link *and* the full Proton URL. The URL works from any
device with no port published at all — the sign-in has no loopback callback,
so nothing has to reach back into the container. The `-p` is only there to
make the short link usable. Do the two logins one at a time; they would
otherwise both want port 8399.

**Re-authenticating later, from the UI.** Each account card shows the Proton
session state (*signed in*, *signed out* or *not checked yet*) and when it was
last established. **Check** queues a `status --probe` job, which checks the
session, updates `status.json` and publishes to MQTT. **Re-authenticate**
queues a `login` job: the pipeline starts `auth login`, and within a few
seconds the card shows an **Open Proton sign-in** button, which works on any
device. Once you finish, the card switches to *signed in* by itself, and Home
Assistant's *Proton auth* sensor clears at the same moment. The link expires
after `proton.login_timeout_sec` (300 s by default). It is never written to
the logs or kept in the job's final detail.

Every command works with `PIS_WEB_PASSWORD_HASH` still empty — `web-password`
in particular, which is the one that produces it. `serve` is what refuses to
start without a password on a non-loopback bind (exit 4,
`web.refusing_unauthenticated_bind`), not compose.

`.env` contains nothing named after a person — see
[Adding a person](#adding-a-person).

`PIS_UID`/`PIS_GID` must own the bind-mounted state and staging directories on
the host — the containers do not run as root.

`docker compose run --rm <pipeline> login` prints a link; open it on any
device. There is no loopback callback, so nothing needs forwarding — see
[bare-metal.md → Signing in](bare-metal.md#signing-in) for what that flow
actually does.

### Where the secrets live

Three different things are secret, and they are kept in three different ways.
That is the bit that trips people up, so:

| | What it is | Where it lives | How it gets in |
|---|---|---|---|
| **Immich API key** | one per person | a plain file, `secrets/<account>.key` on the host | bind-mounted read-only at `/secrets`; the config points at it |
| **Proton session** | one per person | `<staging>/<account>/.proton/` | written by `login`, never by you |
| **Web password** | one, shared | an scrypt *hash* in `.env` | `PIS_WEB_PASSWORD_HASH` env var |

These are **not** Docker "secrets" in the Swarm sense — that needs a swarm.
They are ordinary files with ordinary permissions, which is all a single-host
setup needs.

#### The Immich key, step by step

```
  HOST                              CONTAINER "mirjam"
  /mnt/immich/pis/secrets/
      default.key   ──┐
      mirjam.key    ──┼── bind mount, :ro ──►  /secrets/
                      │                            default.key
                      │                            mirjam.key
                      │                                 ▲
  config.yaml         │                                 │
    immich:           │                                 │
      api_key_file: /secrets/{account}.key  ────────────┘
                                   │
                    PIS_ACCOUNT=mirjam resolves {account}
                        → reads /secrets/mirjam.key
```

1. You write each person's key into its own file on the host. One line, no
   newline needed, no quoting, no escaping:

   ```bash
   printf '%s' 'THE-KEY' > /mnt/immich/pis/secrets/mirjam.key
   chmod 600 /mnt/immich/pis/secrets/mirjam.key
   ```

2. `docker-compose.yml` mounts that whole directory **read-only** into every
   pipeline container, at `/secrets`.

3. `config.yaml` names the file with the account token —
   `api_key_file: /secrets/{account}.key` — written once for everyone.

4. Each container sets `PIS_ACCOUNT`, which is what `{account}` resolves to.
   The `mirjam` container reads `/secrets/mirjam.key`; the `default`
   container reads `/secrets/default.key`.

5. The key is read from that file **when it is needed**, never copied into
   the config in memory, so it cannot be logged with it.

The **UI container gets no `/secrets` mount at all** — it never talks to
Immich, so it has no business holding anyone's key.

#### Why files rather than environment variables

- `.env` then contains nothing named after a person, so adding a fifth is one
  line and no new variables (see [Adding a person](#adding-a-person)).
- An env var is visible to anything that can read `/proc/<pid>/environ` and
  shows up in `docker inspect`. A file has an owner and a mode.
- Compose interpolates `.env`, so a value containing `$` is silently mangled —
  the same trap that bites the password hash.

The trade, stated plainly: every pipeline container can read every key in that
directory, because the whole directory is mounted. They all run as the same
uid on the same host anyway. `docker-compose.yml` has a commented alternative
that mounts one key per service if you want the stricter version.

### Day to day

Every CLI command in the rest of this page works in a container; put
`docker compose run --rm <pipeline>` in front of it. The service name *is* the
account name, so `--account` is already implied.

```bash
docker compose run --rm david status
docker compose run --rm david pull --dry-run
docker compose run --rm david staged
docker compose run --rm david delete-staged --yes 14 15

docker compose ps                     # who is up
docker compose logs -f david          # one pipeline's journal
docker compose restart david          # picks up a config.yaml edit
docker compose exec david cat /proc/1/cmdline   # what the agent is running
```

Two things do **not** need a command: the nightly run (each agent owns its own
schedule, `agent.at`) and force-sync (the UI queues it, the agent runs it).

`config.yaml` is mounted read-only, so editing it on the host and restarting
the affected container is the whole update loop. `docker compose up -d` after
a `git pull` + `docker compose build` is the whole upgrade loop.

### Work through the phases

Before trusting the nightly schedule, drive each phase by hand once and check
it before moving on:

```bash
docker compose run --rm david pull --dry-run   # counts only, writes nothing
docker compose run --rm david pull             # a second run must report zero new
docker compose run --rm david download --limit 20
docker compose run --rm david push
docker compose run --rm david verify
docker compose run --rm david reconcile        # stages nothing unless Immich's
docker compose run --rm david status           # trash holds something of yours
```

After `push`, the 20 files should appear in the Immich UI *and* under
`/mnt/immich/data/library/<user>/...` — the storage template is on, so that
tree is human-readable; use it.

Nothing else to hand over afterwards: each pipeline container is already
running its own `agent`, so the nightly run starts at `agent.at` on its own.

### What the container layout fixes for free

| Bare metal | Container |
|---|---|
| Create `protonsync`, `chown` staging | `user:` and two bind mounts |
| `wget` the CLI, remember `chmod 755` | baked into the image, pinned to 0.8.0, `--version` proven at build time |
| `apt install python3-yaml` | in the image |
| Four systemd units + `daemon-reload` | `docker compose up -d` |
| A sudoers entry for force-sync | not needed — the UI only enqueues |
| `IMMICH_API_KEY` leaking to every account | impossible: one container, one account, one key in *its* environment |
| Remembering which key belongs to whom | the service name is the account name, and the key is `secrets/<name>.key` |
| Inventing a variable per person | none: everything is derived from the account name |

### The upload mode

`config.docker.yaml` sets `immich.upload_mode: api`. The `cli` mode shells out
to `docker run immich-cli`, which inside a container needs the docker socket.
The REST path needs nothing — but it is the less-travelled route: see
[known-issues.md item 3](known-issues.md). If a push misbehaves, that is the
first thing to suspect.

### Reaching Immich

Both pipelines join the Immich stack's docker network by name — `IMMICH_NETWORK`
in `.env`, which `docker network ls` will tell you. It is declared
`external: true`, so a wrong name fails at `up` rather than at 03:15.

`IMMICH_URL` is shared; the **keys are not**. Each one is a file,
`secrets/<account>.key`, taken from that person's own Immich user (their
Account Settings → API Keys), and that is what keeps the two libraries apart.
There is deliberately no `DAVID_IMMICH_API_KEY`-style variable per person —
see [Where the secrets live](#where-the-secrets-live).

If Immich is published on the host instead, delete the `networks:` blocks and
point `IMMICH_URL` at the host address.

Someone on a **different Immich instance** overrides the shared URL in their
own service block, which is one line:

```yaml
  bob:
    <<: *pipeline
    environment:
      <<: *pipeline-env
      PIS_ACCOUNT: bob
      IMMICH_INSTANCE_URL: http://other-immich:2283/api
```

and gets a second `external:` network if that instance is on one.

### Pinning the CLI

The image pins `proton-drive` 0.8.0 on purpose: its flags drift between
releases, and a container that silently upgraded would break downloads at
03:15 rather than while you were watching. On a CPU without AVX2
(`grep avx2 /proc/cpuinfo` comes back empty):

```bash
docker compose build --build-arg PROTON_DRIVE_ARCH=linux-x64-baseline
```

A mismatch fails the build rather than the first run — the Dockerfile runs
`proton-drive --version` as the last step of installing it.

---

## The backfill

Only once the nightly cycle has been green for a few days:

```bash
docker compose run --rm david run --backfill   # uses backfill.max_files / max_bytes
docker compose logs -f david | grep -E 'download.batched|circuit'
watch df -h /mnt/immich                        # on the host; watch dmesg for USB resets
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

## Skipping what Immich already has

Proton reports a sha1 for every file at discovery and Immich dedupes on sha1,
so the two can be matched *before* anything transfers:

```bash
docker compose run --rm david precheck        # mark them
docker compose run --rm david run --precheck  # or fold it into a run
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
The only thing it can do is write a list.

It keeps that list in step with the trash **in both directions**. Restore a
photo in Immich and the pending deletion is withdrawn on the next run, because
restoring is how you say "no, keep this one" — and a queue that went on listing
photos you had visibly rescued was contradicting the one signal the feature
reads. See [Withdrawing a staged row](#withdrawing-a-staged-row).

> **A checksum match is not proof the photo is in the library.**
> `/assets/bulk-upload-check` — the endpoint `push` uses to avoid re-sending
> what is already there — answers on checksum alone and does not care that the
> match is sitting in Immich's **trash**.
>
> Left alone, that means a photo this pipeline has never uploaded gets
> recognised as present, recorded as an upload-duplicate, and staged for
> deletion by the very next step — in a single run, on a trash entry that
> predates the pipeline. The symptom is **`in immich` stuck at 0 while
> `staged` climbs by `limits.max_files` every run**.
>
> `push` therefore checks the trash before believing a match:
>
> | `immich.restore_trashed_duplicates` | What happens |
> |---|---|
> | `true` (default) | The asset is **restored out of the trash**, then recorded as uploaded. The photo ends up in the library, which is almost always what was meant. |
> | `false` | The asset is **failed** with a clear error. Choose this when Immich's trash is a deliberate "do not want these" pile. |
>
> Restoring, rather than re-uploading, because a trashed asset still owns its
> checksum — sending the bytes again just produces another duplicate. Either
> way the row is **never recorded as uploaded while the asset is in the
> trash**, which is what used to fill the delete queue with photos that had
> never been uploaded at all.

```bash
docker compose run --rm david reconcile     # or just let `run` do it
docker compose run --rm david staged        # what is waiting
docker compose run --rm david staged --csv  # the same, for a spreadsheet or xargs
```

### Why the list lives in our database

**Immich empties its own trash after about 30 days.** A list recomputed from
the server on each view would silently lose anything you had not got to, while
the file sat in Proton with nothing left to say it should not. So the row is
written on first sighting and stays until you act on it.

Extend or disable the auto-empty under **Administration → Settings → Trash**,
so a staged photo can still be looked up in Immich before you decide.

### Withdrawing a staged row

Restore a photo in Immich and the next `reconcile` takes it back off the queue:
the asset goes back to `purged` — an ordinary completed asset — and the Proton
original is left alone. Nothing to run by hand.

The subtlety is that **an asset can leave the trash in two opposite ways**, and
from the trash listing alone they look identical:

| How it left the trash | What it means | What happens to the staged row |
|---|---|---|
| Someone **restored** it | "Keep this one" | **Withdrawn.** The Proton original stays. |
| Immich's 30-day sweep **purged** it | The asset is gone from the server; the staged row is now the only record that the original was meant to go | **Kept.** |

So a row is withdrawn only on **positive proof of life**: Immich is asked about
that asset directly and has to answer that it holds it and it is not trashed.
Anything else — purged, still trashed, an API error, a truncated trash scan —
leaves the row exactly as it was. Errors never drain the queue.

That costs one lookup per row that has left the trash since the last pass, so
in the steady state it makes no requests at all. `reconcile.cancel_check_max`
(default 5000) caps it; `reconcile.cancel_restored: false` turns the whole
thing off if you would rather the queue stayed an append-only record.

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
docker compose run --rm david delete-staged                 # dry run, oldest batch_cap rows
docker compose run --rm david delete-staged 14 15 --yes     # trash exactly those two
docker compose run --rm david delete-staged --limit 5 --yes # the oldest five
docker compose run --rm david unstage 14                    # take a row off the queue
docker compose run --rm david unstage --all --resync        # ...and sync them again properly
```

`unstage` alone returns the asset to `purged` — right when the photo *is* in
Immich and you trashed it by accident. Restoring it in Immich is usually enough
on its own, since the next `reconcile` withdraws the row for you; `unstage` is
the manual version for when you do not want to wait for a run.

`--resync` returns it to `discovered` instead, so the whole pipeline runs
again. That is the recovery path for rows that were **never really uploaded**;
`purged` is terminal for the puller, so those would otherwise sit there
looking synced while absent from Immich.

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

`sync.py serve` — every pipeline's status side by side, a force-sync button,
and the delete queue. It opens each `<account>.sqlite` **read-only** and
combines them, so it never takes a write lock a pipeline needs.

Standard library only: no framework, no Node, nothing to build.

It is already running: the `ui` service, on `PIS_WEB_PORT` (8080 by default).
Without Docker it is a unit of its own — see
[bare-metal.md → The web UI](bare-metal.md#the-web-ui).

The UI holds **no Proton session and no Immich key** — it never talks to
either. That is why it validates its config on structure alone and starts
happily without credentials, and why the container version mounts no staging
tree.

### Setting the password

```bash
docker compose run --rm ui web-password      # containers
python3 sync.py web-password                 # without Docker
```

It prompts twice and prints the hash in both forms — a
`PIS_WEB_PASSWORD_HASH=…` line for `.env` or a systemd env file, and a
`web.password_hash:` line for the config.

The hash is **colon**-separated, `scrypt:32768:8:1:salt:key`, not the `$` that
crypt-style strings normally use. That is deliberate: Docker Compose
interpolates values from `.env`, so `scrypt$32768$8$1$<salt>$<key>` reaches
the container as `scrypt$32768$8$1` — `$<salt>` is an undefined variable and
expands to nothing. `env_file:` behaves the same way, and systemd's
`EnvironmentFile=` has the same hazard. The result is a silently truncated
hash and a login that can never succeed.

`serve` refuses to start on a hash it cannot parse and says so, rather than
rejecting every password with no explanation. A `$`-separated hash from an
older build still verifies **if it reached the process intact**.

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

| `web.job_runner` | How | For |
|---|---|---|
| `queue` | The UI writes the row and stops. Each pipeline's own `sync.py agent` picks it up. | **Docker.** The only one that crosses a container boundary — no docker socket, no sudo, no cross-container exec. |
| `subprocess` (default) | One worker thread in the serve process spawns `sync.py run`. | Bare metal. No sudo, works from a checkout. |
| `systemd` | `systemctl start proton-to-immich-pipeline@<account>.service` via a narrow NOPASSWD sudoers entry. | Bare metal. systemd then owns the lock, the logging and the exit code. |

For the `systemd` runner, install [`systemd/sudoers.example`](../systemd/sudoers.example)
— one line per account, naming the exact unit, never a wildcard.

`queue` needs an agent per pipeline, which is what the compose file runs. Set
it on bare metal and jobs will sit in the table forever unless you are also
running `sync.py agent` for each account.

The existing `flock` still applies either way: a forced run during a scheduled
one exits 3 cleanly rather than racing.

### Seeing why something failed

The status tiles say *how many*; three places say *why*:

| | |
|---|---|
| **Why things failed** panel in the UI | the distinct errors and how many assets each hit, newest first, with the individual rows folded underneath. `126 failed` becomes `126 × size mismatch: remote 604740 bytes, got 0`. |
| `agent.done` in the container logs | a non-zero exit logs the run's output in its `detail` field. `docker compose logs <account> \| grep agent.done` |
| the run itself, in the foreground | `docker compose run --rm <account> run` — every phase, live |

More from the CLI:

```bash
docker compose run --rm default status            # counts, per account
docker compose run --rm default status --json     # the same, machine-readable
docker compose run --rm default -v run            # add the executed commands
docker compose run --rm default staged            # the delete queue
```

Quarantined assets have used up `limits.max_attempts` and are not retried.
Fix the cause, then `sync.py requeue` puts them back in play — `--now` also
ignores the backoff.

### What the numbers on a card mean

| | Counts |
|---|---|
| **backlog** | Seen in Proton, not yet safely in Immich: `discovered`, `downloading`, `downloaded`, `uploading`, `failed`. |
| **in immich** | Everything that reached Immich and is still there. **Includes rows staged for deletion and already deleted from Proton** — staging is about the Proton original, the Immich asset stays put. |
| **failed** | Will be retried, after a backoff. |
| **quarantined** | Used up `limits.max_attempts`; not retried until `requeue`. |
| **staged** | Pending rows in the delete queue: trashed in Immich, Proton original not yet dealt with. |
| **GB free of N** | The disk the staging tree is on, as measured by the pipeline at its last run: free space, total size, % used and the host path (`PIS_STAGING_DIR`). Red from 90% used; downloads stop at `staging.min_free_gb`. The UI container does not mount staging, so this is never its own disk. |

`in immich` used to count only the three pre-staging statuses, which is exactly
the set staging moves a row *out* of — so every photo staged took one off the
number, and a night that staged 499 read `in immich 0, staged 499` for a
library that plainly had 499 photos in it. The counts now add up.

### Endpoints

```
GET  /api/config                    poll intervals, accounts, whether to log in
GET  /api/accounts                  per account: last run, backlog, staged, …
GET  /api/problems?account=         distinct errors + the assets carrying them
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
which together with the `SameSite=Lax` cookie is the CSRF defence (Lax only
adds top-level GET navigations, and every GET is a read).

---

## Two pipelines

Two Proton accounts, two staging trees, two databases — and **one Immich
instance with a separate user for each person**. The API key is what decides
whose library an upload lands in, so one key each is the thing that matters,
not one instance each.

Two separate Immich instances work equally well: give each account its own
`immich_url`. Nothing in the pipeline assumes either shape.

The plan's hard rule: **the Proton session, the staging subtree and the Immich
API key travel as one object.** The failure mode is uploading one person's
photos into the other's library, so three things must differ per account and
`sync.py status` refuses the config if any collides:

| Must differ | Why |
|---|---|
| `staging_dir` | Never a shared `ready/` — the reaper works per account. |
| `proton_cache_dir` | One Proton session per directory. `credentials_store: unsafe_file` keeps it there; the keyring path uses a single fixed service name, so two accounts sharing a keyring invalidate each other. Optional — it defaults to `<staging_dir>/.proton`, which is already per-account. |
| `immich_api_key_file` | A separate Immich **user** per person, and so a separate key. One instance is fine. |

`immich_url` deliberately is *not* on that list: sharing it is the normal
case. Sharing a **key** is refused —
`accounts 'default' and 'mirjam' share one Immich API key`.

### Adding a person

Those three things are derived from the account name rather than written out
per person, so they cannot be copy-pasted wrong. Write them once in the shared
part of the config, using `{account}`:

```yaml
state:
  dir: /mnt/immich/staging/.state          # shared, never templated
staging:
  root: /mnt/immich/staging/{account}
immich:
  url: http://127.0.0.1:2283/api           # shared
  api_key_file: /etc/proton-to-immich-pipeline/{account}.key

accounts:
  - name: default
  - name: mirjam
  - name: alice
  - name: bob
  - name: kid
```

Each account resolves the token with its own name:

| | derived from the name |
|---|---|
| database | `<state.dir>/<name>.sqlite` |
| staging | `/mnt/immich/staging/<name>`, with `.proton` inside it |
| key file | `/etc/proton-to-immich-pipeline/<name>.key` |
| lock | `sync-<name>.lock` — plain `sync.lock` for `default` |
| status | `status-<name>.json` — plain `status.json` for `default` |
| MQTT | `proton_immich_sync/<name>/state` |

So adding someone is **one line plus a key file**, whether they are the second
or the fifth. An entry can still override anything — a different Immich,
different roots, a different `delete_action` — by naming it under that person.

`state.dir` is deliberately *not* templated: every database lives in one
directory so the UI finds them all with a single read-only mount.

`{account}` is a literal substitution, not `str.format`, so the `{path}` and
`{dest_dir}` templates in `proton.cmd` are left alone.

**Under Docker** it is the same one line, plus a service block that names only
the account:

```yaml
  alice:
    <<: *pipeline
    environment:
      <<: *pipeline-env
      PIS_ACCOUNT: alice
```

and `secrets/alice.key`. There are no `ALICE_*` variables to invent —
`.env` holds nothing named after a person.

### One database per pipeline

Each account gets **its own `<name>.sqlite`**, all in one shared state
directory:

```
/mnt/immich/staging/.state/
    david.sqlite      ← only the david pipeline writes this
    mirjam.sqlite     ← only the mirjam pipeline writes this
    web-secret
```

One writer per file. Two pipelines never contend for a write lock, and a bug
in one cannot reach the other's rows — which matters most in the container
layout, where they are separate processes with separate volumes. The UI mounts
that one directory, opens each database **read-only**, and combines them.

The `account` column stays inside each file even though it is now redundant
there. It keeps a file self-describing, and it is what makes splitting and
merging databases possible in either direction — see
[Going from one account to two](#going-from-one-account-to-two).

`node_id` is unique per Proton volume, not globally, which is why the two
files can hold the same id for different photos without either noticing.

---

## Upgrading an existing install: `sync.py migrate`

There have been two layout changes, and one command handles both:

| | Layout |
|---|---|
| v1/v2 | one `state.sqlite`, no account column |
| v3 | one `state.sqlite`, rows tagged with an `account` |
| **v4** | **one `<account>.sqlite` per pipeline** ← current |

The v3 step is a schema change to a file that is already the right file, so it
still runs automatically on the next command, backing up to
`state.sqlite.pre-v3-…` first and keeping the old tables as `assets_v2` /
`runs_v2`.

The v4 step **never runs by itself**. Splitting one database into several has
to be deliberate: a pipeline that quietly created an empty `david.sqlite` next
to a `state.sqlite` full of David's rows would re-download the whole library.
So any command refuses until you have run the migration:

```
$ sync.py run
This install predates the one-database-per-pipeline layout.
Nothing has been changed. Run:

    sync.py migrate            # shows the plan
    sync.py migrate --yes      # carries it out
```

The plan is printed first and nothing happens without `--yes`:

```
$ sync.py migrate
  plan (2 step(s)):
    - bring state.sqlite up to schema v3
    - split 2109 assets for 'david' out of state.sqlite into david.sqlite

  This is a dry run. Re-run with --yes to carry it out.
```

`--yes` writes `state.sqlite.pre-split-<ts>` first, then retires the original
as `state.sqlite.split-<ts>` rather than deleting it. Both are the rollback.

What it will not do:

- **Guess an owner.** A pre-v3 database has no account column, so every row is
  unowned. One configured account is not a guess; two is, and it stops and
  asks for `--assign-to <account>`.
- **Overwrite.** If a target `<account>.sqlite` already exists it says so and
  leaves both files alone.
- **Silently drop anyone.** Rows for an account the config does not name are
  reported as a warning and stay in the retired file, which is then the only
  copy — so read the warnings before deleting it.

**Verify afterwards**, per account:

```bash
sync.py --account david pull --dry-run     # must report 0 new
```

Keep `account.name: default` and the lock and `status.json` keep their v1
paths, so existing Home Assistant sensors carry on working. Rename it and they
become `sync-<name>.lock` and `status-<name>.json` — and the database becomes
`<name>.sqlite`, which is what makes the rename a real migration rather than a
config edit.

---

## Going from one account to two

A working single-account install already has every row in `state.sqlite`
stamped with an account name. Adding a second person is mostly config plus
whatever runs it — but there is one way to lose a night to it, and it is worth
understanding before you touch anything.

The steps below are written for a bare-metal install, because that is what an
existing one-account setup almost certainly is. **Moving to Docker at the same
time is a reasonable thing to do** and changes only the last step: instead of
templated systemd units, write `config.yaml` and `.env` from
[`config.docker.yaml`](../config.docker.yaml) and `.env.example`, point
`PIS_STATE_DIR` at the *existing* `.state` directory, and `docker compose up
-d`. Steps 1 to 8 — drain, back up, rename, split, verify — are identical
either way, because they are all about the database.

#### The thing that will bite you

**Renaming the existing account in the config does not rename it in the
database.** The rows keep the old name, the puller finds nothing it recognises
under the new one, and the next run re-downloads the entire library while the
old rows sit there owned by nobody:

```
account.name: default  →  accounts: [{name: david}, …]

  rows owned by 'default'  5   ← orphaned, nothing will ever sync them
  rows owned by 'david'    0   → pull reports your whole library as new
```

Two ways out. **Pick one before you start.**

| | Effort | What it costs |
|---|---|---|
| **A — keep the first account called `default`** | none | `accounts:` reads `[{name: default}, {name: mirjam}]`, which is ugly but harmless. `default` is special-cased throughout, so Home Assistant entities, `status.json` and `sync.lock` all keep their v1 names. |
| **B — rename it to a person** | five `UPDATE`s | Tidy config and unit names. Home Assistant entities are renamed, so dashboards and automations need updating. |

**A is the low-risk choice** and the one to take if you are not sure. Nothing
below depends on which you pick except step 4.

#### What actually changes

| | Single account | Two accounts |
|---|---|---|
| Config | top-level `account.name` | an `accounts:` list — **needs PyYAML** |
| Staging | `staging/ready/` | `staging/<name>/ready/` |
| Proton session | `staging/.proton/` | `staging/<name>/.proton/` (automatic — see step 5) |
| Immich key | `IMMICH_API_KEY` in the shared env file | `immich_api_key_file:` per account |
| Immich user | one | **one per person** — not just one key per person |
| Lock | `.state/sync.lock` | `.state/sync-<name>.lock` |
| `status.json` | `.state/status.json` | `.state/status-<name>.json` |
| MQTT node | `proton_immich_sync` | `proton_immich_sync_<name>` |
| MQTT topic | `proton_immich_sync/state` | `proton_immich_sync/<name>/state` |
| systemd | `proton-to-immich-pipeline.timer` | `…@<name>.timer`, one per account |
| Database | one `state.sqlite` | one `<name>.sqlite` **each**, in the same directory |

#### 1. Drain the existing account first

This is what makes the staging move a non-problem. Every row holding a file has
an absolute `local_path` pointing into the *old* staging tree; if you move the
tree out from under those rows, `push` reports `local file missing` and
re-downloads them. Drain and there is nothing to move:

```bash
python3 sync.py run                 # repeat until backlog is 0
python3 sync.py status | grep backlog
python3 sync.py reap --keep-days 0
ls -A /mnt/immich/staging/ready     # must be empty
```

After a clean drain every row is `purged` with `local_path` NULL, so nothing
in the database points at a path that is about to change.

*If you cannot drain* — a backfill is half done and you would rather not lose
it — move the tree and fix the paths in the same breath, with the timer
stopped:

```bash
sudo -u protonsync mkdir -p /mnt/immich/staging/david
sudo -u protonsync mv /mnt/immich/staging/ready /mnt/immich/staging/david/ready
sqlite3 /mnt/immich/staging/.state/state.sqlite \
  "UPDATE assets SET local_path =
     replace(local_path, '/mnt/immich/staging/ready/',
                         '/mnt/immich/staging/david/ready/')
   WHERE local_path LIKE '/mnt/immich/staging/ready/%';"
```

#### 2. Stop everything and back up

```bash
sudo systemctl stop proton-to-immich-pipeline.timer
sudo systemctl stop proton-to-immich-pipeline-web    # if you are running the UI
sudo -u protonsync cp /mnt/immich/staging/.state/state.sqlite \
                      /mnt/immich/staging/.state/state.sqlite.pre-multi
```

The web UI matters: its worker holds a connection and can start a job mid-edit.

#### 3. Install PyYAML

```bash
sudo apt install python3-yaml
```

The built-in fallback parser cannot read maps inside a list. It says so rather
than guessing — `maps inside lists are not supported` — but it is easier to
install this first than to debug that message later.

#### 4. Rename the account — **option B only**

Skip this entirely if you kept `default`.

Do this **before** `sync.py migrate`, while the rows are still in the one
shared `state.sqlite`. Renaming after the split means renaming inside
`default.sqlite` *and* renaming the file, which is more steps and more to get
wrong.

Five tables carry an `account` column and all five must move together, in one
transaction, with nothing running:

```bash
sqlite3 /mnt/immich/staging/.state/state.sqlite <<'SQL'
BEGIN;
UPDATE assets         SET account='david' WHERE account='default';
UPDATE runs           SET account='david' WHERE account='default';
UPDATE staged_deletes SET account='david' WHERE account='default';
UPDATE deletions      SET account='david' WHERE account='default';
UPDATE jobs           SET account='david' WHERE account='default';
COMMIT;
SELECT account, COUNT(*) FROM assets GROUP BY account;
SQL
```

That last `SELECT` must print **one** row, named `david`. Two rows means a
table was missed and half your library is orphaned — restore the backup from
step 2 and start again.

**Never rename onto a name that already owns rows.** `(account, node_id)` is
the primary key of `assets`, so merging two libraries means collisions, and
`staged_deletes` has the same constraint. Rename into an unused name only.

#### 5. Write the new config

Start from [`config.accounts.example.yaml`](../config.accounts.example.yaml).
The minimum per account is a name, a staging dir and a key file:

```yaml
accounts:
  - name: david
    staging_dir: /mnt/immich/staging/david
    immich_api_key_file: /etc/proton-to-immich-pipeline/david.key
    proton_roots: ["/my-files/Photos"]
  - name: mirjam
    staging_dir: /mnt/immich/staging/mirjam
    immich_api_key_file: /etc/proton-to-immich-pipeline/mirjam.key
    proton_roots: ["/my-files/Camera"]
```

`proton_cache_dir` needs no entry: it defaults to `<staging_dir>/.proton`,
which is already one directory per account — which is the whole isolation
requirement. Set it explicitly only if you want the sessions somewhere else.

**Mirjam needs her own Immich *user*, not just her own key.** Administration →
Users → Add, then her API key comes from her own account settings. Sharing one
user would put both libraries in one place and defeat the exercise.

```bash
for who in david mirjam; do
  sudo install -o root -g protonsync -m 640 /dev/null \
       /etc/proton-to-immich-pipeline/$who.key
  printf '%s' 'THE-KEY-FOR-THAT-USER' | \
       sudo tee /etc/proton-to-immich-pipeline/$who.key > /dev/null
done
```

#### 6. Remove the shared `IMMICH_API_KEY`

```bash
sudo sed -i '/^IMMICH_API_KEY=/d' /etc/proton-to-immich-pipeline/env
```

It applies to the *base* config and therefore to every account, so leaving it
in gives both people the same key and uploads one library into the other. The
config check catches exactly this and refuses to run — `accounts 'david' and
'mirjam' share one Immich API key` — so it fails safe rather than quietly, but
delete it anyway.

#### 7. Sign both accounts in

The old session lives in the old cache dir, which nothing points at any more.
Re-logging in is one command and beats moving a credential around by hand:

```bash
sudo -u protonsync python3 sync.py --account david  login
sudo -u protonsync python3 sync.py --account mirjam login
```

Then run the **session isolation test** from
[bare-metal.md](bare-metal.md#setting-it-up-by-hand), with the new
per-account cache dirs.
Do it now, before the timers exist: if the sessions are not isolated, nothing
after this point works and you want to find that out by hand.

#### 8. Split the database, then verify

The rows are still in one shared `state.sqlite`; each pipeline now needs its
own file. The plan comes first:

```bash
python3 sync.py migrate            # shows what it would do
python3 sync.py migrate --yes      # backs up, splits, retires the original
```

Then check what landed:

```bash
python3 sync.py status                       # both accounts, no --account
ls -1 /mnt/immich/staging/.state/*.sqlite    # one per account
```

Read any warnings `migrate` printed. An account it mentions that you did not
expect is an orphan — its rows stay in the retired `state.sqlite.split-…` and
nothing will sync them.

**The real proof is a dry run:**

```bash
python3 sync.py --account david pull --dry-run
```

It must report **0 new**. If it reports your whole library as new, the rows are
still owned by the old name: stop, restore the backup from step 2, and redo
step 4.

#### 9. Hand it back to whatever runs it

**Docker:** write `config.yaml` from
[`config.docker.yaml`](../config.docker.yaml) and `.env` from `.env.example`,
set `PIS_STATE_DIR` to the `.state` directory you just migrated and
`PIS_STAGING_DIR` to the tree holding the per-account subdirectories from
step 1, put each key in `secrets/<account>.key`, then:

```bash
sudo systemctl disable --now proton-to-immich-pipeline.timer
docker compose build
docker compose run --rm david  login       # the sessions are not portable
docker compose run --rm mirjam login
docker compose up -d
```

**systemd:**

```bash
sudo systemctl disable --now proton-to-immich-pipeline.timer
sudo cp systemd/proton-to-immich-pipeline@.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proton-to-immich-pipeline@david.timer
sudo systemctl enable --now proton-to-immich-pipeline@mirjam.timer
sudo systemctl start proton-to-immich-pipeline-web       # if you use the UI
```

**Disabling the old timer is not optional.** Left enabled it keeps running the
untemplated unit, which has no `--account` and will happily process an account
called `default` — a third, empty library alongside the two real ones.

Then stagger them, per [bare-metal.md → Nightly](bare-metal.md#nightly).

#### What this does to Home Assistant

Only if you took option B and renamed. Each account publishes under its own
MQTT node id and its own `status.json`, so:

- `status.json` becomes `status-david.json` — a `file` sensor pointed at the
  old path goes stale rather than erroring. Update the path.
- MQTT discovery creates a **new device** (`proton_immich_sync_david`) with new
  entity ids. The old entities stay in the registry as unavailable; delete them
  once the new ones are reporting.
- Any automation referencing the old `sensor.proton_to_immich_sync_*` entity
  ids needs updating. Grep your config before you rename, not after.

Option A leaves the first account's names untouched — `default` keeps
`status.json`, `sync.lock`, the `proton_immich_sync` node and the
`proton_immich_sync/state` topic. Only Mirjam's are new, so nothing you
already have in Home Assistant moves.

#### Rolling back

Stop the timers, delete the per-account `*.sqlite` files, restore
`state.sqlite.pre-multi` from step 2 as `state.sqlite`, put the old
single-account config back, and re-enable the old timer. The staging move from
step 1 is the only thing that is not in that backup — if you moved `ready/`,
move it back before restoring.

`migrate` leaves two more copies of its own (`.pre-split-…` and `.split-…`),
so there is no point at which only one copy of the database exists.

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
broker (the Home Assistant host, if you run the Mosquitto add-on).

**Where the broker settings go:**

- **Docker:** `mqtt.enabled: true` in `config.yaml` (the `mqtt:` block in
  `config.docker.yaml`), and the broker itself in `.env`:
  `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`. Then
  `docker compose up -d` so the containers pick up the new environment.
  Inside a container `127.0.0.1` is the container, not the host.
- **Bare metal:** the `mqtt:` block in `config.yaml` (see
  `config.example.yaml`), or the same `MQTT_*` variables in the env file the
  systemd unit loads. The environment wins over the file.

### Testing it

```bash
docker compose run --rm david status --probe -v
```

`--probe` checks Proton and Immich, rewrites `status.json` **and publishes to
MQTT** — it is the one-shot way to test a broker without waiting for a
nightly run. Publishing otherwise happens at the end of any non-dry-run phase.

The journal tells you what happened. One of these appears every time:

| Event | Means |
|---|---|
| `mqtt.published` | Sent. Names the transport (`paho-mqtt` or `mosquitto_pub`), the message count and the topic. |
| `mqtt.transport_failed` | That transport failed; the other one is tried next. |
| `mqtt.publish_failed` | Both failed, or neither is installed. `detail` says which and why. |
| `mqtt.disabled` | `mqtt.enabled` is false. Only shown with `-v`. |

If nothing publishes, work down this list:

1. **`mqtt.enabled: true`?** Run with `-v` and look for `mqtt.disabled`. This
   is the commonest answer.
2. **Is a client installed *for the Python running the pipeline*?**
   `apt install mosquitto-clients` or `pip install paho-mqtt` — and under
   systemd that means the interpreter named in `ExecStart`, which is not
   necessarily the one on your `$PATH`. The container image ships
   `paho-mqtt` already.
3. **Is the broker reachable from where the pipeline runs, not just from your
   shell?** From a container: `docker compose run --rm david ping -c1 <broker>`
   — a broker on the host is not on the container network unless you put it
   there. Without Docker, test as the service account:
   `sudo -u protonsync mosquitto_pub -d -h <broker> -p 1883 -t test -m hi`
4. **Watch the other end** while you run `status --probe`:
   `mosquitto_sub -h <broker> -v -t 'proton_immich_sync/#' -t 'homeassistant/#'`

Both transports are tried independently, so a broken `paho-mqtt` no longer
prevents the `mosquitto_pub` fallback from running — that bug made publishing
fail on hosts where `mosquitto_pub` worked perfectly by hand.

**paho-mqtt 2.x** changed its constructor: `Client()` now requires a
`callback_api_version`. Both 1.x and 2.x are handled.

Still untested against a real broker.

Without MQTT, point a `command_line` sensor at `sync.py status --json`.

---

## How state works

**One SQLite database per pipeline**, at `staging/.state/<account>.sqlite`,
schema version 3. Five tables each: `assets`, `runs`, `staged_deletes`,
`deletions`, `jobs`. Every row is scoped by an `account` column and
`(account, node_id)` is the primary key of `assets` — a Proton node id is
unique within one volume, not globally.

The column is redundant inside a single-account file, and kept anyway: it
makes a file self-describing, and it is what lets `sync.py migrate` split and
merge databases in either direction.

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
DB=/mnt/immich/staging/.state/david.sqlite      # one per account
sqlite3 $DB "SELECT account, status, COUNT(*) FROM assets GROUP BY 1, 2;"
sqlite3 $DB "SELECT remote_path, attempts, last_error FROM assets
             WHERE status='quarantined';"
sqlite3 $DB "SELECT id, account, remote_path, staged_at FROM staged_deletes
             WHERE state='staged';"
sqlite3 $DB "SELECT executed_at, result, remote_path, error FROM deletions
             ORDER BY id DESC LIMIT 20;"
```

Migrations keep every previous copy: `assets_v2` / `runs_v2` inside the file
for the v3 schema change, and `state.sqlite.pre-split-…` plus
`state.sqlite.split-…` beside it for the per-pipeline split. To roll back, stop
everything, restore the relevant file, and downgrade the code.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `217/USER` at unit start | The `User=` account does not exist. Create it, or point the unit at one that does. |
| `Permission denied` running `proton-drive` | Missing execute bit — a download arrives `644`, and exec fails for root too. `sudo chmod 755 /usr/bin/proton-drive`. |
| `Cannot autolaunch D-Bus without X11 $DISPLAY` | The CLI fell back to its `keychain` credentials store. Use `sync.py login`, which sets `PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file`, or export that before calling `proton-drive` by hand. |
| Files under staging owned by `root` | A phase was run as root. `sudo chown -R 1000:1000 /mnt/immich/pis/staging` (or `protonsync:protonsync` without Docker) — that also catches `.state/*-wal` and the `.proton` session, which fail separately. Scope it to the mount, never a parent. |
| `Unable to find image ... locally` then exit 1 | The first push pulls immich-cli and the pull failed (DNS, registry, disk). Pre-pull it as the service account to see the real error. |
| exit 2, `auth.failed` | Proton session gone. Re-run `sync.py login`. Signing in as the wrong user looks identical — `staging/.proton` is mode 700. |
| exit 3, `lock.held` | A previous run is still going. Normal during a backfill. |
| exit 4, `config.invalid` | Setup is wrong and every run will fail the same way. No attempts are charged, so just fix it and re-run — no `requeue` needed. |
| `Unknown option '-c'` on every download | A config pinned to the old alias. 0.8.0 wants `--conflict-strategy skip`; fix `proton.cmd.download` in `config.yaml`, then `sync.py requeue`. |
| `cannot create /mnt/immich/...` | The SSD is not mounted. The unit has `RequiresMountsFor` for exactly this. |
| `download.aborted_low_space` | Free space below `staging.min_free_gb`. Reap, or lower the caps. |
| `mqtt.publish_failed` | Both transports failed or neither is installed — `detail` says which. `sync.py status --probe -v` reproduces it on demand. |
| `mqtt.transport_failed`, then `mqtt.published` | Normal: the first transport was unavailable and the second worked. |
| Nothing at all about MQTT in the journal | `mqtt.enabled` is false. `-v` shows `mqtt.disabled`. |
| `Unsupported callback API version` | paho-mqtt 2.x with an older build of this pipeline. Fixed — both 1.x and 2.x are handled now. |
| `staged` climbing by `limits.max_files` a run while `in immich` stays 0 | Every file is being matched to an asset in Immich's **trash** and queued for deletion instead of landing in the library. Fixed by `immich.restore_trashed_duplicates` (default `true`); to recover rows already queued, `sync.py unstage --all --resync`. |
| **Everything** that reaches Immich is staged — `in immich` and `staged` are the same number, and Immich's trash is empty | This Immich ignores the `isTrashed` search filter, so the trash query returns the whole library. Look for `immich.trash_filter_ignored`. Nothing more is staged once that appears, and the rows already queued are withdrawn by the next `reconcile`. |
| `immich.trash_filter_ignored` | As above. The delete queue is inert until the query works — nothing is deleted from Proton, which is the safe half. Check your Immich version's `/search/metadata` support for `isTrashed`. |
| `push.restore_ineffective` | Immich accepted a restore and left the asset in the trash. Those assets are failed, not recorded as uploaded. Restore them in the Immich UI, then `requeue`. |
| The queue lists photos you can see in Immich, and they are **not** in its trash | They were trashed when the row was written and have since been restored. The next `reconcile` withdraws them — `reconcile.cancelled_restored` in the log says how many. Nothing to run by hand. |
| A staged photo is in Immich but `reconcile` will not withdraw the row | Withdrawal needs Immich to confirm the asset is live. Check for `reconcile.cancel_kept` (`purged_by_immich` = Immich deleted it; `unverifiable` = the lookup failed) or `reconcile.cancel_skipped` (the trash scan hit `reconcile.max_pages`). |
| `push.matched_trashed_assets` | Immich recognised these checksums but the assets are in its trash. With the default setting they are restored and the run continues; it is worth knowing how many. |
| `push.restore_failed` | `POST /trash/restore/assets` was refused — the path has moved between Immich versions. The assets are failed rather than recorded as uploaded. Restore them in the Immich UI, then `sync.py requeue`. |
| `reconcile.staged_without_uploading` | Photos were staged that this pipeline never uploaded. With the default settings this should no longer happen; if it does, check `sync.py staged` before executing anything. |
| `Node not found: *` | A glob in `proton.roots`. The CLI takes a path, not a pattern, and nothing expands one — so it looks for a folder literally called `*`. Name the parent instead: **every root is walked recursively**, so `/my-files/Photos` already covers everything beneath it. The config check now refuses this up front. |
| `agent.done` with a non-zero `exit_code` | The `detail` field on that same line carries the run's output — that is where the reason is. `exit 1` = some or all assets failed; `exit 2` = Proton session gone, run `login` again; `exit 3` = a run was already in progress; `exit 4` = config fault. |
| "It runs, but nothing happens" | Run it in the foreground and watch: `docker compose run --rm <account> run`. Most often the Proton roots in `config.yaml` do not match the real folder names — `docker compose run --rm <account> pull --dry-run` reports zero discovered, and `proton-drive filesystem list /my-files` shows what is actually there. |
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
| `web.password_hash is not a usable scrypt hash` | It was truncated in transit — almost always a `$`-separated hash through a `.env` file, where compose ate everything after the first `$`. Regenerate with `sync.py web-password`; the current format uses colons and survives. |
| The web password is rejected no matter what | Same cause as above on an older build. Check the hash in the container: `docker compose exec ui printenv PIS_WEB_PASSWORD_HASH` — if it is shorter than the one in `.env`, that is it. |
| The UI rejects every sync click | A job is stuck `running` from a killed server. Restarting the web service releases them; `serve` does that at startup. |
| `sudo: a password is required` in a job | The `systemd` job runner without the sudoers entry. Install `systemd/sudoers.example`, or use `job_runner: subprocess`. |
| `layout.migration_required` (exit 4) | The shared `state.sqlite` is still there and a pipeline has no database of its own. Nothing was changed — run `sync.py migrate`. |
| Jobs queue in the UI but never run | `job_runner: queue` with no agent for that pipeline. In Docker: `docker compose ps` — is that container up? Bare metal: `queue` needs `sync.py agent` running; use `subprocess` instead. |
| An account shows as `pending` in the UI | It has no database yet, so it has never run. Normal for a pipeline you just added; run it once. |
| `web.layout_migration_required` from `serve` | Same as above — the UI refuses to start against an unsplit install rather than showing half a picture. |
| A container exits with `config.invalid` about a shared Immich key | Two accounts resolving to the same key. In the container layout each pipeline has its own `IMMICH_API_KEY`; check you did not put one in a shared `env` block in compose. |
| `cannot use /state/...` or `/staging/...: Permission denied` | The bind-mount source did not exist, so docker created it as **root**. The message names the uid it needs and the nearest existing directory — that last one is what has the wrong owner. `sudo chown -R 1000:1000 <that directory>`, using the numeric `PIS_UID` from `.env`, **not** `$(id -u)`. Scope it to the mount, never to a parent like `/mnt/immich`. |
| `cannot use /mnt/...` inside a container | The mounted `config.yaml` is the **bare-metal** one; container paths are `/state`, `/staging`, `/secrets`. `cp config.docker.yaml config.yaml`. No amount of chowning fixes this — the message says so when it detects it. |
| The login link is `172.x.x.x` and the phone cannot reach it | That is the container's own address. Re-run with `-p 8399:8399` and use the host's address, or just paste the full Proton URL the same output prints — it works from anywhere. |
| `Permission denied` on the state volume in Docker | `PIS_UID`/`PIS_GID` in `.env` do not match the owner of the bind-mounted directories. `ls -ln` the host path. |
| `network immich_default declared as external, but could not be found` | `IMMICH_NETWORK` in `.env` does not match a real network. `docker network ls`. Or drop the `networks:` blocks and point `IMMICH_URL` at a host address. |
| `required variable PIS_WEB_PASSWORD_HASH is missing a value` | An older compose file. It made *every* command fail — including the `run ... web-password` that generates the hash. Pull the current `docker-compose.yml`. |

Logs are one JSON object per line, one per state transition. `--human-logs`
makes them readable interactively; `-v` adds the executed commands.
