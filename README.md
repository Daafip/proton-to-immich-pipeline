# proton-to-immich-pipeline

One-way, incremental, resumable sync: **Proton Drive → staging → Immich**.
Python 3.11+, standard library only. Runs unattended under systemd and reports
its health to Home Assistant.

```
 phone ─► Proton Drive ─► [pull] ─► [download] ─► staging/ready ─► [push] ─► Immich
                  ▲          │           │                          │         │
                  │          └──► <account>.sqlite ◄────────────────┘         │
      [delete-staged]                    │                                    │
      trash, on request                  ├─ [verify] → [reap] purges staging  │
                  │                      │                                    │
                  └─── staged_deletes ◄──┴─ [reconcile] ◄── what you trashed ──┘
                                         │
                                   status.json ─► Home Assistant
```

One pipeline per person, each with its own Proton account, staging tree,
database and **Immich API key** — normally one Immich instance with a user
each, though separate instances work too. `sync.py serve` reads every database
and combines them:

```
  ui ──reads──► .state/david.sqlite   ◄──writes── david pipeline ──┐
     ──reads──► .state/mirjam.sqlite  ◄──writes── mirjam pipeline ─┤
                                                                   ▼
                                          immich (a user, and a key, each)
```

Nothing is deleted locally until the asset is confirmed **server-side by
checksum**, and nothing is transferred twice: every node is tracked by its
Proton node id in SQLite.

Deleting a photo in Immich stages the Proton copy for deletion too — but
**scanning is automatic and deleting is not**. `reconcile` only ever adds to a
list; trashing in Proton takes a deliberate `--yes` (or a confirmation in the
UI), re-resolves every node first, and is reversible.

