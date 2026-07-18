import io
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import sls_mass_notify as app
import sls_installer as installer


class ConfigurationTests(unittest.TestCase):
    def test_new_profile_defaults_to_live_basic(self):
        profile = app.blank_endpoint(0)
        self.assertEqual(app.DELIVERY_LIVE, profile["delivery_mode"])
        self.assertEqual(app.AUTH_BASIC, profile["auth_mode"])

    def test_existing_profile_migrates_to_explicit_polling(self):
        profile = app.normalize_endpoint(
            {"endpoint": "https://pbx.example.com/api/old", "auth_mode": "token"}, 0
        )
        self.assertEqual(app.DELIVERY_POLL, profile["delivery_mode"])

    def test_global_legacy_interval_migrates_into_each_existing_profile(self):
        config = app.normalize_config(
            {
                "poll_seconds": 37,
                "endpoints": [
                    {
                        "endpoint": "https://pbx.example.com",
                        "delivery_mode": app.DELIVERY_POLL,
                        "auth_mode": app.AUTH_TOKEN,
                    }
                ],
            }
        )
        self.assertEqual(37, config["endpoints"][0]["poll_seconds"])

    def test_endpoint_polling_intervals_have_independent_worker_signatures(self):
        first = app.normalize_endpoint({"endpoint": "https://one.example.com", "poll_seconds": 10}, 0)
        second = dict(first, poll_seconds=45)
        self.assertNotEqual(app.endpoint_worker_signature(first), app.endpoint_worker_signature(second))

    def test_pbx_address_normalization_and_transport_urls(self):
        profile = {"endpoint": "PBX.Example.com/ignored", "delivery_mode": app.DELIVERY_LIVE}
        self.assertEqual("https://pbx.example.com", app.normalize_pbx_address(profile["endpoint"]))
        self.assertEqual(
            "https://pbx.example.com/api/sipnotify/desktop/stream",
            app.pbx_transport_url(profile),
        )
        self.assertEqual(
            "https://pbx.example.com/api/sipnotify/desktop?limit=25",
            app.pbx_transport_url(profile, app.DELIVERY_POLL),
        )

    def test_idle_status_distinguishes_disabled_and_connection_test(self):
        config = app.default_config()
        config["enabled"] = False
        self.assertEqual("Monitoring is disabled in Settings.", app.monitoring_idle_status(config))
        self.assertEqual(
            "Connection test in progress; monitoring will resume automatically.",
            app.monitoring_idle_status(config, connection_test_running=True),
        )

    def test_idle_status_identifies_missing_basic_password(self):
        config = app.default_config()
        config["endpoints"][0].update(
            {
                "name": "Main PBX",
                "endpoint": "https://pbx.example.com",
                "enabled": True,
                "auth_mode": app.AUTH_BASIC,
                "username": "frontdesk",
                "password": "",
            }
        )
        self.assertEqual(
            "Monitoring is waiting: enter the desktop password for Main PBX.",
            app.monitoring_idle_status(config),
        )

    def test_valid_basic_profile_reports_starting_instead_of_disabled(self):
        config = app.default_config()
        config["endpoints"][0].update(
            {
                "endpoint": "https://pbx.example.com",
                "enabled": True,
                "auth_mode": app.AUTH_BASIC,
                "username": "frontdesk",
                "password": "secret",
            }
        )
        self.assertEqual("Starting PBX monitoring...", app.monitoring_idle_status(config))


