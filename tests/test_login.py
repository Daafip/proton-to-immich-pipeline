"""Sign-in flow: URL capture from the CLI, and the phone-facing redirect."""

import stat
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import login as login_mod  # noqa: E402
from src.config import load  # noqa: E402
from src.proton import ProtonCliBackend, ProtonError  # noqa: E402

SIGN_IN_URL = ("https://account.proton.me/desktop/login?app=drive&pv=3"
               "#payload=0%3AABCD%3Axyz%3D%3Acli-drive")

def fake_cli(directory: Path, body: str) -> Path:
    """A stand-in for the proton-drive binary."""
    path = directory / "fake-proton-drive"
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path

class TestStartLogin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = load(None)
        self.cfg.set("staging.root", str(self.dir))

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def reap(proc) -> None:
        proc.kill()
        proc.wait(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()

    def backend_for(self, body: str) -> ProtonCliBackend:
        self.cfg.set("proton.binary", str(fake_cli(self.dir, body)))
        return ProtonCliBackend(self.cfg)

    def test_sign_in_url_is_captured(self):
        backend = self.backend_for(
            "import json, time\n"
            f"print(json.dumps({{'signInUrl': {SIGN_IN_URL!r}}}), flush=True)\n"
            "time.sleep(30)\n")
        url, proc = backend.start_login(timeout=10)
        self.addCleanup(self.reap, proc)
        self.assertEqual(url, SIGN_IN_URL)
        self.assertIsNone(proc.poll(), "the CLI keeps running while you sign in")

    def test_leading_noise_is_skipped(self):
        backend = self.backend_for(
            "import json, time\n"
            "print('starting up', flush=True)\n"
            f"print(json.dumps({{'signInUrl': {SIGN_IN_URL!r}}}), flush=True)\n"
            "time.sleep(30)\n")
        url, proc = backend.start_login(timeout=10)
        self.addCleanup(self.reap, proc)
        self.assertEqual(url, SIGN_IN_URL)

    def test_no_url_raises(self):
        backend = self.backend_for("import sys\nsys.stderr.write('boom\\n')\n")
        with self.assertRaises(ProtonError) as ctx:
            backend.start_login(timeout=5)
        self.assertIn("no sign-in URL", str(ctx.exception))

    def test_missing_binary_raises(self):
        self.cfg.set("proton.binary", str(self.dir / "does-not-exist"))
        with self.assertRaises(ProtonError):
            ProtonCliBackend(self.cfg).start_login(timeout=5)

class TestRedirectPage(unittest.TestCase):
    def test_ampersands_are_escaped_in_html(self):
        body = login_mod.render_page(SIGN_IN_URL).decode()
        self.assertIn("&amp;pv=3", body)
        self.assertNotIn('href="https://account.proton.me/desktop/login?app=drive&pv',
                         body)

    def test_script_tag_cannot_be_broken_out_of(self):
        body = login_mod.render_page('https://x/?a=1"></script><script>evil()').decode()
        js = body.split("location.replace")[1]
        # The payload stays inside the JS string: its </script> is neutralised,
        # so it cannot close the element and start a new one.
        self.assertNotIn("</script><script>", js)
        self.assertIn("<\\/script>", js)

    def test_fragment_is_preserved(self):
        self.assertIn("payload=", login_mod.render_page(SIGN_IN_URL).decode())

class TestRedirectServer(unittest.TestCase):
    def setUp(self):
        self.httpd = login_mod.serve_redirect(SIGN_IN_URL, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        time.sleep(0.2)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def fetch(self, path="/"):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            return opener.open(f"http://127.0.0.1:{self.port}{path}")
        except urllib.error.HTTPError as exc:
            return exc

    def test_redirects_with_the_fragment_intact(self):
        resp = self.fetch()
        self.assertEqual(resp.code, 302)
        # The payload lives in the fragment, so it must survive the redirect.
        self.assertEqual(resp.headers.get("Location"), SIGN_IN_URL)

    def test_any_path_redirects(self):
        """Easier to type on a phone than a token path."""
        self.assertEqual(self.fetch("/anything").code, 302)

    def test_favicon_is_not_a_redirect(self):
        self.assertEqual(self.fetch("/favicon.ico").code, 404)

    def test_body_offers_a_manual_link(self):
        self.assertIn("Continue to Proton", self.fetch().read().decode())

class TestDefaults(unittest.TestCase):
    def test_login_port_avoids_immich(self):
        port = load(None).get("proton.login_redirect_port")
        self.assertNotEqual(port, 2283, "must not collide with Immich")
        self.assertEqual(port, 8399)

    def test_local_ip_is_a_string_or_none(self):
        ip = login_mod.local_ip()
        self.assertTrue(ip is None or ip.count(".") == 3)

if __name__ == "__main__":
    unittest.main()
