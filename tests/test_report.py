"""status.json and the MQTT publish path.

This had no tests, which is how three bugs lived here at once: paho-mqtt 2.x
could not be constructed at all, its failure took the `mosquitto_pub` fallback
down with it, and nothing logged on success — so a broker that received
nothing looked exactly like one that received everything.

The MQTT clients are injected rather than installed: `paho` goes into
`sys.modules` and `mosquitto_pub` is faked at the `subprocess`/`shutil` seam,
so these run offline with neither package present.
"""

import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import log, report, state  # noqa: E402
from src.config import DEFAULTS, Config  # noqa: E402
from tests.helpers import silence_logs  # noqa: E402


def make_paho(api_version_required: bool, fail_on: str | None = None):
    """A stand-in paho module.

    `api_version_required` models the 2.0 breaking change: `Client()` with no
    `callback_api_version` raises. 1.x takes no such argument at all.
    """
    module = types.ModuleType("paho.mqtt.client")
    module.SENT = []
    module.ORDER = []

    class CallbackAPIVersion:
        VERSION1 = 1
        VERSION2 = 2

    class Info:
        def wait_for_publish(self, timeout=None):
            return True

        def is_published(self):
            return fail_on != "puback"

    class Client:
        def __init__(self, callback_api_version=None, *a, **kw):
            if api_version_required and callback_api_version is None:
                raise ValueError(
                    "Unsupported callback API version: version 2.0 added a "
                    "callback_api_version")
            if not api_version_required and callback_api_version is not None:
                raise TypeError("__init__() takes 1 positional argument")
            module.ORDER.append("construct")

        def username_pw_set(self, user, password):
            module.ORDER.append("auth")
            module.AUTH = (user, password)

        def connect(self, host, port, keepalive=30):
            if fail_on == "connect":
                raise ConnectionRefusedError("[Errno 111] Connection refused")
            module.ORDER.append("connect")
            module.ENDPOINT = (host, port)

        def loop_start(self):
            module.ORDER.append("loop_start")
            # The broker's answer: 5 is "not authorised".
            if fail_on != "no_connack":
                self.on_connect(self, None, {}, 5 if fail_on == "refused" else 0)

        def publish(self, topic, payload, retain=False, qos=0):
            module.SENT.append((topic, payload, retain, qos))
            return Info()

        def disconnect(self):
            module.ORDER.append("disconnect")

        def loop_stop(self):
            module.ORDER.append("loop_stop")

    module.CallbackAPIVersion = CallbackAPIVersion
    module.Client = Client
    if not api_version_required:
        del module.CallbackAPIVersion
    return module


class ReportTest(unittest.TestCase):
    def setUp(self):
        silence_logs()
        self.tmp = tempfile.TemporaryDirectory()
        data = copy.deepcopy(DEFAULTS)
        data["staging"]["root"] = self.tmp.name
        data["immich"]["url"] = "http://vm:2283/api"
        data["immich"]["api_key"] = "k"
        data["mqtt"]["enabled"] = True
        self.cfg = Config(data)
        self.account = self.cfg.account(None)
        self.account.state_dir.mkdir(parents=True, exist_ok=True)
        self.conn = state.connect(self.account.db_path)
        state.init_schema(self.conn, self.account.account_name)

        self.events = []
        log._emit = lambda level, event, fields: self.events.append(
            (level, event, fields))
        self.addCleanup(silence_logs)

        # No real clients anywhere.
        self._saved = {k: v for k, v in sys.modules.items()
                       if k.startswith("paho")}
        for key in list(sys.modules):
            if key.startswith("paho"):
                del sys.modules[key]
        self.mosquitto_runs = []
        self._which = report.shutil.which
        self._run = report.subprocess.run
        report.shutil.which = lambda binary: None
        self.addCleanup(self._restore)

    def _restore(self):
        report.shutil.which = self._which
        report.subprocess.run = self._run
        for key in list(sys.modules):
            if key.startswith("paho"):
                del sys.modules[key]
        sys.modules.update(self._saved)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # -- injection helpers -------------------------------------------------
    def install_paho(self, **kwargs):
        module = make_paho(**kwargs)
        pkg = types.ModuleType("paho")
        mqtt_pkg = types.ModuleType("paho.mqtt")
        mqtt_pkg.client = module
        pkg.mqtt = mqtt_pkg
        sys.modules["paho"] = pkg
        sys.modules["paho.mqtt"] = mqtt_pkg
        sys.modules["paho.mqtt.client"] = module
        return module

    def install_mosquitto(self, returncode=0, stderr=""):
        report.shutil.which = lambda binary: f"/usr/bin/{binary}"

        def fake_run(argv, **kwargs):
            self.mosquitto_runs.append(argv)
            return types.SimpleNamespace(returncode=returncode, stderr=stderr,
                                         stdout="")
        report.subprocess.run = fake_run

    def events_named(self, name):
        return [f for level, event, f in self.events if event == name]

    def publish(self):
        return report.MqttPublisher(self.account).publish({"backlog": 3})


