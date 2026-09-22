"""The web UI: routes, auth, and what the job runner is allowed to execute.

The HTTP tests start a real server on an ephemeral port and talk to it with
urllib -- there is no framework to fake, and the interesting behaviour (401s,
cookies, the CSP headers, path traversal) lives in the HTTP layer.
"""

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import state, web  # noqa: E402
from src.config import load  # noqa: E402
from tests.helpers import silence_logs  # noqa: E402

ACCOUNT = "default"
PASSWORD = "correct horse battery staple"


class WebTest(unittest.TestCase):
    def setUp(self):
        silence_logs()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load(None)
        self.cfg.set("staging.root", self.tmp.name)
        self.cfg.set("immich.api_key", "k")
        self.cfg.set("immich.url", "http://vm:2283/api")
        self.cfg.set("web.password_hash", web.hash_password(PASSWORD))
        for directory in (self.cfg.state_dir, self.cfg.ready_dir,
                          self.cfg.incoming_dir, self.cfg.batch_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.conn = state.connect(self.cfg.db_path)
        state.init_schema(self.conn, ACCOUNT)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def api(self, require_auth=True) -> web.Api:
        return web.Api(self.cfg, require_auth=require_auth)

    def seed_asset(self, node_id="n1", status=state.PURGED, asset_id="a1"):
        state.upsert_discovered(self.conn, ACCOUNT, node_id, f"/Photos/{node_id}.jpg",
                                f"{node_id}.jpg", 100, "2026-01-01T00:00:00+00:00",
                                capture_time="2019-06-01T12:00:00+00:00")
        self.conn.execute(
            "UPDATE assets SET status=?, immich_asset_id=? WHERE account=? AND node_id=?",
            (status, asset_id, ACCOUNT, node_id))
        self.conn.commit()
        return state.get(self.conn, ACCOUNT, node_id)

    def stage(self, node_id="n1"):
        row = self.seed_asset(node_id, asset_id=f"asset-{node_id}")
        state.stage_delete(self.conn, ACCOUNT, row)
        return int(state.staged_deletes(self.conn, ACCOUNT)[-1]["id"])


# ---------------------------------------------------------------------------
# passwords and sessions
# ---------------------------------------------------------------------------

class TestPasswords(unittest.TestCase):
    def test_hash_round_trip(self):
        stored = web.hash_password("hunter2")
        self.assertTrue(stored.startswith("scrypt:"))
        self.assertNotIn("hunter2", stored)
        self.assertTrue(web.verify_password(stored, "hunter2"))
        self.assertFalse(web.verify_password(stored, "hunter3"))

    def test_the_hash_contains_no_dollar_sign(self):
        """A `$` would be eaten by Docker Compose's .env interpolation --
        `scrypt$32768$8$1$<salt>$<key>` reaches the container as
        `scrypt$32768$8$1`, and every login fails with no explanation."""
        for _ in range(5):
            self.assertNotIn("$", web.hash_password("hunter2"))

    def test_a_legacy_dollar_separated_hash_still_verifies(self):
        stored = web.hash_password("hunter2").replace(":", "$")
        self.assertTrue(web.verify_password(stored, "hunter2"))
        self.assertFalse(web.verify_password(stored, "wrong"))

    def test_a_truncated_hash_is_refused_not_matched(self):
        """What a `$`-separated hash looks like after .env has had it."""
        for mangled in ("scrypt$32768$8$1", "scrypt:32768:8:1",
                        "scrypt$$32768$$8$$1", "scrypt:32768"):
            with self.subTest(mangled=mangled):
                self.assertIsNone(web.split_hash(mangled))
                self.assertFalse(web.verify_password(mangled, "hunter2"))

    def test_each_hash_is_salted(self):
        self.assertNotEqual(web.hash_password("x"), web.hash_password("x"))

    def test_a_malformed_hash_is_just_a_failed_login(self):
        for junk in ("", "nonsense", "scrypt:a:b:c:d:e", "md5:1:2:3:4:5",
                     "scrypt$a$b$c$d$e"):
            self.assertFalse(web.verify_password(junk, "anything"))

    def test_session_signing(self):
        secret = b"s" * 32
        token = web.sign_session(secret, int(time.time()) + 60)
        self.assertTrue(web.check_session(secret, token))
        self.assertFalse(web.check_session(b"other" * 8, token),
                         "a different key must not validate")
        self.assertFalse(web.check_session(secret, token[:-2] + "00"))
        self.assertFalse(web.check_session(secret, None))
        self.assertFalse(web.check_session(secret, "no-dot"))

    def test_an_expired_session_is_rejected(self):
        secret = b"s" * 32
        token = web.sign_session(secret, int(time.time()) - 1)
        self.assertFalse(web.check_session(secret, token))

    def test_the_expiry_cannot_be_edited_without_the_key(self):
        secret = b"s" * 32
        token = web.sign_session(secret, int(time.time()) - 1)
        payload, _, mac = token.rpartition(".")
        forged = web.sign_session(b"guess" * 8, int(time.time()) + 9999)
        self.assertFalse(web.check_session(secret, forged))
        self.assertFalse(web.check_session(secret, f"{forged.split('.')[0]}.{mac}"))


class TestMangledHashIsRefusedAtStartup(WebTest):
    def test_serve_refuses_a_truncated_hash_rather_than_failing_logins(self):
        """The failure mode this replaces: every login rejected, nothing in
        the log to say the hash itself was the problem."""
        self.cfg.set("web.password_hash", "scrypt$32768$8$1")
        with self.assertRaises(ValueError) as ctx:
            web.Api(self.cfg, require_auth=True)
        message = str(ctx.exception)
        self.assertIn(".env", message)
        self.assertIn("web-password", message)

    def test_a_good_hash_starts_normally(self):
        self.cfg.set("web.password_hash", web.hash_password("x"))
        self.assertTrue(web.Api(self.cfg, require_auth=True).auth_configured)

    def test_no_hash_at_all_is_not_an_error(self):
        self.cfg.set("web.password_hash", "")
        self.assertFalse(web.Api(self.cfg, require_auth=True).auth_configured)


class TestSecret(WebTest):
    def test_the_secret_is_created_once_and_reused(self):
        first = web.load_secret(self.cfg)
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(web.load_secret(self.cfg), first,
                         "a regenerated key would log everyone out on restart")

    def test_the_secret_file_is_not_world_readable(self):
        web.load_secret(self.cfg)
        path = self.cfg.state_dir / "web-secret"
        self.assertEqual(path.stat().st_mode & 0o077, 0,
                         "holding the key is enough to mint a session")


# ---------------------------------------------------------------------------
# the API, called directly
# ---------------------------------------------------------------------------

class TestApiReads(WebTest):
    def test_config_carries_no_secrets(self):
        payload = json.dumps(self.api().get_config())
        self.assertNotIn(PASSWORD, payload)
        self.assertNotIn("scrypt", payload)
        self.assertNotIn("api_key", payload)

    def test_accounts_reports_one_row_per_account(self):
        self.seed_asset()
        data = self.api().get_accounts()
        self.assertEqual([a["account"] for a in data["accounts"]], [ACCOUNT])
        row = data["accounts"][0]
        for key in ("backlog", "uploaded_total", "failed", "quarantined",
                    "staged_deletes", "last_run", "last_success", "auth_ok"):
            self.assertIn(key, row)

    def test_no_endpoint_leaks_the_immich_key(self):
        self.stage()
        api = self.api()
        blob = json.dumps([api.get_config(), api.get_accounts(),
                           api.get_staged(ACCOUNT), api.get_runs(ACCOUNT)],
                          default=str)
        self.assertNotIn("api_key", blob)
        self.assertNotIn('"k"', blob)

    def test_staged_lists_what_the_table_needs(self):
        self.stage("n1")
        data = self.api().get_staged(ACCOUNT)
        self.assertEqual(len(data["staged"]), 1)
        row = data["staged"][0]
        for key in ("id", "node_id", "remote_name", "remote_path",
                    "capture_time", "staged_at", "state"):
            self.assertIn(key, row)
        self.assertEqual(data["delete_action"], "mark_only")

    def test_staged_csv_has_a_header_and_a_row(self):
        self.stage("n1")
        text = self.api().staged_csv(ACCOUNT)
        lines = text.strip().splitlines()
        self.assertTrue(lines[0].startswith("id,node_id,remote_name"))
        self.assertIn("/Photos/n1.jpg", lines[1])

    def test_an_unknown_account_is_a_404_not_an_echo(self):
        with self.assertRaises(KeyError) as ctx:
            self.api().get_staged("nope")
        self.assertNotIn("nope", str(ctx.exception))

    def test_reads_use_a_read_only_connection(self):
        import sqlite3
        conn = self.api().reader(ACCOUNT)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM assets")
        finally:
            conn.close()


class TestApiJobs(WebTest):
    def test_a_job_is_queued_not_run(self):
        result = self.api().create_job(ACCOUNT, "sync")
        self.assertFalse(result["rejected"])
        self.assertEqual(result["job"]["state"], state.JOB_QUEUED)
        self.assertEqual(result["job"]["type"], "sync")

    def test_a_second_click_while_running_is_rejected_clearly(self):
        api = self.api()
        api.create_job(ACCOUNT, "sync")
        again = api.create_job(ACCOUNT, "sync")
        self.assertTrue(again["rejected"])
        self.assertIn(ACCOUNT, again["reason"])
        self.assertIn("already", again["reason"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"], 1)

    def test_an_unknown_job_type_is_refused(self):
        with self.assertRaises(ValueError):
            self.api().create_job(ACCOUNT, "rm-rf")

    def test_stale_jobs_are_released_at_startup(self):
        api = self.api()
        api.create_job(ACCOUNT, "sync")
        state.claim_job(self.conn)
        # A killed server leaves the row `running` and the UI would then
        # refuse every new job for that account forever.
        self.assertEqual(state.release_stale_jobs(self.conn), 1)
        self.assertIsNone(state.active_job(self.conn, ACCOUNT))
        self.assertFalse(api.create_job(ACCOUNT, "sync")["rejected"])


class TestApiExecute(WebTest):
    def test_only_staged_ids_are_accepted(self):
        staged_id = self.stage("n1")
        result = self.api().execute_staged(ACCOUNT, [staged_id], dry_run=True)
        self.assertFalse(result["rejected"])
        payload = json.loads(result["job"]["payload"])
        self.assertEqual(payload["ids"], [staged_id])
        self.assertTrue(payload["dry_run"])

    def test_dry_run_is_carried_through_to_the_job(self):
        staged_id = self.stage("n1")
        result = self.api().execute_staged(ACCOUNT, [staged_id], dry_run=False)
        self.assertFalse(json.loads(result["job"]["payload"])["dry_run"])

    def test_an_unknown_id_is_refused_rather_than_ignored(self):
        self.stage("n1")
        with self.assertRaises(ValueError) as ctx:
            self.api().execute_staged(ACCOUNT, [999999], dry_run=False)
        self.assertIn("no longer staged", str(ctx.exception))

    def test_another_accounts_row_id_is_refused(self):
        """Ids are resolved against `staged_deletes` scoped to the account, so
        a stray id cannot reach across -- the property that makes it safe for
        a request to name rows by id."""
        staged_id = self.stage("n1")
        self.cfg.set("accounts", [{"name": ACCOUNT}, {"name": "mirjam",
                                                       "staging_dir": self.tmp.name + "/m",
                                                       "immich_api_key": "other"}])
        api = self.api()
        with self.assertRaises(ValueError):
            api.execute_staged("mirjam", [staged_id], dry_run=False)

    def test_ids_must_be_integers(self):
        self.stage("n1")
        with self.assertRaises(ValueError):
            self.api().execute_staged(ACCOUNT, ["/Photos/../../etc/passwd"],
                                      dry_run=False)

    def test_an_empty_selection_is_refused(self):
        with self.assertRaises(ValueError):
            self.api().execute_staged(ACCOUNT, [], dry_run=False)

    def test_the_batch_cap_is_enforced_server_side(self):
        ids = [self.stage(f"n{i}") for i in range(5)]
        self.cfg.set("delete.batch_cap", 3)
        with self.assertRaises(ValueError) as ctx:
            self.api().execute_staged(ACCOUNT, ids, dry_run=False)
        self.assertIn("at most 3", str(ctx.exception))

    def test_unstage_takes_rows_off_the_queue(self):
        staged_id = self.stage("n1")
        self.assertEqual(self.api().unstage(ACCOUNT, [staged_id]),
                         {"unstaged": 1})
        self.assertEqual(state.count_staged(self.conn, ACCOUNT), 0)


# ---------------------------------------------------------------------------
# what the worker is allowed to execute
# ---------------------------------------------------------------------------

class TestJobWorkerArgv(WebTest):
    def worker(self, runner="subprocess"):
        self.cfg.set("web.job_runner", runner)
        api = self.api()
        return web.JobWorker(self.cfg, api.accounts, None)

    def job(self, job_type="sync", account=ACCOUNT, payload=None):
        job_id = state.create_job(self.conn, account, job_type, payload)
        return state.get_job(self.conn, job_id)

    def test_sync_runs_sync_py(self):
        argv = self.worker().argv_for(self.job("sync"))
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(argv[1].endswith("sync.py"))
        self.assertIn("--account", argv)
        self.assertEqual(argv[-1], "run")

    def test_sync_can_trigger_systemd_instead(self):
        argv = self.worker("systemd").argv_for(self.job("sync"))
        self.assertEqual(argv[-2:], ["start", f"proton-to-immich-pipeline@{ACCOUNT}.service"])
        self.assertIn("-n", argv, "sudo must never prompt from a daemon")

    def test_reconcile_has_its_own_subcommand(self):
        self.assertEqual(self.worker().argv_for(self.job("reconcile"))[-1],
                         "reconcile")

    def test_a_real_delete_passes_yes_and_the_ids(self):
        argv = self.worker().argv_for(
            self.job("delete", payload={"ids": [3, 7], "dry_run": False}))
        self.assertIn("delete-staged", argv)
        self.assertIn("--yes", argv)
        self.assertEqual(argv[-2:], ["3", "7"])

    def test_a_dry_run_delete_omits_yes(self):
        """`delete-staged` without --yes is a dry run whatever else is asked,
        so forgetting the flag can only ever be the safe direction."""
        argv = self.worker().argv_for(
            self.job("delete", payload={"ids": [3], "dry_run": True}))
        self.assertNotIn("--yes", argv)

    def test_ids_are_coerced_to_integers(self):
        argv = self.worker().argv_for(
            self.job("delete", payload={"ids": ["4"], "dry_run": False}))
        self.assertEqual(argv[-1], "4")
        with self.assertRaises(ValueError):
            self.worker().argv_for(
                self.job("delete", payload={"ids": ["; rm -rf /"],
                                            "dry_run": False}))

    def test_an_account_the_config_does_not_name_is_refused(self):
        """The account reaches a unit name and an argv, so it is checked
        against the config allowlist rather than trusted."""
        worker = self.worker("systemd")
        for bad in ("nope", "../../etc", "a b", "$(id)", ""):
            with self.assertRaises(KeyError):
                worker.argv_for(self.job("sync", account=bad))

    def test_an_unknown_type_is_refused(self):
        with self.assertRaises(ValueError):
            self.worker().argv_for(self.job("mystery"))

    def test_nothing_is_ever_run_through_a_shell(self):
        import inspect
        source = inspect.getsource(web.JobWorker.execute)
        self.assertNotIn("shell=True", source)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class HttpTest(WebTest):
    require_auth = True

    def setUp(self):
        super().setUp()
        self.server = web.build_server(self.cfg, "127.0.0.1", 0,
                                       require_auth=self.require_auth)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()
        self.cookie = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def request(self, path, body=None, method=None, cookie=True):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data,
                                     method=method or ("POST" if data else "GET"))
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if cookie and self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def login(self, password=PASSWORD):
        status, body, headers = self.request("/api/login", {"password": password})
        if status == 200:
            self.cookie = headers["Set-Cookie"].split(";")[0]
        return status, json.loads(body or b"{}")


class TestHttpAuth(HttpTest):
    def test_every_api_route_needs_a_session(self):
        for path in ("/api/accounts", "/api/runs", "/api/staged-deletes",
                     "/api/jobs", "/api/jobs/1", "/api/staged-deletes.csv"):
            status, body, _ = self.request(path)
            self.assertEqual(status, 401, path)
            self.assertIn("authentication", json.loads(body)["error"])

    def test_write_routes_need_a_session_too(self):
        for path, body in (("/api/jobs", {"type": "sync"}),
                           ("/api/staged-deletes/execute", {"ids": [1]}),
                           ("/api/staged-deletes/unstage", {"ids": [1]})):
            status, _, _ = self.request(path, body)
            self.assertEqual(status, 401, path)

    def test_config_is_readable_without_a_session(self):
        """The page needs it to know whether to show a login form."""
        status, body, _ = self.request("/api/config")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["auth_required"])
        self.assertNotIn("password_hash", payload)

    def test_the_right_password_gets_in(self):
        status, payload = self.login()
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        status, _, _ = self.request("/api/accounts")
        self.assertEqual(status, 200)

    def test_the_wrong_password_does_not(self):
        status, payload = self.login("guess")
        self.assertEqual(status, 401)
        self.assertIsNone(self.cookie)
        self.assertEqual(self.request("/api/accounts")[0], 401)

    def test_the_cookie_is_httponly_and_samesite(self):
        _, _, headers = self.request("/api/login", {"password": PASSWORD})
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_a_forged_cookie_is_rejected(self):
        self.login()
        self.cookie = f"{web.SESSION_COOKIE}=eyJleHAiOjk5OTk5OTk5OTl9.deadbeef"
        self.assertEqual(self.request("/api/accounts")[0], 401)

    def test_logout_clears_the_cookie(self):
        self.login()
        _, _, headers = self.request("/api/logout", {})
        self.assertIn("Max-Age=0", headers["Set-Cookie"])


class TestHttpRoutes(HttpTest):
    def setUp(self):
        super().setUp()
        self.login()

    def get(self, path):
        status, body, headers = self.request(path)
        return status, json.loads(body or b"{}"), headers

    def test_accounts_and_runs(self):
        self.seed_asset()
        status, payload, _ = self.get("/api/accounts")
        self.assertEqual(status, 200)
        self.assertEqual(payload["accounts"][0]["account"], ACCOUNT)
        self.assertEqual(self.get("/api/runs")[1]["account"], ACCOUNT)

    def test_a_queued_job_comes_back_202(self):
        status, body, _ = self.request("/api/jobs", {"type": "sync"})
        self.assertEqual(status, 202)
        payload = json.loads(body)
        self.assertFalse(payload["rejected"])
        self.assertEqual(payload["job"]["state"], state.JOB_QUEUED)

    def test_a_duplicate_job_is_409_with_a_reason(self):
        self.request("/api/jobs", {"type": "sync"})
        status, body, _ = self.request("/api/jobs", {"type": "sync"})
        self.assertEqual(status, 409)
        payload = json.loads(body)
        self.assertTrue(payload["rejected"])
        self.assertIn("already", payload["reason"])

    def test_a_job_can_be_polled_by_id(self):
        _, body, _ = self.request("/api/jobs", {"type": "sync"})
        job_id = json.loads(body)["job"]["id"]
        status, payload, _ = self.get(f"/api/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertIn(payload["state"], (state.JOB_QUEUED, state.JOB_RUNNING,
                                         state.JOB_DONE, state.JOB_FAILED))

    def test_a_non_numeric_job_id_is_a_400(self):
        self.assertEqual(self.request("/api/jobs/../../etc/passwd")[0], 400)

    def test_staged_deletes_and_its_csv(self):
        self.stage("n1")
        status, payload, _ = self.get("/api/staged-deletes")
        self.assertEqual(len(payload["staged"]), 1)
        status, body, headers = self.request("/api/staged-deletes.csv")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn(b"/Photos/n1.jpg", body)

    def test_execute_queues_a_delete_job(self):
        staged_id = self.stage("n1")
        status, body, _ = self.request("/api/staged-deletes/execute",
                                       {"ids": [staged_id], "dry_run": True})
        self.assertEqual(status, 202)
        job = json.loads(body)["job"]
        self.assertEqual(job["type"], "delete")
        self.assertEqual(json.loads(job["payload"])["ids"], [staged_id])

    def test_execute_refuses_a_path_in_place_of_an_id(self):
        self.stage("n1")
        status, body, _ = self.request(
            "/api/staged-deletes/execute",
            {"ids": ["/Photos/n1.jpg"], "dry_run": False})
        self.assertEqual(status, 400)
        self.assertIn("integers", json.loads(body)["error"])

    def test_unknown_routes_are_404(self):
        self.assertEqual(self.request("/api/nope")[0], 404)
        self.assertEqual(self.request("/api/nope", {})[0], 404)

    def test_a_form_post_is_refused(self):
        """The CSRF pairing: JSON-only bodies plus a SameSite=Strict cookie.
        A cross-site form cannot set this content type without a preflight."""
        url = f"http://127.0.0.1:{self.port}/api/jobs"
        req = urllib.request.Request(url, data=b"type=sync", method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.fail(f"expected 400, got {resp.status}")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertIn("application/json", json.loads(exc.read())["error"])

    def test_an_oversized_body_is_refused(self):
        status, _, _ = self.request("/api/jobs", {"type": "sync",
                                                  "pad": "x" * 300_000})
        self.assertEqual(status, 400)

    def test_a_rejected_body_leaves_the_connection_usable(self):
        """This is HTTP/1.1 with keep-alive: a body left unread would be
        parsed as the next request line. A refusal must either drain the body
        or hang up, never leave it in the socket."""
        import http.client
        for headers, body in (
            ({"Content-Type": "application/x-www-form-urlencoded"},
             b"type=sync"),                                  # drained
            ({"Content-Type": "application/json"},
             b'{"type": "sync", "pad": "' + b"x" * 300_000 + b'"}'),  # hang up
        ):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            try:
                headers = {**headers, "Cookie": self.cookie}
                conn.request("POST", "/api/jobs", body=body, headers=headers)
                self.assertEqual(conn.getresponse().status, 400)
                # Reuse the same connection. A leftover body shows up here as
                # a 400 on a well-formed request, or a hang.
                conn.request("GET", "/api/config")
                second = conn.getresponse()
                self.assertEqual(second.status, 200)
                json.loads(second.read())
            except (http.client.HTTPException, OSError):
                # The server hung up, which is the other acceptable answer to
                # an undrainable body -- the client just has to reconnect.
                # BrokenPipeError is an OSError, not an HTTPException.
                pass
            finally:
                conn.close()


class TestHttpStatic(HttpTest):
    def test_the_page_is_served_at_the_root(self):
        status, body, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Proton", body)

    def test_security_headers_are_present(self):
        _, _, headers = self.request("/")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

    def test_path_traversal_is_refused(self):
        for path in ("/../sync.py", "/../../etc/passwd", "/web/../sync.py",
                     "/src/config.py"):
            status, _, _ = self.request(path)
            self.assertEqual(status, 404, path)

    def test_the_page_carries_no_api_key_or_password(self):
        _, body, _ = self.request("/")
        text = body.decode()
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn("scrypt$", text)


class TestHttpNoAuth(HttpTest):
    """--no-auth, for a localhost-only development run."""

    require_auth = False

    def test_routes_are_open(self):
        self.assertEqual(self.request("/api/accounts")[0], 200)

    def test_the_page_is_told_not_to_ask_for_a_password(self):
        _, body, _ = self.request("/api/config")
        self.assertFalse(json.loads(body)["auth_required"])


class TestServeSchema(WebTest):
    def test_serve_creates_the_schema_on_a_fresh_install(self):
        """`serve` touches the `jobs` table at startup, so on a brand-new box
        it has to create it rather than crash on `no such table`."""
        self.conn.close()
        self.cfg.db_path.unlink()
        api = web.Api(self.cfg, require_auth=True)
        owner = next(iter(api.accounts))
        conn = state.connect(self.cfg.db_path)
        try:
            state.init_schema(conn, owner)
            self.assertEqual(state.release_stale_jobs(conn), 0)
        finally:
            conn.close()
        self.conn = state.connect(self.cfg.db_path)

    def test_serve_refuses_a_pre_v3_database_it_cannot_assign(self):
        """Deciding which account owns a legacy row is a decision, not a
        guess, so a multi-account serve refuses instead of picking one."""
        self.conn.executescript(
            "DROP INDEX IF EXISTS idx_assets_status;"
            "DROP INDEX IF EXISTS idx_assets_sha1;"
            "DROP INDEX IF EXISTS idx_assets_immich;"
            "DROP TABLE assets;")
        self.conn.execute(
            "CREATE TABLE assets (node_id TEXT PRIMARY KEY, remote_path TEXT"
            " NOT NULL, remote_name TEXT NOT NULL, status TEXT NOT NULL,"
            " first_seen TEXT NOT NULL, sha1 TEXT, local_path TEXT,"
            " remote_size INTEGER, remote_modified TEXT, immich_asset_id TEXT,"
            " is_duplicate INTEGER, attempts INTEGER, last_attempt TEXT,"
            " last_error TEXT)")
        self.conn.commit()
        with self.assertRaises(state.MigrationError):
            state.init_schema(self.conn, None)


class TestServeRefusesUnsafeBind(WebTest):
    def test_no_password_plus_a_lan_bind_is_refused(self):
        """Mirjam uses this, so it is not a localhost tool. Publishing the
        delete queue to the LAN with no password is a configuration mistake worth
        failing on rather than warning about."""
        self.cfg.set("web.password_hash", "")
        self.assertEqual(web.serve(self.cfg, port=0, bind="0.0.0.0",
                                   require_auth=False), 4)


if __name__ == "__main__":
    unittest.main()
