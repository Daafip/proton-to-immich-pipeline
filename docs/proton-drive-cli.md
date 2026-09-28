# The Proton Drive CLI, as it actually behaves

Everything here was verified against **`cli-drive@0.6.0`** (SDK `js@0.19.2`) by
running the binary and by replaying real `--json` output. It is recorded
because the build plan had to guess at most of it, and several guesses were
wrong in ways that would have broken the pipeline silently.

**`cli-drive@0.8.0` is what the pipeline has actually been run on** — sign-in,
discovery and downloads all against live Proton Drive on 2026-09-21. The
differences found are called out below; the `--json` shape was unchanged.

Re-check this page if you upgrade the CLI. **The flags do drift**: `0.8.0`
rejects the `-c` alias that `0.6.0` documents for `filesystem download`, though
it keeps the long `--conflict-strategy`. That is why the argument templates
live in config and the backend can renegotiate the flag at runtime — see
below.

---

## Command surface

```
auth login | auth logout
filesystem list [-t TYPE] path
filesystem info path
filesystem download [-c STRATEGY] [-f STRATEGY] [-d STRATEGY] path... localFolder
filesystem upload [-f STRATEGY] [-d STRATEGY] [-t] localPath... parentPath
filesystem create-folder | rename | copy | move
filesystem trash path...       | restore path...
filesystem delete path...      | empty-trash
sharing … | invitation … | album … | photo timeline | photo upload | photo download
General options: -h|--help  -j|--json  -v|--verbose
```

Four things follow from that signature:

- **`download` takes a destination *folder*, not a file path.** The CLI names
  the file itself; the caller renames afterwards. Each download therefore gets
  its own scratch folder, so a skipped conflict can never promote another
  node's leftover file.
- **A conflict strategy matters for unattended use, and only the long
  spelling is portable.** Without a strategy the CLI *prompts*, and a nightly
  run must never wait on a prompt. `0.6.0` documents `-c, --conflict-strategy`;
  **`0.8.0` rejects `-c`** (`Unknown option '-c'`) while keeping
  `--conflict-strategy`, `--file-conflict-strategy` and
  `--folder-conflict-strategy`. The default template therefore uses
  `--conflict-strategy skip`, which both builds accept.
  If some future build rejects that too, `ProtonCliBackend._run_download`
  retries with `--conflict-strategy`, then with no flag at all, and remembers
  the first that works — one probe per process, logged as
  `proton.conflict_flag_fallback`. Dropping the flag is safe because every node
  downloads into its own empty scratch folder; belt-and-braces, the CLI runs
  with stdin on `/dev/null` so no prompt can hang a run.