# ---------------------------------------------------------------------------
# the bug that started it
# ---------------------------------------------------------------------------

class TestPahoVersions(ReportTest):
    def test_paho_2x_is_constructed_with_a_callback_api_version(self):
        """2.0 made it a required argument. Calling Client() bare raises
        "Unsupported callback API version", and publishing stopped dead."""
        module = self.install_paho(api_version_required=True)
        self.assertTrue(self.publish())
        self.assertEqual(len(module.SENT), 12)
        self.assertIn("construct", module.ORDER)

    def test_paho_1x_is_still_constructed_bare(self):
        """1.x has no such parameter and rejects a positional argument."""
        module = self.install_paho(api_version_required=False)
        self.assertTrue(self.publish())
        self.assertEqual(len(module.SENT), 12)

    def test_a_broken_paho_falls_through_to_mosquitto_pub(self):
        """The regression this is really about: the two transports shared one
        `try`, so anything paho raised skipped the fallback entirely — and
        publishing failed on a box where mosquitto_pub worked by hand."""
        self.install_paho(api_version_required=True, fail_on="connect")
        self.install_mosquitto()
        self.assertTrue(self.publish())
        self.assertEqual(len(self.mosquitto_runs), 12)
        failed = self.events_named("mqtt.transport_failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["transport"], "paho-mqtt")

    def test_a_refused_login_is_a_failure_not_a_success(self):
        """connect() only opens the socket. A broker that answers CONNACK
        "not authorised" used to drop every message while the pipeline
        logged `mqtt.published`."""
        self.install_paho(api_version_required=True, fail_on="refused")
        self.assertFalse(self.publish())
        self.assertFalse(self.events_named("mqtt.published"))
        detail = self.events_named("mqtt.transport_failed")[0]["detail"]
        self.assertIn("refused", detail)

    def test_a_message_without_puback_is_a_failure(self):
        self.install_paho(api_version_required=True, fail_on="puback")
        self.assertFalse(self.publish())
        self.assertIn("PUBACK",
                      self.events_named("mqtt.transport_failed")[0]["detail"])

    def test_no_client_at_all_says_which_to_install(self):
        self.assertFalse(self.publish())
        detail = self.events_named("mqtt.publish_failed")[0]["detail"]
        self.assertIn("paho-mqtt", detail)
        self.assertIn("mosquitto", detail)


# ---------------------------------------------------------------------------
# being able to tell whether it worked
# ---------------------------------------------------------------------------

class TestDiagnostics(ReportTest):
    def test_success_is_logged_with_the_transport_used(self):
        """Without this line, "published" and "quietly did nothing" look the
        same in the journal."""
        self.install_paho(api_version_required=True)
        self.publish()
        sent = self.events_named("mqtt.published")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["transport"], "paho-mqtt")
        self.assertEqual(sent[0]["messages"], 12)
        self.assertEqual(sent[0]["topic"], "proton_immich_sync/state")

    def test_the_mosquitto_transport_names_itself_too(self):
        self.install_mosquitto()
        self.publish()
        self.assertEqual(self.events_named("mqtt.published")[0]["transport"],
                         "mosquitto_pub")

    def test_a_failing_mosquitto_reports_which_message_and_why(self):
        self.install_mosquitto(returncode=1, stderr="Connection refused")
        self.assertFalse(self.publish())
        detail = self.events_named("mqtt.transport_failed")[0]["detail"]
        self.assertIn("Connection refused", detail)
        self.assertIn("1/12", detail)

    def test_disabled_mqtt_is_visible_under_v(self):
        """Debug level, not info: most installs do not use MQTT and do not
        want a line about it every night. But `-v` has to answer "is it even
        switched on", because that is the commonest reason nothing arrives."""
        self.account.set("mqtt.enabled", False)
        log.configure(json_logs=True, verbose=True)
        log._emit = lambda level, event, fields: self.events.append(
            (level, event, fields))
        try:
            report.publish(self.conn, self.account)
        finally:
            log.configure(json_logs=True, verbose=False)
        self.assertTrue(self.events_named("mqtt.disabled"))
        self.assertFalse(self.events_named("mqtt.published"))

    def test_disabled_mqtt_publishes_nothing(self):
        self.account.set("mqtt.enabled", False)
        self.install_paho(api_version_required=True)
        module = sys.modules["paho.mqtt.client"]
        report.publish(self.conn, self.account)
        self.assertEqual(module.SENT, [])


