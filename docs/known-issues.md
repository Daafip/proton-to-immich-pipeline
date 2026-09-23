# Known issues and untested surfaces

An honest account of what is not solved. Items 1 and 2 were the two things
that mattered most at backfill scale and are now implemented — read item 1
before the first big backfill, because batching has not been run against real
Proton. Item 3 is the list of things that cannot be verified without the VM
and live services. **Items 5 and 6 are the v2 additions, and item 5 is the one
to read before using the delete queue for real.**

---

## 1. Backfill throughput — fixed, but unverified against real Proton

**Measured: ~1.2 s of CLI startup per invocation**, even when the command fails
immediately — it is Bun runtime plus SDK init, not transfer time. The download
loop used to spawn **one process per file**, which for ~25k files is roughly
**8 hours of process startup alone**, before a single byte moves.

`filesystem download path... localFolder` accepts **multiple paths per call**,
so `proton.download_batch_size` (default **25**) now sends a batch per
invocation. Counted against a realistic library shape:

| Library | `download_batch_size` | CLI invocations | Startup cost |
|---|---|---|---|
| 2,109 files / 13 folders | 1 | 2,109 | 42 min |
| | 25 | 91 | 2 min |
| 25,000 files / 150 folders | 1 | 25,000 | **8 h 20 m** |
| | 25 | 1,050 | 21 min |
| | 50 | 600 | 12 min |

The constraint the plan called out is enforced in `Pipeline._plan_batches`:
all files in one call land in the same destination folder, so a batch is
grouped **by source folder**, and **no two files in a batch share a name** —
a name can repeat across folders, and an undecryptable name falls back to a
node uid. Anything that would collide, or whose name is not a safe basename,
gets a batch of its own. With `--conflict-strategy skip` a collision would
silently keep the wrong file.

Two further behaviours worth knowing:

- **Partial success is expected.** A batch that dies halfway leaves real files
  on disk, so the destination folder is inspected rather than the exit code
  trusted; whatever landed is kept and not re-transferred.
- **A failed batch degrades to one call per file.** Otherwise a single
  unreadable file would charge an attempt to the other 24 and quarantine them
  in five nights.

`proton.timeout_sec` is a per-invocation budget, and it now scales with the
batch.

**Still unverified:** no batch has gone to real Proton. If the first backfill
night misbehaves, `proton.download_batch_size: 1` restores the old path
exactly, and `sync.py requeue` clears anything that failed. The safety net if
a batch ever returns the wrong bytes is the existing per-file check — size
must match what Proton reported, and the file is sha1'd before it is promoted
out of the scratch folder.

## 2. No circuit breaker — fixed

Per-asset exponential backoff did not help when everything failed at once: if
Proton started rate-limiting mid-backfill, every file failed in quick
succession and each burned an attempt. At `limits.max_attempts` (5) they reach
`quarantined`, so one bad night could mass-quarantine hundreds of files that
were never actually broken.

`limits.consecutive_failures` (default **25**, 0 disables) now stops a pass
after that many consecutive failures, in `download`, `push` and `verify`. A
single success resets the count, so scattered bad files do not trip it — only
a run of them does.

Tripping is deliberate, not an error path:

- The rows never reached keep their full attempt budget.
- Anything left mid-flight is rewound immediately — a pass that stops on
  purpose leaves no row looking `downloading`.
- It is recorded in `stats.aborted`, so the run exits **1** and both the timer
  and Home Assistant see it. Grep the journal for `circuit.tripped`.

## 3. What has and has not run live

