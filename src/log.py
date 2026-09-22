"""Structured logging.

One JSON line per event on stdout so `journalctl -u proton-to-immich-pipeline` stays
greppable. Human mode is for interactive use.
"""

from __future__ import annotations

import json
import re
import sys
import time
from typing import Any

# Docker pull chatter. A failing `docker run` emits screenfuls of it before
# the line that says what actually went wrong, so a head-only truncation
# reports the pull and hides the cause.
_NOISE = re.compile(
    r"^(?:[0-9a-f]{8,}: (?:Pulling fs layer|Waiting|Downloading|Verifying Checksum"
    r"|Download complete|Extracting|Pull complete|Already exists).*"
    r"|Unable to find image .* locally"
    r"|[\w.-]+: Pulling from .*"
    r"|Digest: sha256:[0-9a-f]+"
    r"|Status: (?:Downloaded newer image|Image is up to date).*)$")


def in_container() -> bool:
    """Whether we are running inside a container.

    Used only to sharpen diagnostics -- the commonest container mistakes
    (a host path in the config, a root-owned bind mount) have very different
    fixes from their bare-metal equivalents. `/.dockerenv` is the long-standing
    marker; the other two cover podman. Being wrong either way costs a line of
    advice, so a heuristic is fine.
    """
    from pathlib import Path
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text()
    except OSError:
        return False


def condense(blob: str, limit: int = 500) -> str:
    """Squeeze subprocess output down to something that fits one log line.

    Drops pull progress, then keeps BOTH ends: an argument error puts the
    cause first (`Unknown option '-c'`), a docker failure puts it last.
    """
    kept = [ln.strip() for ln in (blob or "").splitlines()
            if ln.strip() and not _NOISE.match(ln.strip())]
    text = " | ".join(kept) if kept else (blob or "").strip()
    if len(text) <= limit:
        return text
    head = max(limit // 3, 1)
    tail = max(limit - head - 5, 1)
    return f"{text[:head]} ... {text[-tail:]}"

_JSON = True
_VERBOSE = False
_RUN_ID = "-"


def configure(json_logs: bool = True, verbose: bool = False, run_id: str = "-") -> None:
    global _JSON, _VERBOSE, _RUN_ID
    _JSON = json_logs
    _VERBOSE = verbose
    _RUN_ID = run_id


def set_run_id(run_id: str) -> None:
    global _RUN_ID
    _RUN_ID = run_id


def is_verbose() -> bool:
    return _VERBOSE


def _emit(level: str, event: str, fields: dict[str, Any]) -> None:
    if _JSON:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "level": level,
            "run_id": _RUN_ID,
            "event": event,
        }
        rec.update(fields)
        line = json.dumps(rec, default=str, ensure_ascii=False)
    else:
        extra = " ".join(f"{k}={v}" for k, v in fields.items())
        line = f"[{level}] {event} {extra}".rstrip()
    stream = sys.stderr if level in ("error", "warn") else sys.stdout
    print(line, file=stream, flush=True)


def debug(event: str, **fields: Any) -> None:
    if _VERBOSE:
        _emit("debug", event, fields)


def info(event: str, **fields: Any) -> None:
    _emit("info", event, fields)


def warn(event: str, **fields: Any) -> None:
    _emit("warn", event, fields)


def error(event: str, **fields: Any) -> None:
    _emit("error", event, fields)


def transition(node_id: str, frm: str, to: str, **fields: Any) -> None:
    """One line per state transition -- Phase 5 acceptance criterion."""
    _emit("info", "transition", {"node_id": node_id, "from": frm, "to": to, **fields})