# ---------------------------------------------------------------------------
# what actually goes on the wire
# ---------------------------------------------------------------------------

class TestMessages(ReportTest):
    def test_the_state_topic_is_retained_at_qos_1(self):
        """Home Assistant needs the retained message to repopulate its
        entities after a restart."""
        module = self.install_paho(api_version_required=True)
        self.publish()
        state_msgs = [m for m in module.SENT if m[0] == "proton_immich_sync/state"]
        self.assertEqual(len(state_msgs), 1)
        _, payload, retain, qos = state_msgs[0]
        self.assertTrue(retain)
        self.assertEqual(qos, 1)
        self.assertEqual(json.loads(payload)["backlog"], 3)

    def test_discovery_accompanies_the_state_message(self):
        module = self.install_paho(api_version_required=True)
        self.publish()
        topics = [m[0] for m in module.SENT]
        self.assertTrue(any(t.startswith("homeassistant/sensor/") for t in topics))
        self.assertTrue(any(t.startswith("homeassistant/binary_sensor/")
                            for t in topics))
        self.assertEqual(topics[-1], "proton_immich_sync/state",
                         "discovery first, then the state it refers to")

    def test_credentials_are_passed_to_paho(self):
        self.account.set("mqtt.username", "ha")
        self.account.set("mqtt.password", "s3cret")
        module = self.install_paho(api_version_required=True)
        self.publish()
        self.assertEqual(module.AUTH, ("ha", "s3cret"))

    def test_the_mosquitto_argv_carries_host_port_topic_and_retain(self):
        self.install_mosquitto()
        self.account.set("mqtt.host", "broker.lan")
        self.account.set("mqtt.port", 8883)
        self.publish()
        argv = self.mosquitto_runs[-1]
        self.assertIn("broker.lan", argv)
        self.assertIn("8883", argv)
        self.assertIn("-r", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "proton_immich_sync/state")

    def test_the_connection_is_closed_in_the_right_order(self):
        """disconnect() is written by the network loop, so stopping the loop
        first leaves the broker seeing an unclean disconnect."""
        module = self.install_paho(api_version_required=True)
        self.publish()
        self.assertLess(module.ORDER.index("disconnect"),
                        module.ORDER.index("loop_stop"))


class TestDiscoveryShape(ReportTest):
    def test_every_sensor_has_what_home_assistant_requires(self):
        for topic, payload in report.discovery_payloads(self.account):
            with self.subTest(topic=topic):
                self.assertTrue(topic.startswith("homeassistant/"))
                for key in ("name", "unique_id", "state_topic", "device"):
                    self.assertIn(key, payload)
                self.assertIn("identifiers", payload["device"])

    def test_the_staged_delete_count_is_published(self):
        keys = {p["value_template"] for _, p in report.discovery_payloads(self.account)
                if "staged_deletes" in p.get("value_template", "")}
        self.assertTrue(keys, "the delete queue needs a sensor to be noticed")

    def test_unique_ids_do_not_collide(self):
        ids = [p["unique_id"] for _, p in report.discovery_payloads(self.account)]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
