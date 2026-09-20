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

## 3. Never executed against live services

Everything is tested against in-process fakes. These have never run for real:

- **Immich REST paths and response shapes** — `/server/ping`, `/users/me`,
  `/assets/{id}`, `/assets/bulk-upload-check`, `/search/metadata`,
  `POST /assets`. Endpoint paths do drift between Immich versions.
- **The `bulk-upload-check` checksum encoding.** The client tries hex, falls
  back to base64 and remembers which worked — sound, but inference.
- **immich-cli docker flags** — `--album`, `--album-name`, `--concurrency`,
  `--dry-run` are taken from the documented interface, not observed.
- **The REST upload path** (`immich.upload_mode: api`) and its multipart body.
- **An actual `filesystem download`.** The folder-destination behaviour is read
  off the CLI's own usage string and help text.
- **The rclone fallback backend**, entirely.
- **MQTT discovery payloads.** Built to the Home Assistant spec and unit-tested
  for shape, but never sent to a broker. Neither `paho-mqtt` nor
  `mosquitto_pub` is installed by default — without one, reporting quietly
  degrades to `status.json` only.

What *has* been verified against reality: the Proton CLI interface and its
`--json` schema, replayed from real listings — see
[proton-drive-cli.md](proton-drive-cli.md).

## 4. Operational assumptions

- **`User=immich` in the systemd unit** must exist and own
  `/mnt/immich/staging`. The Immich docker stack commonly runs as root, so this
  is the most likely first-boot failure.
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
