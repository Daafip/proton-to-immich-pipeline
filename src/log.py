"""Structured logging.

One JSON line per event on stdout so `journalctl -u proton-immich-sync` stays
greppable. Human mode is for interactive use.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

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