class InstallerPreferenceTests(unittest.TestCase):
    def test_silent_reinstall_preserves_saved_startup_preference(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "settings.json"
            config_path.write_text(json.dumps({"startup_enabled": True}), encoding="utf-8")
            with (
                mock.patch.object(installer, "CONFIG_PATH", config_path),
                mock.patch.object(installer, "startup_entry_enabled", return_value=False),
            ):
                self.assertTrue(installer.saved_startup_preference(default=False))


class ConnectionTestTests(unittest.TestCase):
    def test_authenticated_worker_is_reused_without_stopping_live_monitoring(self):
        profile = app.normalize_endpoint(
            {
                "endpoint": "https://pbx.example.com",
                "enabled": True,
                "auth_mode": app.AUTH_BASIC,
                "delivery_mode": app.DELIVERY_LIVE,
                "username": "frontdesk",
                "password": "secret",
            },
            0,
        )
        worker = mock.Mock()
        worker.thread.is_alive.return_value = True
        worker.signature = app.endpoint_worker_signature(profile, 10)
        instance = app.MassNotifyApp.__new__(app.MassNotifyApp)
        instance.worker_lock = threading.RLock()
        instance.workers = {0: worker}
        instance.transport_statuses = {0: ("AUTHENTICATED", "live authenticated")}

        self.assertTrue(instance.transport_is_authenticated(0, profile, 10))
        worker.stop.assert_not_called()

    def test_connection_test_returns_immediately_for_authenticated_worker(self):
        profile = app.normalize_endpoint(
            {
                "endpoint": "https://pbx.example.com",
                "enabled": True,
                "auth_mode": app.AUTH_BASIC,
                "delivery_mode": app.DELIVERY_LIVE,
                "username": "frontdesk",
                "password": "secret",
            },
            0,
        )
        live_worker = mock.Mock()
        live_worker.thread.is_alive.return_value = True
        live_worker.signature = app.endpoint_worker_signature(profile, 10)
        instance = app.MassNotifyApp.__new__(app.MassNotifyApp)
        instance.config_lock = threading.RLock()
        instance.worker_lock = threading.RLock()
        instance.connection_test_event = threading.Event()
        instance.config = {"enabled": True, "poll_seconds": 10, "endpoints": [profile]}
        instance.workers = {0: live_worker}
        instance.transport_statuses = {0: ("AUTHENTICATED", "live authenticated")}
        instance.ui_queue = queue.Queue()
        instance.status_text = "Live authenticated"

        instance.test_now(lambda _message: None)
        kind, (_callback, message) = instance.ui_queue.get(timeout=1)

        self.assertEqual("test_result", kind)
        self.assertEqual("Test succeeded: 1/1 PBX profile authenticated.", message)
        self.assertFalse(instance.connection_test_event.is_set())
        live_worker.stop.assert_not_called()


class LiveCatchupTests(unittest.TestCase):
    def test_targeted_announcement_missed_by_stream_is_recovered(self):
        accepted_alerts = []

        class FakeApp:
            def get_endpoint_state(self, _index, _key):
                return "announcement-old"

            def accept_alert(self, _index, alert, _signature):
                accepted_alerts.append(alert)
                return True

            def set_transport_status(self, *_args):
                pass

            def record_fault(self, *_args):
                pass

            def clear_fault(self, *_args):
                pass

        profile = {
            "endpoint": "https://pbx.example.com",
            "auth_mode": app.AUTH_BASIC,
            "delivery_mode": app.DELIVERY_LIVE,
            "username": "desktop-user",
            "password": "secret",
        }
        old = {"id": "announcement-old", "kind": "announcement", "desktop_all": True}
        targeted = {
            "id": "announcement-new",
            "kind": "announcement",
            "title": "Announcement",
            "message": "Targeted desktop message",
            "desktop_all": False,
            "desktop_recipients": ["desktop-user"],
        }
        worker = app.EndpointTransportWorker(FakeApp(), 0, profile, 10)
        with mock.patch.object(
            app,
            "fetch_endpoint",
            return_value=({"ok": True, "events": [old, targeted], "latest": targeted}, ""),
        ):
            count = worker._live_catchup_once()

        self.assertEqual(1, count)
        self.assertEqual(["announcement-new"], [alert.event_id for alert in accepted_alerts])


class SseProtocolTests(unittest.TestCase):
    def test_stream_request_uses_basic_auth_and_resume_header(self):
        class FakeSocket:
            timeout = None

            def settimeout(self, value):
                self.timeout = value

        class FakeResponse(io.BytesIO):
            headers = {"Content-Type": "text/event-stream; charset=utf-8"}

            def __init__(self):
                super().__init__()
                self.stream_socket = FakeSocket()
                self.fp = type("FakeFp", (), {"raw": type("FakeRaw", (), {"_sock": self.stream_socket})()})()

        class FakeOpener:
            request = None

            def open(self, request, timeout):
                self.request = request
                return FakeResponse()

        opener = FakeOpener()
        profile = {
            "endpoint": "https://pbx.example.com",
            "auth_mode": app.AUTH_BASIC,
            "username": "frontdesk",
            "password": "secret",
            "last_event_id": "announcement-7",
        }
        with mock.patch.object(app, "build_http_opener", return_value=opener):
            response = app.open_sse_response(profile)
            self.assertEqual(app.SSE_READ_TIMEOUT_SECONDS, response.stream_socket.timeout)
            response.close()
        self.assertEqual("text/event-stream", opener.request.get_header("Accept"))
        self.assertEqual("announcement-7", opener.request.get_header("Last-event-id"))
        self.assertTrue(opener.request.get_header("Authorization").startswith("Basic "))

    def test_stream_refuses_insecure_basic_credentials(self):
        with self.assertRaises(app.TlsRequiredError):
            app.open_sse_response(
                {
                    "endpoint": "http://pbx.example.com",
                    "auth_mode": app.AUTH_BASIC,
                    "username": "frontdesk",
                    "password": "secret",
                }
            )

    def test_incremental_sse_parsing_preserves_id_and_ignores_comments(self):
        body = io.BytesIO(
            b"retry: 1000\n"
            b": keepalive 1\n\n"
            b"event: authenticated\n"
            b'data: {"ok":true,"transport":"live_sse"}\n\n'
            b"id: announcement-1\n"
            b"event: notification\n"
            b'data: {"id":"announcement-1","message":"hello"}\n\n'
            b"event: reconnect\n"
            b'data: {"ok":true}\n\n'
        )
        activity = []
        events = list(app.iter_sse_events(body, on_activity=lambda: activity.append(True)))
        self.assertEqual(["authenticated", "notification", "reconnect"], [item.name for item in events])
        self.assertEqual("announcement-1", events[1].event_id)
        self.assertGreater(len(activity), len(events))

    def test_oversize_line_is_rejected(self):
        body = io.BytesIO(b"data: " + (b"x" * app.SSE_MAX_LINE_BYTES) + b"\n\n")
        with self.assertRaises(app.StreamProtocolError):
            list(app.iter_sse_events(body))

    def test_worker_requires_handshake_before_notification(self):
        class FakeApp:
            def set_transport_status(self, *_args):
                pass

            def record_fault(self, *_args):
                pass

            def clear_fault(self, *_args):
                pass

            def accept_alert(self, *_args):
                return True

        worker = app.EndpointTransportWorker(
            FakeApp(),
            0,
            {
                "endpoint": "https://pbx.example.com",
                "auth_mode": app.AUTH_BASIC,
                "delivery_mode": app.DELIVERY_LIVE,
                "username": "frontdesk",
                "password": app.protect_secret("secret"),
            },
            10,
        )
        stream = io.BytesIO(
            b"event: notification\n"
            b'data: {"desktop_all":true,"message":"too early"}\n\n'
        )
        with self.assertRaises(app.StreamProtocolError):
            worker._consume_stream(stream)


class NotificationContractTests(unittest.TestCase):
    def test_presentation_and_text_fallback_contract(self):
        payload = {
            "kind": "alert",
            "event": "Weather Warning",
            "title": "Safety Notice",
            "message": "preferred",
            "body": "secondary",
            "presentation": {
                "style": "weather_alert",
                "background_color": "#991B1B",
                "header_color": "#7f1d1d",
                "accent_color": "#fecaca",
                "text_color": "#ffffff",
            },
        }
        alert = app.extract_alert(payload, "")
        self.assertEqual("preferred", alert.body)
        self.assertEqual("Safety Notice", alert.title)
        self.assertEqual("#991b1b", alert.background_color)
        self.assertEqual("#7f1d1d", alert.header_color)

    def test_lightning_test_gets_test_only_marker(self):
        alert = app.extract_alert(
            {
                "kind": "announcement",
                "title": "Lightning Test",
                "message": "Configured radius test",
                "background_color": "#92400e",
            },
            "",
        )
        self.assertTrue(alert.test_only)
        self.assertEqual("#92400e", alert.background_color)

    def test_basic_profile_enforces_desktop_routing(self):
        profile = {"auth_mode": app.AUTH_BASIC, "username": "frontdesk"}
        self.assertTrue(app.notification_routed_to_client({"desktop_all": True}, profile))
        self.assertTrue(
            app.notification_routed_to_client({"desktop_recipients": ["frontdesk"]}, profile)
        )
        self.assertFalse(
            app.notification_routed_to_client({"desktop_recipients": ["warehouse"]}, profile)
        )
        self.assertFalse(app.notification_routed_to_client({"message": "legacy"}, profile))

    def test_polling_does_not_replay_current_latest(self):
        data = {
            "events": [{"id": "one"}, {"id": "two"}],
            "latest": {"id": "two"},
        }
        self.assertEqual([], app.polling_records(data, "two"))
        self.assertEqual([{"id": "two"}], app.polling_records(data, ""))


if __name__ == "__main__":
    unittest.main()