**Status:** `login → pull → download → push` has run end to end on a Debian VM
against `cli-drive@0.8.0` and `immich-server:v3` (2026-09-21). `verify`, `reap`,
`reconcile`, the delete path, the REST upload mode and MQTT have not yet run
against live services — see
[docs/known-issues.md](docs/known-issues.md#3-what-has-and-has-not-run-live).

| Subcommand | What it does |
|---|---|
| `login` | Sign in to Proton, serving a phone-friendly redirect link. |
| `pull` | Walk the configured roots, record new/changed nodes. No transfers. |
| `precheck` | Mark files Immich already holds, so they are never downloaded. |
| `download` | Fetch `discovered` nodes into `staging/ready/<yyyy-mm>/`, sha1-checked, batched. |
| `push` | Upload to Immich, record asset ids, flag server-side duplicates. |
| `verify` | Confirm each asset exists server-side and its checksum matches. |
| `reap` | Delete verified local files once past the retention grace period. |
| `reconcile` | Stage the Proton copies of anything you trashed in Immich. |
| `run` | All of the above, in order. This is what the timer runs. |
| `status` | Print pipeline health (`--json` for machine output). |
| `requeue` | Put `failed` / `quarantined` rows back in play. |
| `staged` | List what is staged for deletion (`--csv` for the paths). |
| `delete-staged` | Trash staged files in Proton. Dry run unless `--yes`. |
| `unstage` | Take rows back off the delete queue. |
| `serve` | The web UI: status, force sync, the delete queue, across every pipeline. |
| `agent` | Run one pipeline forever: its schedule plus its job queue. What a container runs. |
| `migrate` | Move an existing install to the layout the config asks for. |
| `web-password` | Hash a password for `web.password_hash`. |

Every command works on one account (`--account`, or `$PIS_ACCOUNT`, implicit
when there is one). `status` and `serve` span all of them.

**Each person is an independent pipeline** — separate Proton account, staging
tree, database and Immich API key. They share the VM and, normally, one Immich
instance with a user each; separate instances work too.

Adding one is a single config line plus a key file, whether they are the
second or the fifth: the per-account paths are written once with `{account}`
and resolved per name, so nothing is named after anyone and the things that
must differ cannot be copy-pasted wrong. See
[Adding a person](docs/operations.md#adding-a-person).
[Docker](docs/operations.md#install-with-docker) is the easy way to run that: one container
per pipeline plus one for the UI.

Exit codes: **0** ok · **1** partial failure · **2** auth failure · **3** lock held ·
**4** config fault (nothing was attempted; fix the config and re-run).

---

## Documentation

| | |
|---|---|
| [docs/operations.md](docs/operations.md) | Install, configure, sign in, run the phases, the delete queue, the web UI, two accounts, systemd, backfill, Home Assistant, state model, troubleshooting. |
| [docs/proton-drive-cli.md](docs/proton-drive-cli.md) | How `cli-drive` actually behaves (verified on 0.6.0, flags re-checked on 0.8.0): command surface, environment, sign-in, the `--json` schema and its four traps. |
| [docs/known-issues.md](docs/known-issues.md) | What is not solved, and what has and has not run against live services. |
| [proton-immich-sync-v2-plan.md](proton-immich-sync-v2-plan.md) | The v2 plan: the schema move, the delete queue, the web UI, two accounts. |

---

## Quick start

**Docker** — one container per pipeline plus one for the UI, and the
recommended path:

```bash
cp .env.example .env                 # uid/gid, keys, paths, web password
cp config.docker.yaml config.yaml    # accounts, roots, schedule
docker compose build
docker compose run --rm ui web-password      # → PIS_WEB_PASSWORD_HASH
docker compose run --rm david login          # once per pipeline
docker compose up -d
```

**By hand:**

```bash
sudo cp config.example.yaml /etc/proton-to-immich-pipeline/config.yaml
export PIS_CONFIG=/etc/proton-to-immich-pipeline/config.yaml
export IMMICH_API_KEY=...            # Immich → Account Settings → API Keys
                                     # one account only — see below

python3 sync.py login                # open the printed link on any device
python3 sync.py pull --dry-run       # counts only, writes nothing
python3 sync.py run                  # pull … verify, reap, reconcile
python3 sync.py serve                # the UI on http://127.0.0.1:8080
```

The three settings you must provide:

```yaml
proton:
  roots: ["/my-files/Photos"]    # a list; each entry is walked recursively
immich:
  url: http://<vm-ip>:2283/api   # the /api suffix is mandatory
  api_key: ""                    # leave empty; use IMMICH_API_KEY instead
```

For two people, start from
[config.accounts.example.yaml](config.accounts.example.yaml) instead (that form
needs PyYAML) and **drop `IMMICH_API_KEY` from the environment** — it applies
to every account, and one shared key uploads one person's photos into the
other's library. Each account gets its own `immich_api_key_file:`;
`sync.py status` refuses a config where two of them collide.

Already running with one account? Follow
[Going from one account to two](docs/operations.md#going-from-one-account-to-two)
rather than editing the config in place — renaming the existing account without
renaming it in the database re-downloads the whole library.

Full deployment, including systemd, the backfill, the delete queue and the UI,
is in [docs/operations.md](docs/operations.md). **Read
[docs/known-issues.md](docs/known-issues.md) before a large backfill.**

---

## Layout

```
sync.py                 CLI entry point
src/config.py           config, Account objects, a PyYAML-free fallback parser
src/log.py              one JSON line per state transition
src/state.py            SQLite schema, transitions, resume, the delete queue
src/migrate.py          moving an install to the layout the config asks for
src/proton.py           Proton CLI backend, rclone fallback backend
src/login.py            sign-in flow + the phone redirect page
src/immich.py           REST client + docker immich-cli uploader
src/pipeline.py         phase orchestration (backends injected, so testable)
src/report.py           status.json + MQTT discovery
src/agent.py            the loop one pipeline container runs
src/web.py              http.server API, session auth, the job worker
web/index.html          the whole frontend: one file, no build step
Dockerfile              one image, two roles: agent and serve
docker-compose.yml      one container per pipeline + one for the UI
systemd/                templated per-account units, nightly timers, web service
tests/                  456 tests, no network, no Docker
```

`config.py`, `log.py`, `login.py` and `pipeline.py` are additions to the layout
the build plan sketched; the rest matches it.

---

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

456 tests, no network and no Docker. The Proton backend and Immich server are
faked in-process, so `pull → download → push → verify → reap → reconcile` runs
end to end, including the failure paths: truncated transfers, checksum
mismatches, sessions expiring mid-run, killed runs resuming, quarantine after
repeated failures.

The v2 additions get the same treatment, and the destructive path gets more of
it than anything else:

- **`tests/test_delete.py`** — the delete queue, mostly about what it refuses
  to do: a reused path, a lying `trash`, an unresolvable node, an over-cap
  batch, an id from another account, a row already executed. Plus the plan's
  acceptance criterion end to end — trash in Immich, stage, execute, and check
  it does not come back.
- **`tests/test_web.py`** — real HTTP against a server on an ephemeral port:
  401s on every route, cookie flags, forged sessions, path traversal, a form
  POST refused, and what the job worker is allowed to put in an argv.
- **`tests/test_accounts.py`** — two complete pipelines through one database,
  checking that nothing leaks either way.
- **`tests/test_state.py`** — the v1 → v3 migration, row by row, including the
  backup file and the refusal to migrate with no account to assign rows to.
- **`tests/test_migrate.py`** — the layout migrations, and mostly what they
  refuse: to run unasked, to guess whose rows are whose, to overwrite a
  database, to drop an account nobody mentioned.
- **`tests/test_agent.py`** — the container loop: an agent runs its own jobs
  from its own database and nobody else's.
- **`tests/test_report.py`** — the MQTT publish path, with both paho-mqtt
  generations and `mosquitto_pub` injected rather than installed, so it runs
  offline with neither package present.
- **`tests/test_throughput.py`** — batched downloads and the circuit breaker:
  that a batch never spans two folders or repeats a filename, that a batch
  which dies halfway keeps what landed, that one bad file does not charge an
  attempt to the other 24, and that a byte-for-byte comparison of batched and
  unbatched output is identical.

`tests/fixtures/proton_list_real_*.json` are captured from real authenticated
listings (uids, emails and content hashes redacted, structure verbatim), so
discovery is pinned to observed output rather than guesswork. Replaying a real
2,109-entry listing through `pull` discovers all of them, records content sizes
(1.64 GB, where the encrypted sizes would have claimed 1.98 GB), captures a
sha1 for every file, spreads them over 13 capture-date buckets, and reports
zero new on a second pass.

---

## Design notes

Three deliberate departures from the plans:

- **Asset ids come from the REST API, not from parsing CLI stdout**, which has
  no stable machine-readable form. `push` calls `/assets/bulk-upload-check` —
  the endpoint the CLI itself uses for dedupe — before and after uploading:
  before, to identify true duplicates and skip sending them; after, to confirm
  what landed and collect the asset id. Checksums go out as sha1, and the
  client works out whether the server wants hex or base64, then remembers.
- **Backends are injected into the pipeline**, which is why the whole thing can
  be exercised against fakes without a network.
- **Downloads are batched, and a global circuit breaker stops a bad pass.**
  The CLI costs ~1.2 s of startup per invocation whatever it does, so one call
  per file is ~8 hours of pure process startup for a 25k-file library; sending
  25 paths per call makes that ~21 minutes. And a run of consecutive failures
  now stops the pass instead of burning an attempt on every remaining file —
  see [known-issues.md](docs/known-issues.md) items 1 and 2.
- **The web UI is `http.server` plus one HTML file**, where the v2 plan called
  for FastAPI and React/Vite. A status page for two people does not justify
  `pip install fastapi uvicorn` plus a Node toolchain and a committed bundle on
  a box that is awkward to debug. The endpoints are the contract and do not
  change if that trade ever stops making sense.

Everything the build plan had to guess about the Proton CLI — credentials,
sign-in, flags, JSON shape — has since been verified against the real binary
and corrected. [docs/proton-drive-cli.md](docs/proton-drive-cli.md) records
what it actually does and which assumptions were wrong.
