# Known issues and untested surfaces

An honest account of what is not solved. Items 1 and 2 matter most at backfill
scale; item 3 is simply the list of things that cannot be verified without the
VM and live services.

---

## 1. Backfill throughput

**Measured: ~1.2 s of CLI startup per invocation**, even when the command fails
immediately — it is Bun runtime plus SDK init, not transfer time. The download
loop currently spawns **one process per file**. For ~25k files that is roughly
**8 hours of process startup alone**, before a single byte moves.

The fix is available in the CLI signature: `filesystem download path...
localFolder` accepts **multiple paths per call**. Batching ~50 files per
invocation would cut the overhead by ~50×.

The constraint to respect when implementing it: all files in one call land in
the same destination folder, so a batch must be grouped **by source folder**,
where names are unique and cannot collide. With `-c skip`, a collision would
silently keep the wrong file.

Not implemented.

## 2. No circuit breaker

Per-asset exponential backoff exists. A global one does not.

If Proton starts rate-limiting mid-backfill, every file fails in quick
succession and each burns an attempt. At `limits.max_attempts` (5) they reach
`quarantined` — so one bad night could mass-quarantine hundreds of files that
were never actually broken. `sync.py requeue` recovers them, but only after
someone notices.

What is missing: a consecutive-failure threshold that aborts the pass and
leaves the remaining attempts intact.

Not implemented.

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

- **`verify` and `reap` against the server** — and with them `/assets/{id}`,
  `/server/ping`, `/users/me`, `/search/metadata`. Endpoint paths drift
  between Immich versions.
- **The `bulk-upload-check` checksum encoding.** `push` calls it before and
  after uploading, and the hex-then-base64 negotiation is still inference. It
  degrades to a `push.precheck_unavailable` warning rather than an error, so a
  green push does not prove it worked — grep the run for that event.
- **The REST upload path** (`immich.upload_mode: api`) and its multipart body.
- **The rclone fallback backend**, entirely.
- **MQTT discovery payloads.** Built to the Home Assistant spec and unit-tested
  for shape, but never sent to a broker. Neither `paho-mqtt` nor
  `mosquitto_pub` is installed by default — without one, reporting quietly
  degrades to `status.json` only.

## 4. Operational assumptions

- **The service account.** The unit runs as `protonsync`, created by the
  install steps — the Immich docker stack runs as root and leaves no host user
  to borrow. It must own `/mnt/immich/staging`, and `immich.upload_mode: cli`
  also needs it in the `docker` group, which is root-equivalent on that host.
  Anything run by hand as root that writes into staging or `.state` leaves
  files the timer then cannot touch; that is the most likely first-boot
  failure.
- **CLI flags drift between builds.** Observed the hard way: `-c` for
  `filesystem download` works in 0.6.0 and is rejected by 0.8.0, which failed
  every download in a run until the template moved to the long
  `--conflict-strategy` both accept. `proton.cmd` templates plus the
  conflict-flag negotiation absorb that class of change; a rename of
  `filesystem list` or a change to its `--json` shape would not be absorbed,
  and would surface as discovery quietly finding nothing.
- **Proton cache growth.** 26 MB after listing ~2,100 entries; a full library is
  plausibly a few hundred MB, living in `staging/.proton` on the shared SSD.
- **No partial-file resume.** A 2 GB video failing at 90% restarts from zero.
- **`verify` does one HTTP GET per asset.** 25k sequential calls is slow,
  though harmless.
- **Proton fair-use behaviour under sustained load is unknown.** Downloads are
  sequential, which helps; see item 2 for what happens if limits are hit.

## 5. Decisions still open

- **Album strategy** (`immich.album_strategy`) — settle before the first real
  push; changing it later means re-tagging.
- **`reap.keep_days`** — 7 to start, 0 once trusted.

---

## Out of scope, worth scheduling separately

The SSD is a single copy, and Immich's own docs are explicit that this is not a
backup. Postgres lives on the VM disk and holds faces, embeddings and albums —
expensive to rebuild. Plan a `pg_dump` plus a second copy of `data/` elsewhere,
independently of this pipeline.
