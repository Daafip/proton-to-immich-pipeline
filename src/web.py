"""The web UI: status per account, force sync, and the delete queue.

Standard library only, like the rest of this. `http.server` plus one HTML file
is not the fashionable answer, but the whole point of this project is that it
runs unattended on a box that is awkward to debug, and a status page for two
people does not justify a framework plus a Node build and a committed bundle.
The endpoints are the contract; swapping the implementation later changes
nothing the frontend can see.

Three things are load-bearing:

* **A request never runs a sync.** A handler that shelled out to a download
  would time out, and a page refresh would start a second one. Every action
  becomes a row in `jobs`, and one worker thread runs them one at a time.
* **Reads never block the writer.** Status queries use a `mode=ro` connection,
  so a page refresh during a backfill sees a WAL snapshot instead of queueing
  behind the pipeline's write lock.
* **Nothing destructive is decided here.** `/api/staged-deletes/execute`
  passes row *ids* to `sync.py delete-staged`, which resolves them against the
  database and re-checks every node against Proton. A path in a request body
  never reaches the CLI.

Immich API keys stay server-side: no endpoint returns one, and the page has no
build step that could inline one.
"""

from __future__ import annotations

import base64
import hmac
import http.server
import json
import os
import re
import secrets
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from hashlib import scrypt, sha256
from pathlib import Path
from typing import Any

from . import log, report, state

ROOT = Path(__file__).resolve().parent.parent
SYNC_PY = ROOT / "sync.py"
STATIC_DIR = ROOT / "web"

# An account name reaches a systemd unit name and an argv, so it is checked
# against this even though it comes from the config rather than a request.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

SESSION_COOKIE = "pis_session"

# scrypt parameters. N=2**15 is ~100 ms per attempt on the VM, which is the
# point: it makes guessing the shared password expensive without making a
# legitimate login feel slow.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1


# --------------------------------------------------------------------------
# passwords and sessions
# --------------------------------------------------------------------------

def hash_password(password: str, salt: bytes | None = None) -> str:
    """`scrypt$n$r$p$salt$key`, so the config holds no password."""
    salt = salt or secrets.token_bytes(16)
    key = scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                 r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)
    return (f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}"
            f"${salt.hex()}${key.hex()}")


def verify_password(stored: str, password: str) -> bool:
    """Constant-time compare, and a malformed hash is simply a failed login."""
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                           n=int(n), r=int(r), p=int(p),
                           dklen=len(key_hex) // 2, maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(candidate.hex(), key_hex)


def load_secret(cfg) -> bytes:
    """The cookie signing key.

    Kept in a file so sessions survive a restart -- a regenerated key would
    log Mirjam out every time the service is reloaded. Generated on first use
    with 0600, because holding it is enough to mint a session.
    """
    configured = cfg.get("web.secret_file")
    path = Path(configured) if configured else cfg.state_dir / "web-secret"
    try:
        existing = path.read_bytes().strip()
        if len(existing) >= 32:
            return existing
    except OSError:
        pass
    secret = secrets.token_hex(32).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(secret)
    log.info("web.secret_created", path=str(path))
    return secret


def sign_session(secret: bytes, expires_at: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at}).encode()).decode().rstrip("=")
    mac = hmac.new(secret, payload.encode(), sha256).hexdigest()
    return f"{payload}.{mac}"


