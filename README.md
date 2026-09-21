# proton-to-immich-pipeline

One-way, incremental, resumable sync: **Proton Drive → staging → Immich**.
Python 3.11+, standard library only. Runs unattended under systemd and reports
its health to Home Assistant.

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

**Status:** `login → pull → download → push` has run end to end on a Debian VM
against `cli-drive@0.8.0` and `immich-server:v3` (2026-09-21). `verify`, `reap`,
the REST upload mode and MQTT have not yet run against live services — see
[docs/known-issues.md](docs/known-issues.md#3-what-has-and-has-not-run-live).

| Subcommand | What it does |
|---|---|
| `login` | Sign in to Proton, serving a phone-friendly redirect link. |
| `pull` | Walk the configured roots, record new/changed nodes. No transfers. |
| `precheck` | Mark files Immich already holds, so they are never downloaded. |
| `download` | Fetch `discovered` nodes into `staging/ready/<yyyy-mm>/`, sha1-checked. |
| `push` | Upload to Immich, record asset ids, flag server-side duplicates. |
| `verify` | Confirm each asset exists server-side and its checksum matches. |
| `reap` | Delete verified local files once past the retention grace period. |
| `run` | All of the above, in order. This is what the timer runs. |
| `status` | Print pipeline health (`--json` for machine output). |
| `requeue` | Put `failed` / `quarantined` rows back in play. |

Exit codes: **0** ok · **1** partial failure · **2** auth failure · **3** lock held ·
**4** config fault (nothing was attempted; fix the config and re-run).

---

## Documentation

| | |
|---|---|
| [docs/operations.md](docs/operations.md) | Install, configure, sign in, run the phases, systemd, backfill, Home Assistant, state model, troubleshooting. |
| [docs/proton-drive-cli.md](docs/proton-drive-cli.md) | How `cli-drive` actually behaves (verified on 0.6.0, flags re-checked on 0.8.0): command surface, environment, sign-in, the `--json` schema and its four traps. |
| [docs/known-issues.md](docs/known-issues.md) | What is not solved, and what has and has not run against live services. |
| `proton-to-immich-pipeline-build-plan.md` | The original plan this was built from. |

---

## Quick start

```bash
sudo cp config.example.yaml /etc/proton-to-immich-pipeline/config.yaml
export PIS_CONFIG=/etc/proton-to-immich-pipeline/config.yaml
export IMMICH_API_KEY=...            # Immich → Account Settings → API Keys

python3 sync.py login                # open the printed link on any device
python3 sync.py pull --dry-run       # counts only, writes nothing
python3 sync.py run                  # pull, download, push, verify, reap
```

The three settings you must provide:

```yaml
proton:
  roots: ["/my-files/Photos"]    # a list; each entry is walked recursively
immich:
  url: http://<vm-ip>:2283/api   # the /api suffix is mandatory
  api_key: ""                    # leave empty; use IMMICH_API_KEY instead
```

Full deployment, including systemd and the backfill, is in
[docs/operations.md](docs/operations.md). **Read
[docs/known-issues.md](docs/known-issues.md) before a large backfill.**

---

## Layout

```
sync.py                 CLI entry point
src/config.py           config + a PyYAML-free fallback parser
src/log.py              one JSON line per state transition
src/state.py            SQLite schema, transitions, resume
src/proton.py           Proton CLI backend, rclone fallback backend
src/login.py            sign-in flow + the phone redirect page
src/immich.py           REST client + docker immich-cli uploader
src/pipeline.py         phase orchestration (backends injected, so testable)
src/report.py           status.json + MQTT discovery
systemd/                service + nightly timer
tests/                  186 tests, no network, no Docker
```

`config.py`, `log.py`, `login.py` and `pipeline.py` are additions to the layout
the build plan sketched; the rest matches it.

---

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

186 tests, no network and no Docker. The Proton backend and Immich server are
faked in-process, so `pull → download → push → verify → reap` runs end to end,
including the failure paths: truncated transfers, checksum mismatches, sessions
expiring mid-run, killed runs resuming, quarantine after repeated failures.

`tests/fixtures/proton_list_real_*.json` are captured from real authenticated
listings (uids, emails and content hashes redacted, structure verbatim), so
discovery is pinned to observed output rather than guesswork. Replaying a real
2,109-entry listing through `pull` discovers all of them, records content sizes
(1.64 GB, where the encrypted sizes would have claimed 1.98 GB), captures a
sha1 for every file, spreads them over 13 capture-date buckets, and reports
zero new on a second pass.

---

## Design notes

Two deliberate departures from the build plan:

- **Asset ids come from the REST API, not from parsing CLI stdout**, which has
  no stable machine-readable form. `push` calls `/assets/bulk-upload-check` —
  the endpoint the CLI itself uses for dedupe — before and after uploading:
  before, to identify true duplicates and skip sending them; after, to confirm
  what landed and collect the asset id. Checksums go out as sha1, and the
  client works out whether the server wants hex or base64, then remembers.
- **Backends are injected into the pipeline**, which is why the whole thing can
  be exercised against fakes without a network.

Everything the build plan had to guess about the Proton CLI — credentials,
sign-in, flags, JSON shape — has since been verified against the real binary
and corrected. [docs/proton-drive-cli.md](docs/proton-drive-cli.md) records
what it actually does and which assumptions were wrong.
