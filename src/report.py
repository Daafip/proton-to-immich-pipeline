"""status.json and the Home Assistant signal.

The failure this pipeline is most likely to hide is a silently expired Proton
session, so `auth_ok` is a first-class field and gets its own binary_sensor.

MQTT is optional and dependency-free: paho-mqtt if it happens to be installed,
otherwise the mosquitto_pub binary. If neither exists, status.json is still
written and can be read by an HA `file`/`command_line` sensor.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import log, state


def _disk(path: Path) -> tuple[float | None, float | None]:
    """Free space on the filesystem holding `path`.

    Walks up to the nearest existing ancestor: an account whose staging
    subtree has not been created yet still lives on a filesystem with a
    known amount of free space, and reporting "None GB" for it would look
    like a broken sensor rather than an empty directory.
    """
    for candidate in (path, *path.parents):
        try:
            usage = shutil.disk_usage(str(candidate))
        except OSError:
            continue
        return (round(usage.free / 1e9, 2),
                round(usage.used / usage.total * 100, 1) if usage.total else None)
    return None, None


def build_status(conn: sqlite3.Connection, cfg, auth_ok: bool | None = None,
                 immich_ok: bool | None = None) -> dict[str, Any]:
    """One account's health. `cfg` is that account's Account object."""
    account = str(getattr(cfg, "account_name", None) or "default")
    counts = state.counts(conn, account)
    last = state.last_run(conn, account)
    success = state.last_success(conn, account)
    stale_hours = int(cfg.get("report.stale_success_hours", 48))

    last_success_ts = success["finished_at"] if success else None
    parsed = state.parse_ts(last_success_ts)
    stale = True
    if parsed is not None:
        stale = datetime.now(timezone.utc) - parsed > timedelta(hours=stale_hours)

    free_gb, used_pct = _disk(cfg.staging)

    return {
        "schema": 2,
        "account": account,
        "generated_at": state.utcnow(),
        "last_run": last["started_at"] if last else None,
        "last_run_finished": last["finished_at"] if last else None,
        "last_run_exit_code": last["exit_code"] if last else None,
        "last_success": last_success_ts,
        "stale": stale,
        "new": last["discovered"] if last else 0,
        "downloaded": last["downloaded"] if last else 0,
        "uploaded": last["uploaded"] if last else 0,
        "failed": last["failed"] if last else 0,
        "backlog": state.backlog(conn, account),
        "uploaded_total": state.uploaded_total(conn, account),
        "quarantined": counts.get(state.QUARANTINED, 0),
        # v2: how many files someone has trashed in Immich and not yet
        # deleted from Proton. A number that only ever grows is the signal
        # that nobody is working the queue.
        "staged_deletes": state.count_staged(conn, account),
        "delete_failed": counts.get(state.DELETE_FAILED, 0),
        "delete_action": str(getattr(cfg, "delete_action", "mark_only")),
        "auth_ok": bool(auth_ok) if auth_ok is not None else None,
        "immich_ok": bool(immich_ok) if immich_ok is not None else None,
        "counts": {k: v for k, v in counts.items() if k != "total"},
        "total_assets": counts.get("total", 0),
        "staging_free_gb": free_gb,
        "staging_used_pct": used_pct,
    }


def write_status(path: str | Path, data: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".status-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


SENSORS: list[dict[str, Any]] = [
    {"key": "backlog", "name": "Backlog", "icon": "mdi:tray-full", "unit": "files"},
    {"key": "uploaded", "name": "Uploaded last run", "icon": "mdi:cloud-upload", "unit": "files"},
    {"key": "new", "name": "New last run", "icon": "mdi:image-plus", "unit": "files"},
    {"key": "failed", "name": "Failed last run", "icon": "mdi:alert-circle", "unit": "files"},
    {"key": "quarantined", "name": "Quarantined", "icon": "mdi:biohazard", "unit": "files"},
    {"key": "staged_deletes", "name": "Staged for deletion",
     "icon": "mdi:delete-clock", "unit": "files"},
    {"key": "staging_free_gb", "name": "Staging free", "icon": "mdi:harddisk", "unit": "GB"},
    {"key": "last_run", "name": "Last run", "device_class": "timestamp"},
    {"key": "last_success", "name": "Last success", "device_class": "timestamp"},
]

BINARY_SENSORS: list[dict[str, Any]] = [
    {"key": "auth_ok", "name": "Proton auth", "device_class": "problem", "invert": True},
    {"key": "stale", "name": "Sync stale", "device_class": "problem"},
]


def mqtt_identity(cfg) -> tuple[str, str, str]:
    """(discovery_prefix, node_id, state_topic) for this account.

    Suffixed per account: two accounts publishing to one retained topic would
    each overwrite the other's state, and Home Assistant would show whichever
    ran last as the whole truth.
    """
    prefix = str(cfg.get("mqtt.discovery_prefix", "homeassistant"))
    node = str(cfg.get("mqtt.node_id", "proton_immich_sync"))
    topic = str(cfg.get("mqtt.state_topic", "proton_immich_sync/state"))
    account = str(getattr(cfg, "account_name", None) or "default")
    if account != "default" and not node.endswith(account):
        node = f"{node}_{account}"
        base, _, leaf = topic.rpartition("/")
        topic = f"{base}/{account}/{leaf}" if base else f"{account}/{topic}"
    return prefix, node, topic


def discovery_payloads(cfg) -> list[tuple[str, dict[str, Any]]]:
    prefix, node, state_topic = mqtt_identity(cfg)
    account = str(getattr(cfg, "account_name", None) or "default")
    label = ("Proton to Immich sync" if account == "default"
             else f"Proton to Immich sync ({account})")
    device = {
        "identifiers": [node],
        "name": label,
        "manufacturer": "proton-to-immich-pipeline",
        "model": "pipeline",
    }
    out: list[tuple[str, dict[str, Any]]] = []
    for sensor in SENSORS:
        key = sensor["key"]
        payload = {
            "name": sensor["name"],
            "unique_id": f"{node}_{key}",
            "state_topic": state_topic,
            "value_template": (
                "{{ value_json.%s if value_json.%s is not none else none }}" % (key, key)
            ),
            "json_attributes_topic": state_topic,
            "device": device,
        }
        if sensor.get("unit"):
            payload["unit_of_measurement"] = sensor["unit"]
        if sensor.get("icon"):
            payload["icon"] = sensor["icon"]
        if sensor.get("device_class"):
            payload["device_class"] = sensor["device_class"]
        out.append((f"{prefix}/sensor/{node}/{key}/config", payload))

    for sensor in BINARY_SENSORS:
        key = sensor["key"]
        # device_class "problem": ON means something is wrong.
        truth = "false" if sensor.get("invert") else "true"
        template = (
            "{%% if value_json.%s is none %%}OFF"
            "{%% elif value_json.%s | string | lower == '%s' %%}ON{%% else %%}OFF{%% endif %%}"
            % (key, key, truth)
        )
        out.append((
            f"{prefix}/binary_sensor/{node}/{key}/config",
            {
                "name": sensor["name"],
                "unique_id": f"{node}_{key}",
                "state_topic": state_topic,
                "value_template": template,
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": sensor["device_class"],
                "device": device,
            },
        ))
    return out


class MqttPublisher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.host = cfg.get("mqtt.host", "127.0.0.1")
        self.port = int(cfg.get("mqtt.port", 1883))
        self.username = cfg.get("mqtt.username", "")
        self.password = cfg.get("mqtt.password", "")
        self.retain = bool(cfg.get("mqtt.retain", True))
        self.binary = cfg.get("mqtt.mosquitto_pub", "mosquitto_pub")

    def _publish_paho(self, messages: list[tuple[str, str]]) -> bool:
        try:
            import paho.mqtt.client as mqtt  # type: ignore
        except ImportError:
            return False
        client = mqtt.Client()
        if self.username:
            client.username_pw_set(self.username, self.password)
        client.connect(self.host, self.port, keepalive=30)
        client.loop_start()
        try:
            for topic, payload in messages:
                info = client.publish(topic, payload, retain=self.retain, qos=1)
                info.wait_for_publish(timeout=10)
        finally:
            client.loop_stop()
            client.disconnect()
        return True

    def _publish_mosquitto(self, messages: list[tuple[str, str]]) -> bool:
        if not shutil.which(self.binary):
            return False
        for topic, payload in messages:
            argv = [self.binary, "-h", self.host, "-p", str(self.port),
                    "-t", topic, "-m", payload]
            if self.retain:
                argv.append("-r")
            if self.username:
                argv += ["-u", self.username, "-P", self.password]
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip()[:200])
        return True

    def publish(self, status: dict[str, Any], with_discovery: bool = True) -> bool:
        messages: list[tuple[str, str]] = []
        if with_discovery:
            messages += [(topic, json.dumps(payload))
                         for topic, payload in discovery_payloads(self.cfg)]
        messages.append((mqtt_identity(self.cfg)[2], json.dumps(status)))
        try:
            if self._publish_paho(messages):
                return True
            if self._publish_mosquitto(messages):
                return True
        except Exception as exc:  # noqa: BLE001 - reporting must never fail a run
            log.warn("mqtt.publish_failed", detail=str(exc)[:200])
            return False
        log.warn("mqtt.no_client", detail="install paho-mqtt or mosquitto-clients")
        return False


def publish(conn: sqlite3.Connection, cfg, auth_ok: bool | None = None,
            immich_ok: bool | None = None) -> dict[str, Any]:
    status = build_status(conn, cfg, auth_ok=auth_ok, immich_ok=immich_ok)
    path = cfg.status_path
    if path:
        write_status(path, status)
        log.debug("report.status_written", path=str(path))
    if cfg.get("mqtt.enabled"):
        MqttPublisher(cfg).publish(status)
    return status