A first end-to-end run happened on **2026-09-21** — Debian VM, `cli-drive@0.8.0`,
`immich-server:v3`, uploads via the immich-cli container. The stack is recorded
in [operations.md](operations.md#verified-on).

**Observed working against the real services:**

- **`auth login`, `filesystem list`, `filesystem download`** on 0.8.0, driven
  by `sync.py login`, `pull` and `download` — including the folder-destination
  behaviour, which until then was read off the CLI's usage string.
- **The immich-cli upload path** — `docker run --network host … upload
  --recursive /import --album-name …` against a live Immich, with assets
  landing in the library.

**Still only exercised against in-process fakes:**

- **Everything added in v2.** `reconcile`, the delete path, the web UI and the
  two-pipeline layout have 170+ tests between them (`test_delete.py`,
  `test_web.py`, `test_accounts.py`, `test_migrate.py`, `test_agent.py`) and
  have not touched a real Proton or Immich. The specific unknowns are items 5
  and 6 below.
- **The container layout.** The image builds, both roles start, and a job
  queued in the UI container was picked up and run by a pipeline container
  through the shared state volume — all verified locally, but against an
  unreachable Immich and with no Proton login. What is still unproven is the
  part that needs credentials: `docker compose run --rm <pipeline> login`
  completing a real sign-in, and an upload over `upload_mode: api`.
- **`verify` and `reap` against the server** — and with them `/assets/{id}`,
  `/server/ping`, `/users/me`, `/search/metadata`. Endpoint paths drift
  between Immich versions.
- **The `bulk-upload-check` checksum encoding.** `push` calls it before and
  after uploading, and the hex-then-base64 negotiation is still inference. It
  degrades to a `push.precheck_unavailable` warning rather than an error, so a
  green push does not prove it worked — grep the run for that event.
- **The REST upload path** (`immich.upload_mode: api`) and its multipart body.
- **The rclone fallback backend**, entirely.
- **MQTT against a real broker.** The payloads are built to the Home Assistant
  spec and the publish path is now properly tested — both paho-mqtt 1.x and
  2.x constructors, the `mosquitto_pub` fallback, retain/QoS on the state
  topic, and the failure logging — but all against injected clients, never a
  live broker. Neither client is installed by default on bare metal (the
  container image ships `paho-mqtt`); without one, reporting degrades to
  `status.json` only and says so (`mqtt.publish_failed`).

  Three bugs lived here until 2026-09-22, all silent: paho-mqtt 2.x could not
  be constructed at all (it made `callback_api_version` required), its failure
  took the `mosquitto_pub` fallback down with it because both shared one
  `try`, and nothing was logged on success — so a broker receiving nothing
  looked exactly like one receiving everything. `sync.py status --probe` also
  wrote `status.json` without publishing, which made it useless for testing a
  broker.

## 4. Operational assumptions

- **The service account** (bare metal only). The unit runs as `protonsync`,
  created by the install steps — the Immich docker stack runs as root and
  leaves no host user to borrow. It must own `/mnt/immich/staging`, and
  `immich.upload_mode: cli` also needs it in the `docker` group, which is
  root-equivalent on that host. Anything run by hand as root that writes into
  staging or `.state` leaves files the timer then cannot touch; that is the
  most likely first-boot failure.

  **The container layout removes most of this**: the image runs as
  `PIS_UID`/`PIS_GID`, `upload_mode: api` needs no docker group, and the only
  ownership that matters is that those ids own the two bind mounts. The
  equivalent first-boot failure there is a uid mismatch, which shows up
  immediately as `Permission denied` on the state volume rather than three
  days later.
- **CLI flags drift between builds.** Observed the hard way: `-c` for
  `filesystem download` works in 0.6.0 and is rejected by 0.8.0, which failed
  every download in a run until the template moved to the long
  `--conflict-strategy` both accept. `proton.cmd` templates plus the
  conflict-flag negotiation absorb that class of change; a rename of
  `filesystem list` or a change to its `--json` shape would not be absorbed,
  and would surface as discovery quietly finding nothing.
- **Proton cache growth.** 26 MB after listing ~2,100 entries; a full library is
  plausibly a few hundred MB, living in `staging/.proton` on the shared SSD.
- **No partial-file resume.** A 2 GB video failing at 90% restarts from zero,
  and with batching a failed batch re-fetches only the files that did not
  land, not the whole batch.
- **`verify` does one HTTP GET per asset.** 25k sequential calls is slow,
  though harmless.
- **Proton fair-use behaviour under sustained load is unknown.** Downloads are
  sequential, which helps; see item 2 for what happens if limits are hit.

## 5. The delete path has never touched a real Proton

`cli-drive@0.8.0` has `filesystem trash`, `restore`, `delete` and
`empty-trash` — that much is verified from `--help`. What is **not** verified:

- **Whether `trash` works on the Photos section.** Proton's support docs say
  items there cannot be deleted from desktop apps, and a third-party GUI
  wrapper reports delete/rename/restore as unavailable in the Photos view.
  Hence `delete.action` per account, defaulting to `mark_only`. A
  `/my-files/...` root is the expected `execute` case, and that is what
  `proton.roots` ships with.
- **What `filesystem info` prints for a trashed node.** No sample was
  captured. If it still resolves a trashed node by path with the same uid, the
  post-trash verification would report `delete_failed` on a deletion that
  actually worked — a false alarm, not a lost photo, and the row stays visible
  with the reason. If it errors instead, `looks_like_missing_node` has to
  recognise the wording; anything it does not recognise surfaces as a failure
  rather than a silent success.
- **Whether `trash` is synchronous.** The verification re-resolves immediately.
  If Proton's trash is eventually consistent, expect spurious
  `delete_failed` rows on the first attempt.

**So: run it with `--dry-run` first, then with `--yes` on two or three junk
files in `/my-files`, and read the `deletions` table before trusting it with
anything that matters.** The design limits the damage — trash rather than
delete, one path per invocation, re-resolve before and verify after, a batch
cap of 50, and an append-only audit table — but none of that is a substitute
for trying it.

`reconcile` is the safe half and can be left on: it cannot call a Proton
mutation at all.

## 6. Untested corners of the v2 additions

- **`POST /trash/restore/assets`.** `push` calls this when Immich recognises a
  checksum but the matching asset is in the trash — without it the photo is
  not in the library, and re-uploading cannot help, because the trashed asset
  still owns the checksum. The endpoint has moved between Immich versions and
  has not been exercised against a live server here. A failure is reported
  (`push.restore_failed`) and the asset is failed rather than recorded as
  uploaded, so the bad outcome is a stuck asset, not a phantom one.


- **Immich's trash query.** `POST /search/metadata` with `isTrashed: true`,
  paginated by `page`/`nextPage`, one request per `type`. The field names and
  the pagination shape are read off the API, not observed. If they are wrong,
  `reconcile` finds nothing and logs `reconcile.unavailable` or simply reports
  zero — a quiet failure, so check `staged_deletes` after deliberately
  trashing something.
- **The `systemd` job runner.** `systemctl start` on a `Type=oneshot` unit is
  expected to block until the unit finishes and exit with its status, which is
  what the worker relies on. Untested here, and it needs the sudoers entry.
  `job_runner: subprocess` is the default and needs neither.
- **Two Proton sessions in two cache dirs.** The isolation argument is sound —
  `unsafe_file` keeps the session in `PROTON_DRIVE_CACHE_DIR` — but the CLI's
  keyring path uses one fixed service name, and nobody has confirmed that
  nothing else is shared. [operations.md](operations.md#setting-it-up) has the
  five-command test to run before writing any config.
- **The UI in a browser.** The page is served, its JS parses and every
  endpoint it calls is tested over real HTTP, but no browser has rendered it.
- **`/api/staged-deletes.csv` filename handling.** Account names are validated
  to `[A-Za-z0-9_.-]`, so the `Content-Disposition` cannot be broken by one,
  but no exotic name has been tried.

## 7. Decisions still open

- **Album strategy** (`immich.album_strategy`) — settle before the first real
  push; changing it later means re-tagging.
- **`reap.keep_days`** — 7 to start, 0 once trusted.
- **`delete.action`** — `mark_only` until item 5 is settled.
- **Immich's trash retention.** The default ~30 days is what makes the staged
  list necessary. Extending it (Administration → Settings → Trash) buys time
  to look a staged photo up before deciding; disabling the auto-empty means
  the two lists never disagree.
- **Whether the UI is reachable from outside the LAN.** `web.password_hash`
  gives you the auth; TLS is a reverse proxy's job, not this process's.

---

## Out of scope, worth scheduling separately

The SSD is a single copy, and Immich's own docs are explicit that this is not a
backup. Postgres lives on the VM disk and holds faces, embeddings and albums —
expensive to rebuild. Plan a `pg_dump` plus a second copy of `data/` elsewhere,
independently of this pipeline.