- **`download` accepts multiple paths per call.** Not yet exploited — see
  [known-issues.md](known-issues.md#1-backfill-throughput--fixed-but-unverified-against-real-proton).
- **`list` has no recursive flag**, so discovery is a breadth-first walk,
  depth-capped by `proton.max_depth`.

There is **no `auth status` subcommand**. The session probe is
`filesystem list /` (`proton.auth_probe_path`).

### Deletion (verified present on 0.8.0)

The build plan had to assume the CLI could not delete at all. It can:

| Command | Behaviour, per its own `--help` |
|---|---|
| `filesystem trash path...` | "Moves items to trash. Does not permanently delete." **Reversible** — this is the only one the pipeline calls. |
| `filesystem restore path...` | Undoes a trash. |
| `filesystem delete path...` | "Permanently deletes trashed items only." Never called. |
| `filesystem empty-trash` | Never called. |

`filesystem info path` is the pre-flight check the delete path is built on: it
prints the node currently at a path, so the staged `uid` can be compared with
whatever is there now before anything is touched. A path since reused for a
different file is then skipped rather than deleted.

**Verified not to work on the Photos section (`/photos`):** the
`filesystem` verbs reject that path type outright; see
[the Photos section](#the-photos-section-photos). Earlier notes, for context:
Proton's support docs say items there cannot be deleted from desktop apps, and
a third-party GUI wrapper reports delete/rename/restore as unavailable in the
Photos view. That is why `delete.action` is per account and defaults to
`mark_only`; a `/my-files/...` root is the `execute` case.

There is no documented trashed/`isTrashed` field in `filesystem info` output,
so the pipeline treats **"no longer resolvable at that path"** as the primary
signal that a trash succeeded, and checks a trash-ish flag only as a bonus.

## Paths

Proton's own root is `/my-files`; `/` lists the top-level sections. Paths are
always posix, and a literal `/` inside a node name is backslash-escaped.

```bash
proton-drive filesystem list /                 # sections
proton-drive filesystem list /my-files/Photos  # e.g. one folder per year
```

`proton.roots` is a list and every entry is walked recursively, so one parent
folder takes everything beneath it. Naming folders individually is the way to
spread a backfill over several nights:

```yaml
proton:
  roots:
    - "/my-files/Photos/Photos from 2025"
    - "/my-files/Photos/Albums 2019 - 2026 google"
```

Spaces and hyphens need no quoting (quote only if a name contains a colon).
Nothing is passed through a shell, so `Photos from 2024` stays one argument.

With `album_strategy: folder` these folder names become the Immich album names.

### The Photos section (`/photos`)

The timeline the phone apps back up into is **not** a folder under
`/my-files`. `/my-files/Photos` is just an ordinary folder that happens to be
called Photos. The `filesystem` verbs refuse the real section outright:

```
$ proton-drive filesystem list /photos
Path type photos is not supported
```

It is reached with `photo timeline` and `photo download` instead (verified on
0.8.0, 2026-09-28). Put `/photos` on its own in `proton.roots` and the backend
switches to those verbs:

```yaml
proton:
  roots:
    - /photos
```

- **`photo timeline --json`** returns only `nodeUid`, `captureTime` and `tags`
  per photo: no name, size or digest, which is not enough to sync.
  **`photo timeline -d --json`** loads the details, and each entry then has the
  same shape as a `filesystem list` file entry, with `"type": "photo"` and an
  extra `photo` block (`captureTime`, `contentHash`, `albums`,
  `relatedPhotoNodeUids`). Sample: `tests/fixtures/proton_photo_timeline.json`.
- **The timeline is flat.** Every photo has the same `parentUid`, so paths are
  `/photos/<name>` and `/photos/<subfolder>` is rejected. There is no
  pagination: one call returns everything, ~2 KB of JSON per photo (5.5 MB for
  roughly 2,700 photos), under its own `proton.timeline_timeout_sec`
  (default 3600).
- **Names repeat.** The CLI's own help says so. `photo download` accepts both
  `/photos/<name>` and `/photos/<full uid>`. The pipeline always uses the
  **full** uid (`<volume>~<node>`, both halves; a truncated uid answers
  `Photo not found`). The file still lands under the photo's name, and the
  usual batching rule (no two equal names per batch) keeps duplicates apart.
- **`photo download` still takes `-c`** on 0.8.0. The default is
  `-c rename`, so a collision shows up as a missing file, not a silently kept
  wrong one. Override with `proton.cmd.photo_download`.
- **No delete.** `filesystem info` and `filesystem trash` reject `/photos`
  paths and there is no photo equivalent, so with `delete.action: execute`
  those rows are marked `delete_failed` with a message pointing at
  `mark_only`. Use `mark_only` for an account that syncs `/photos`.

---

## Environment

| Variable | Effect |
|---|---|
| `PROTON_DRIVE_CACHE_DIR` | Sets cache, app data **and** log dir to one directory. |
| `PROTON_DRIVE_CREDENTIALS_STORE` | `keychain` (default) · `unsafe_file` · `pass`. |
| `PROTON_DRIVE_LOG_LEVEL` | `DEBUG` (default) · `INFO` · `WARNING` · `ERROR`. |
| `PROTON_DRIVE_UNSAFE_CACHE` | Accepts `yes`/`y`/`1`/`true`. |
| `PROTON_DRIVE_BASE_URL` | Defaults to `drive-api.proton.me`. |

`PROTON_DRIVE_UNSAFE_SECRETS` — which the build plan expected — **does not
exist in 0.6.0**. The name was real in 0.5.0: `LouisBrunner/ha-proton-drive`
sets it and pins `CLI_VERSION = "0.5.0"`. It was renamed to
`PROTON_DRIVE_CREDENTIALS_STORE` between the two releases.

Two defaults are actively wrong for an unattended service, and the config
overrides both:

- **Log level defaults to `DEBUG`**, and with `PROTON_DRIVE_CACHE_DIR` set the
  log lands in `staging/.proton` with no rotation of its own — unbounded growth
  on the same SSD Immich uses. Pinned to `WARNING` via `proton.cli_log_level`.
- **Credentials default to `keychain`**, which needs an unlocked keyring.

### Credentials

| Value | Where the session lives | Headless? |
|---|---|---|
| `keychain` (default) | libsecret / Secret Service | needs an unlocked keyring |
| `unsafe_file` | plaintext file in the cache dir | **yes — no keyring at all** |
| `pass` | the Unix `pass` store (GPG) | yes |

`config.example.yaml` ships `unsafe_file`, so `gnome-keyring` should be
unnecessary on the VM. The session token is then a plaintext file in
`staging/.proton`, created `chmod 700` — leave it that way, and treat that
directory as a live credential when backing the disk up.

The keyring route fails exactly where a timer needs it to work: on a desktop
with D-Bus running and a session already signed in, a *non-interactive* shell
still gets `You need to login first`, because the collection is locked.

---

## Signing in

**There is no loopback callback.** `auth login --json` prints one line —

```json
{"signInUrl":"https://account.proton.me/desktop/login?app=drive&pv=3#payload=..."}
```

— and then waits. The payload is in the URL **fragment**, which never leaves
the browser: the account page hands the session to Proton's API and the CLI
polls for it. Nothing listens on a local port and **no `ssh -L` forwarding is
needed**. The CLI's own help says so: *"you can use different device to sign
in"*.

`sync.py login` reads that URL and serves it as a redirect on a LAN port so a
phone can reach it — see [bare-metal.md](bare-metal.md#signing-in), or
`docker compose run --rm <pipeline> login`.

### Detecting an expired session

An expired session prints **`You need to login first`** on stdout, exit 1, and
**not JSON even under `--json`**. Matching that string is what turns a dead
session into `auth_ok: false` rather than a generic error — which matters,
because a silently expired session is the failure most likely to stall the
pipeline. `ha-proton-drive` matches the same string.

---

## The `--json` schema

Output is a JSON array of objects. Captured samples live in
`tests/fixtures/proton_list_real_*.json` (uids, emails and content hashes
redacted; structure verbatim).

**There is no `path` field**, and the node id is **`uid`**. Paths are built
from the parent path plus the name.

A folder entry:

```json
{"uid": "...", "parentUid": "...",
 "name": {"ok": true, "value": "Photos from 2017"},
 "type": "folder", "folder": {"isImported": false},
 "creationTime": "2026-02-15T16:02:56.000Z",
 "modificationTime": "2026-02-15T16:02:56.000Z",
 "isShared": false, "isSharedPublicly": false, "directRole": "admin",
 "ownedBy": {...}, "keyAuthor": {...}, "nameAuthor": {...},
 "treeEventScopeId": "..."}
```

A file entry adds `mediaType`, `totalStorageSize` and `activeRevision`:

```json
{"type": "file", "mediaType": "image/jpeg", "totalStorageSize": 763203,
 "activeRevision": {"ok": true, "value": {
    "claimedSize": 604740, "storageSize": 763203,
    "claimedModificationTime": "...",
    "claimedDigests": {"sha1": "24372d...", "sha1Verified": false},
    "claimedAdditionalMetadata": {
      "Media": {"Width": 0, "Height": 0},
      "Camera": {"Device": "Moto G (5S)", "CaptureTime": "2017-12-27T18:55:15.000Z"}}}}}
```

### Four traps in that schema

**1. `name` is a `{"ok", "value"}` envelope, not a string.** Names are
encrypted and decryption can fail. A naive parser writes `{'ok': True, ...}` to
disk as the filename. Everything derived from encrypted metadata arrives this
way, so unwrapping is applied to every field lookup. When `ok` is false the
node falls back to its `uid`, which the CLI accepts in paths; local filenames
are sanitised, because a uid is base64 and can contain `/`.

**2. `totalStorageSize` is the *encrypted* size.** The content size is
`activeRevision.value.claimedSize`. The encrypted one runs ~25% larger —
763,203 vs 604,740 bytes on a sample photo. Using it fails the post-download
size check on *every* file, and inflates `max_bytes` accounting by a fifth.

**3. Timestamps describe the import, not the photo.** A bulk migration stamps
`creationTime`, `modificationTime` *and* `claimedModificationTime` with the
migration date. The real date is `claimedAdditionalMetadata.Camera.CaptureTime`.
Staging buckets use capture time — on one sample folder that is 13 directories
instead of one holding 2,109 files. Change detection still compares
`modificationTime`, which is correct: it tracks the node, not the photo.

**4. `claimedDigests.sha1` is the uploader's claim.** `sha1Verified` is false,
so it is not a server guarantee. It is still the same algorithm Immich dedupes
with, which is what makes [precheck](operations.md#skipping-what-immich-already-has)
possible. It is stored as `claimed_sha1` and a mismatch after download is
logged, but the locally computed digest is what Immich is given.

`mediaType` (`image/jpeg`, `video/mp4`) is the primary filter; file extensions
are the fallback when it is absent.