def check_session(secret: bytes, token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, mac = token.rpartition(".")
    expected = hmac.new(secret, payload.encode(), sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return False
    try:
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return False
    return float(data.get("exp", 0)) > (now if now is not None else time.time())


# --------------------------------------------------------------------------
# the job worker
# --------------------------------------------------------------------------

class JobWorker(threading.Thread):
    """Runs queued jobs, one at a time, by shelling out to sync.py.

    Out of process on purpose. `sync.py` already owns the flock, the exit
    codes and the structured logging, and a download that wedges takes a
    subprocess with it rather than the web server.
    """

    daemon = True

    def __init__(self, cfg, accounts: dict[str, Any], config_path: str | None):
        super().__init__(name="pis-job-worker")
        self.cfg = cfg
        self.accounts = accounts
        self.config_path = config_path
        self.runner = str(cfg.get("web.job_runner", "subprocess"))
        self.timeout = int(cfg.get("web.job_timeout_sec", 28800))
        self.db_path = cfg.db_path
        self._stop = threading.Event()
        self.conn: sqlite3.Connection | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - exercised via integration
        self.conn = state.connect(self.db_path)
        try:
            while not self._stop.is_set():
                try:
                    job = state.claim_job(self.conn)
                except sqlite3.Error as exc:
                    log.warn("web.job_claim_failed", detail=str(exc)[:200])
                    self._stop.wait(2.0)
                    continue
                if job is None:
                    self._stop.wait(1.0)
                    continue
                self.execute(job)
        finally:
            if self.conn:
                self.conn.close()

    def argv_for(self, job) -> list[str]:
        """The command for one job.

        The account name is looked up in the config allowlist, never
        interpolated from a request: `self.accounts[...]` raises for anything
        the config does not name. Nothing here runs through a shell.
        """
        account = str(job["account"])
        if account not in self.accounts or not SAFE_NAME.match(account):
            raise KeyError(f"unknown account {account!r}")
        job_type = str(job["type"])

        if job_type == "sync" and self.runner == "systemd":
            # `systemctl start` on a Type=oneshot unit blocks until it
            # finishes and exits with the unit's status, which is exactly the
            # contract the subprocess runner has.
            unit = str(self.cfg.get(
                "web.systemd_unit",
                "proton-to-immich-pipeline@{account}.service")
            ).format(account=account)
            return [str(self.cfg.get("web.sudo", "sudo")),
                    "-n", str(self.cfg.get("web.systemctl", "systemctl")),
                    "start", unit]

        argv = [sys.executable, str(SYNC_PY)]
        if self.config_path:
            argv += ["-c", self.config_path]
        argv += ["--account", account]

        if job_type == "sync":
            return argv + ["run"]
        if job_type == "reconcile":
            return argv + ["reconcile"]
        if job_type == "delete":
            payload = json.loads(job["payload"] or "{}")
            argv.append("delete-staged")
            # No --yes means a dry run, whatever else was asked for.
            if not payload.get("dry_run"):
                argv.append("--yes")
            if payload.get("limit"):
                argv += ["--limit", str(int(payload["limit"]))]
            argv += [str(int(i)) for i in (payload.get("ids") or [])]
            return argv
        raise ValueError(f"unknown job type {job_type!r}")

    def execute(self, job) -> None:
        job_id = int(job["id"])
        try:
            argv = self.argv_for(job)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            state.finish_job(self.conn, job_id, 4, f"rejected: {exc}")
            log.error("web.job_rejected", job=job_id, detail=str(exc))
            return

        log.info("web.job_start", job=job_id, type=job["type"],
                 account=job["account"])
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=self.timeout, check=False,
                                  cwd=str(ROOT))
            detail = log.condense(f"{proc.stderr}\n{proc.stdout}", 3000)
            code = proc.returncode
        except FileNotFoundError as exc:
            code, detail = 4, f"{argv[0]} not found: {exc}"
        except subprocess.TimeoutExpired:
            code, detail = 1, f"timed out after {self.timeout}s"
        state.finish_job(self.conn, job_id, code, detail)
        log.info("web.job_done", job=job_id, exit_code=code,
                 seconds=round(time.monotonic() - started, 1))


# --------------------------------------------------------------------------
# the API
# --------------------------------------------------------------------------

class Api:
    """Everything the handler needs, with no HTTP in it -- so the routes can
    be tested by calling them."""

    def __init__(self, cfg, require_auth: bool = True):
        self.cfg = cfg
        self.accounts = {a.account_name: a for a in cfg.accounts}
        for name in self.accounts:
            if not SAFE_NAME.match(name):
                raise ValueError(
                    f"account name {name!r} is not usable in a unit name or "
                    f"argv; use letters, digits, dot, dash or underscore")
        self.secret = load_secret(cfg)
        self.password_hash = str(
            os.environ.get("PIS_WEB_PASSWORD_HASH")
            or cfg.get("web.password_hash", "") or "")
        plaintext = os.environ.get("PIS_WEB_PASSWORD")
        if plaintext and not self.password_hash:
            # Hashed here and not written anywhere; the env var is the source.
            self.password_hash = hash_password(plaintext)
        self.require_auth = bool(require_auth and self.password_hash)
        self.auth_configured = bool(self.password_hash)
        self.session_hours = int(cfg.get("web.session_hours", 168))

    # -- db ----------------------------------------------------------------
    def reader(self) -> sqlite3.Connection:
        """A fresh read-only connection per request.

        One connection per thread is the simplest way to stay inside
        sqlite3's threading rules, and on a WAL database opening one is cheap.
        """
        return state.connect_readonly(self.cfg.db_path)

    def writer(self) -> sqlite3.Connection:
        return state.connect(self.cfg.db_path)

    def account(self, name: str | None):
        if name is None:
            if len(self.accounts) == 1:
                return next(iter(self.accounts.values()))
            raise KeyError("account is required")
        if name not in self.accounts:
            # Deliberately does not echo the name back into the response.
            raise KeyError("unknown account")
        return self.accounts[name]

    # -- read routes -------------------------------------------------------
    def get_config(self) -> dict[str, Any]:
        """What the page needs to render itself. No secrets."""
        return {
            "accounts": [
                {"name": a.account_name, "delete_action": a.delete_action}
                for a in self.accounts.values()
            ],
            "poll_active_sec": int(self.cfg.get("web.poll_active_sec", 3)),
            "poll_idle_sec": int(self.cfg.get("web.poll_idle_sec", 30)),
            "job_runner": str(self.cfg.get("web.job_runner", "subprocess")),
            "auth_required": self.require_auth,
            "auth_configured": self.auth_configured,
            "delete_batch_cap": int(self.cfg.get("delete.batch_cap", 50)),
        }

    def get_accounts(self) -> dict[str, Any]:
        conn = self.reader()
        try:
            out = []
            for account in self.accounts.values():
                name = account.account_name
                status = report.build_status(conn, account)
                # auth_ok is whatever the last run recorded: probing Proton
                # from a page refresh would spawn a CLI process per poll.
                status.update(self._cached_probe(account))
                job = state.active_job(conn, name)
                last = state.recent_jobs(conn, name, limit=1)
                status["active_job"] = dict(job) if job else None
                status["last_job"] = dict(last[0]) if last else None
                out.append(status)
            return {"accounts": out, "generated_at": state.utcnow()}
        finally:
            conn.close()

    def _cached_probe(self, account) -> dict[str, Any]:
        """auth_ok / immich_ok as of the last run that wrote status.json."""
        try:
            data = json.loads(account.status_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return {k: data.get(k) for k in ("auth_ok", "immich_ok")
                if data.get(k) is not None}

    def get_runs(self, account_name: str | None, limit: int = 20) -> dict[str, Any]:
        account = self.account(account_name)
        conn = self.reader()
        try:
            rows = state.recent_runs(conn, account.account_name, limit=limit)
            return {"account": account.account_name,
                    "runs": [dict(r) for r in rows]}
        finally:
            conn.close()

    def get_jobs(self, account_name: str | None, limit: int = 20) -> dict[str, Any]:
        account = self.account(account_name)
        conn = self.reader()
        try:
            return {"account": account.account_name,
                    "jobs": [dict(r) for r in state.recent_jobs(
                        conn, account.account_name, limit=limit)]}
        finally:
            conn.close()

    def get_job(self, job_id: int) -> dict[str, Any]:
        conn = self.reader()
        try:
            row = state.get_job(conn, job_id)
            if row is None:
                raise KeyError("no such job")
            return dict(row)
        finally:
            conn.close()

    def get_staged(self, account_name: str | None,
                   include_all: bool = False) -> dict[str, Any]:
        account = self.account(account_name)
        states = ((state.STAGED, state.STAGE_DELETING, state.STAGE_TRASHED,
                   state.STAGE_FAILED, state.STAGE_CANCELLED)
                  if include_all else (state.STAGED, state.STAGE_FAILED))
        conn = self.reader()
        try:
            rows = state.staged_deletes(conn, account.account_name, states=states)
            return {
                "account": account.account_name,
                "delete_action": account.delete_action,
                "batch_cap": int(self.cfg.get("delete.batch_cap", 50)),
                "staged": [dict(r) for r in rows],
                "recent_deletions": [dict(r) for r in state.deletions(
                    conn, account.account_name, limit=50)],
            }
        finally:
            conn.close()

    def staged_csv(self, account_name: str | None,
                   include_all: bool = False) -> str:
        """The staged list as CSV.

        Rendered server-side and served with a Content-Disposition, rather
        than built into a blob in the page: it works the same in every browser
        and over plain http on the LAN, where the Clipboard API does not.
        This is the mark_only workflow's main output -- the list of paths to
        delete by hand in Proton's own web app.
        """
        import csv
        import io
        data = self.get_staged(account_name, include_all=include_all)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["id", "node_id", "remote_name", "remote_path",
                         "capture_time", "staged_at", "state", "error"])
        for row in data["staged"]:
            writer.writerow([row["id"], row["node_id"], row["remote_name"],
                             row["remote_path"], row["capture_time"] or "",
                             row["staged_at"], row["state"], row["error"] or ""])
        return buffer.getvalue()

    # -- write routes ------------------------------------------------------
    def create_job(self, account_name: str | None, job_type: str,
                   payload: dict[str, Any] | None = None) -> dict[str, Any]:
        account = self.account(account_name)
        if job_type not in state.JOB_TYPES:
            raise ValueError(f"type must be one of {', '.join(state.JOB_TYPES)}")
        conn = self.writer()
        try:
            running = state.active_job(conn, account.account_name)
            if running is not None:
                # The plan's "a second click while running is rejected with a
                # clear message". Enforced here so both runners behave alike.
                return {
                    "rejected": True,
                    "reason": (f"a {running['type']} job for "
                               f"{account.account_name} is already "
                               f"{running['state']}"),
                    "job": dict(running),
                }
            job_id = state.create_job(conn, account.account_name, job_type,
                                      payload)
            log.info("web.job_queued", job=job_id, type=job_type,
                     account=account.account_name)
            return {"rejected": False, "job": dict(state.get_job(conn, job_id))}
        finally:
            conn.close()

    def execute_staged(self, account_name: str | None, ids: list[int],
                       dry_run: bool = True) -> dict[str, Any]:
        """Queue a delete job for rows the caller names by id.

        Ids only. They are resolved against `staged_deletes` inside
        `sync.py delete-staged`, which then re-resolves every node against
        Proton before touching it -- so a request cannot name a path, and
        cannot name a row belonging to another account.
        """
        account = self.account(account_name)
        cap = int(self.cfg.get("delete.batch_cap", 50))
        clean = []
        for raw in ids or []:
            try:
                clean.append(int(raw))
            except (TypeError, ValueError):
                raise ValueError("ids must be integers") from None
        if not clean:
            raise ValueError("no rows selected")
        if len(clean) > cap:
            raise ValueError(f"at most {cap} rows per execution "
                             f"(delete.batch_cap); {len(clean)} selected")

        conn = self.reader()
        try:
            known = {int(r["id"]) for r in state.get_staged(
                conn, account.account_name, clean)
                if r["state"] == state.STAGED}
        finally:
            conn.close()
        missing = sorted(set(clean) - known)
        if missing:
            raise ValueError(
                f"{len(missing)} selected row(s) are no longer staged; "
                f"refresh the list")

        return self.create_job(account.account_name, "delete",
                               {"ids": sorted(known), "dry_run": bool(dry_run)})

    def unstage(self, account_name: str | None, ids: list[int]) -> dict[str, Any]:
        account = self.account(account_name)
        clean = [int(i) for i in (ids or [])]
        if not clean:
            raise ValueError("no rows selected")
        conn = self.writer()
        try:
            count = state.unstage(conn, account.account_name, clean)
        finally:
            conn.close()
        log.info("web.unstaged", account=account.account_name, rows=count)
        return {"unstaged": count}

    # -- auth --------------------------------------------------------------
    def login(self, password: str) -> str | None:
        if not self.password_hash or not verify_password(self.password_hash,
                                                         password or ""):
            return None
        expires = int(time.time() + self.session_hours * 3600)
        return sign_session(self.secret, expires)

    def authorised(self, cookie_value: str | None) -> bool:
        if not self.require_auth:
            return True
        return check_session(self.secret, cookie_value)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

MAX_BODY = 256 * 1024

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # The page is one self-contained file: no external scripts, styles or
    # images to allow, so the policy can be this tight.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data:; form-action 'none'; base-uri 'none'; "
        "frame-ancestors 'none'"),
}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "proton-immich-sync"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        # One JSON line like everything else, not stderr's apache-ish format.
        log.debug("web.request", client=self.client_address[0],
                  detail=(fmt % args)[:300])

    def _send(self, code: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200,
              extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", extra)

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            # This is HTTP/1.1, so a body left unread stays in the socket and
            # gets parsed as the next request line. Too big to drain, so hang
            # up instead of corrupting the connection.
            self.close_connection = True
            raise ValueError("request body too large")
        # Read before validating, so every rejection path still leaves the
        # connection at a request boundary.
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        # JSON only. A cross-site form POST cannot set this content type
        # without a CORS preflight, which is the CSRF defence that pairs with
        # the SameSite=Strict cookie.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/json":
            raise ValueError("expected application/json")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def _cookie(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return None

    def _query(self) -> dict[str, str]:
        from urllib.parse import parse_qs, urlsplit
        query = urlsplit(self.path).query
        return {k: v[0] for k, v in parse_qs(query).items()}

    @property
    def _path(self) -> str:
        from urllib.parse import urlsplit
        return urlsplit(self.path).path

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        api: Api = self.server.api  # type: ignore[attr-defined]
        path = self._path

        if not path.startswith("/api/"):
            return self._static(path)

        if path == "/api/login" and method == "POST":
            return self._login(api)
        if path == "/api/logout" and method == "POST":
            return self._json({"ok": True}, extra={
                "Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; "
                              f"HttpOnly; SameSite=Strict"})
        if path == "/api/config" and method == "GET":
            # The only unauthenticated read: it tells the page whether to show
            # a login form, and carries nothing worth protecting.
            return self._json(api.get_config())

        if not api.authorised(self._cookie()):
            return self._error(401, "authentication required")

        try:
            return self._route(api, method, path)
        except KeyError as exc:
            return self._error(404, str(exc).strip("'\"") or "not found")
        except ValueError as exc:
            return self._error(400, str(exc))
        except sqlite3.Error as exc:
            log.error("web.db_error", detail=str(exc)[:300])
            return self._error(503, "the state database is unavailable")

    def _route(self, api: Api, method: str, path: str) -> None:
        query = self._query()
        account = query.get("account")

        if method == "GET":
            if path == "/api/accounts":
                return self._json(api.get_accounts())
            if path == "/api/runs":
                return self._json(api.get_runs(account, _limit(query, 20, 200)))
            if path == "/api/jobs":
                return self._json(api.get_jobs(account, _limit(query, 20, 200)))
            if path.startswith("/api/jobs/"):
                tail = path[len("/api/jobs/"):]
                if not tail.isdigit():
                    raise ValueError("job id must be a number")
                return self._json(api.get_job(int(tail)))
            if path == "/api/staged-deletes":
                return self._json(api.get_staged(
                    account, include_all=query.get("all") == "1"))
            if path == "/api/staged-deletes.csv":
                name = api.account(account).account_name
                body = api.staged_csv(
                    account, include_all=query.get("all") == "1").encode("utf-8")
                return self._send(
                    200, body, "text/csv; charset=utf-8",
                    {"Content-Disposition":
                     f'attachment; filename="staged-deletes-{name}.csv"'})
            raise KeyError("not found")

        body = self._body()
        if path == "/api/jobs":
            result = api.create_job(body.get("account") or account,
                                    str(body.get("type") or ""))
            return self._json(result, 409 if result.get("rejected") else 202)
        if path == "/api/staged-deletes/execute":
            result = api.execute_staged(
                body.get("account") or account,
                body.get("ids") or [],
                dry_run=bool(body.get("dry_run", True)))
            return self._json(result, 409 if result.get("rejected") else 202)
        if path == "/api/staged-deletes/unstage":
            return self._json(api.unstage(body.get("account") or account,
                                          body.get("ids") or []))
        raise KeyError("not found")

    def _login(self, api: Api) -> None:
        try:
            body = self._body()
        except ValueError as exc:
            return self._error(400, str(exc))
        if not api.auth_configured:
            return self._error(400, "no web password is configured")
        token = api.login(str(body.get("password") or ""))
        if token is None:
            log.warn("web.login_failed", client=self.client_address[0])
            # Deliberately slow and vague: the password is shared, so the only
            # useful defence against guessing is cost.
            time.sleep(1.0)
            return self._error(401, "wrong password")
        log.info("web.login_ok", client=self.client_address[0])
        max_age = api.session_hours * 3600
        return self._json({"ok": True}, extra={
            "Set-Cookie": (f"{SESSION_COOKIE}={token}; Path=/; "
                           f"Max-Age={max_age}; HttpOnly; SameSite=Strict")})

    # -- static ------------------------------------------------------------
    def _static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            target = STATIC_DIR / "index.html"
        else:
            # No directory traversal, and no directory listing: this serves
            # exactly the files shipped in web/.
            name = Path(path.lstrip("/")).name
            target = STATIC_DIR / name
            if name != path.lstrip("/") or not name:
                return self._error(404, "not found")
        try:
            body = target.read_bytes()
        except OSError:
            return self._error(404, "not found")
        types = {".html": "text/html; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon"}
        self._send(200, body, types.get(target.suffix, "application/octet-stream"))


def _limit(query: dict[str, str], default: int, cap: int) -> int:
    try:
        value = int(query.get("limit", default))
    except (TypeError, ValueError):
        return default
    return max(1, min(value, cap))


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    api: Api


def build_server(cfg, bind: str, port: int, require_auth: bool = True) -> Server:
    api = Api(cfg, require_auth=require_auth)
    server = Server((bind, port), Handler)
    server.api = api
    return server


def serve(cfg, port: int | None = None, bind: str | None = None,
          require_auth: bool = True) -> int:
    """Run the UI until interrupted. Returns a process exit code."""
    bind = bind or str(cfg.get("web.bind", "127.0.0.1"))
    port = int(port or cfg.get("web.port", 8080))

    try:
        api = Api(cfg, require_auth=require_auth)
    except ValueError as exc:
        log.error("web.config_invalid", detail=str(exc))
        return 4

    if api.require_auth is False and api.auth_configured is False:
        if bind not in ("127.0.0.1", "localhost", "::1"):
            # Mirjam uses this, so it is not a localhost tool. Refusing is
            # better than quietly publishing the delete queue to the LAN.
            log.error("web.refusing_unauthenticated_bind", bind=bind,
                      detail="set web.password_hash (sync.py web-password) or "
                             "PIS_WEB_PASSWORD, or bind to 127.0.0.1")
            return 4
        log.warn("web.no_auth", detail="no web password configured; "
                                       "localhost only")

    # Create the schema if this is a fresh install, then clear anything left
    # `running` by a process that is gone -- without that the UI would refuse
    # every new job for that account forever.
    #
    # A pre-v3 database is NOT migrated here: assigning every legacy row to an
    # account is a decision, and with a list of them there is no right answer
    # to guess. `sync.py --account <name> status` does it deliberately.
    owner = (next(iter(api.accounts)) if len(api.accounts) == 1 else None)
    conn = state.connect(cfg.db_path)
    try:
        state.init_schema(conn, owner)
        released = state.release_stale_jobs(conn)
        if released:
            log.info("web.released_stale_jobs", rows=released)
    except state.MigrationError as exc:
        log.error("web.schema_migration_needed", detail=str(exc))
        return 4
    finally:
        conn.close()

    worker = JobWorker(cfg, api.accounts, str(cfg.path) if cfg.path else None)
    worker.start()

    server = Server((bind, port), Handler)
    server.api = api  # the handler reads it off the server, one per process
    log.info("web.listening", bind=bind, port=port,
             accounts=",".join(api.accounts),
             auth="on" if api.require_auth else "off",
             runner=str(cfg.get("web.job_runner", "subprocess")))
    print(f"proton-immich-sync UI on http://{bind}:{port}", file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("web.stopping")
    finally:
        worker.stop()
        server.server_close()
    return 0
