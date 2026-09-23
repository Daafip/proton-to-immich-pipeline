"""Interactive Proton sign-in, reachable from a phone.

`proton-drive auth login --json` prints a sign-in URL and then waits. The URL
carries its payload in the fragment, so the browser never calls back to this
machine -- the CLI polls Proton's API instead. Any device can therefore finish
the sign-in, which is what makes a phone workable at all.

The only real problem left is getting a 200-character URL onto the phone. This
serves it as a redirect on a LAN port (never Immich's 2283), so you open
http://<vm-ip>:<port>/ on the phone and land on the Proton sign-in page.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

from . import log
from .proton import ProtonCliBackend, ProtonError

PAGE = """<!doctype html>
<title>Proton sign-in</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{{font:16px/1.5 system-ui,sans-serif;margin:0;padding:2rem;
      background:#14141c;color:#e8e8ef}}
 a{{display:inline-block;margin-top:1.5rem;padding:.9rem 1.4rem;border-radius:.6rem;
    background:#6d4aff;color:#fff;text-decoration:none;font-weight:600}}
 p{{color:#a0a0b0}}
</style>
<h1>Proton sign-in</h1>
<p>Redirecting to Proton&hellip; if nothing happens, tap below.</p>
<a href="{url}">Continue to Proton</a>
<script>location.replace({url_json});</script>
"""


def local_ip() -> str | None:
    """Best-guess LAN address. Opens no connection -- UDP connect is local."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 53))
            return sock.getsockname()[0]
    except OSError:
        return None


def render_page(url: str) -> bytes:
    """The sign-in URL contains & and % and goes into two different contexts."""
    import html
    import json as _json
    js = _json.dumps(url).replace("</", "<\\/")
    return PAGE.format(url=html.escape(url, quote=True), url_json=js).encode()


def make_handler(url: str):
    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path.startswith("/favicon"):
                self.send_response(404)
                self.end_headers()
                return
            log.info("login.redirect_served", client=self.client_address[0])
            body = render_page(url)
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            """Silence BaseHTTPRequestHandler's stderr logging."""

    return RedirectHandler


def serve_redirect(url: str, bind: str, port: int) -> HTTPServer:
    httpd = HTTPServer((bind, port), make_handler(url))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def print_qr(url: str) -> bool:
    """Terminal QR via qrencode, if it happens to be installed."""
    if not shutil.which("qrencode"):
        return False
    try:
        proc = subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "1", url],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode == 0 and proc.stdout:
        print(proc.stdout)
        return True
    return False


def run_login(cfg, port: int | None = None, bind: str = "0.0.0.0",
              serve: bool = True, timeout: int | None = None,
              on_url: Callable[[str, str], None] | None = None) -> int:
    """Drive the sign-in. Returns a process exit code.

    `on_url(url, expires_at)` is the UI's route: the job runner passes it, and
    the URL then goes to the job row instead of stdout -- stdout ends up in
    the job's `detail` and the logs, and the link should not outlive its use.
    """
    backend = ProtonCliBackend(cfg)
    port = port or int(cfg.get("proton.login_redirect_port", 8399))
    timeout = timeout or int(cfg.get("proton.login_timeout_sec", 300))

    try:
        url, proc = backend.start_login()
    except ProtonError as exc:
        log.error("login.failed_to_start", detail=str(exc)[:400])
        return 2

    if on_url is not None:
        expires = datetime.now(timezone.utc) + timedelta(seconds=timeout)
        on_url(url, expires.strftime("%Y-%m-%dT%H:%M:%SZ"))
        log.info("login.waiting", seconds=timeout, via="ui")
        return _wait(proc, timeout)

    httpd = None
    if serve:
        try:
            httpd = serve_redirect(url, bind, port)
        except OSError as exc:
            log.warn("login.redirect_unavailable", port=port, detail=str(exc))

    print()
    print("  Open this on the device you want to sign in with:")
    print()
    if httpd is not None:
        ip = local_ip()
        if ip:
            print(f"      http://{ip}:{port}/          <-- phone-friendly")
        print(f"      http://localhost:{port}/")
        if log.in_container():
            # That address is the container's, which nothing on the LAN can
            # reach. Saying so beats letting someone try it on their phone
            # and conclude the sign-in is broken.
            print()
            print(f"      ^ container-internal. Re-run with -p {port}:{port}")
            print("        and use the HOST's address, or just paste the URL")
            print("        below -- it works from any device either way.")
        print()
    print("  Or paste the full URL directly:")
    print()
    print(f"      {url}")
    print()
    if httpd is not None and not print_qr(f"http://{local_ip() or 'localhost'}:{port}/"):
        log.debug("login.no_qrencode")
    print(f"  Waiting up to {timeout}s for the sign-in to complete...")
    print("  (the browser does not call back here -- the CLI polls Proton)")
    print()

    try:
        code = _wait(proc, timeout)
        if code == 0:
            print("  Signed in. Verify it survives a reboot:")
            print("      sudo reboot && sync.py status --probe")
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
    return code


def _wait(proc: subprocess.Popen, timeout: int) -> int:
    """Wait for `auth login` to finish and log how it ended."""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        log.error("login.timed_out", seconds=timeout)
        return 2
    except KeyboardInterrupt:
        proc.kill()
        log.warn("login.interrupted")
        return 2
    code = proc.returncode or 0
    if code == 0:
        log.info("login.succeeded")
    else:
        stderr = (proc.stderr.read() or "").strip() if proc.stderr else ""
        log.error("login.failed", exit_code=code, detail=stderr[:400])
    return code
