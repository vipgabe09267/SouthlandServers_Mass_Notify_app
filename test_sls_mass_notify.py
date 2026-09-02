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

    def test_existing_profile_migrates_to_live_basic(self):
        profile = app.normalize_endpoint(
            {
                "endpoint": "https://pbx.example.com/api/old",
                "auth_mode": "token",
                "delivery_mode": "poll",
                "token": "retired-token",
                "poll_seconds": 37,
                "username": "frontdesk",
                "password": "secret",
            },
            0,
        )
        self.assertEqual(app.DELIVERY_LIVE, profile["delivery_mode"])
        self.assertEqual(app.AUTH_BASIC, profile["auth_mode"])
        self.assertEqual("https://pbx.example.com/api/old", profile["endpoint"])
        self.assertNotIn("token", profile)
        self.assertNotIn("poll_seconds", profile)

    def test_removed_root_transport_fields_are_not_persisted(self):
        config = app.normalize_config(
            {
                "poll_seconds": 37,
                "token": "retired-token",
                "no_token": True,
                "endpoints": [
                    {
                        "endpoint": "https://pbx.example.com",
                        "delivery_mode": "poll",
                        "auth_mode": "token",
                    }
                ],
            }
        )
        self.assertNotIn("poll_seconds", config)
        self.assertNotIn("token", config)
        self.assertNotIn("no_token", config)
        self.assertEqual(app.DELIVERY_LIVE, config["endpoints"][0]["delivery_mode"])

    def test_removed_transport_fields_do_not_change_worker_signature(self):
        first = app.normalize_endpoint({"endpoint": "https://one.example.com", "poll_seconds": 10}, 0)
        second = dict(first, poll_seconds=45, delivery_mode="poll", auth_mode="token")
        self.assertEqual(app.endpoint_worker_signature(first), app.endpoint_worker_signature(second))

    def test_pbx_address_normalization_and_transport_urls(self):
        profile = {"endpoint": "PBX.Example.com/ignored", "delivery_mode": app.DELIVERY_LIVE}
        self.assertEqual("https://pbx.example.com", app.normalize_pbx_address(profile["endpoint"]))
        self.assertEqual(
            "https://pbx.example.com/api/sipnotify/desktop/stream",
            app.pbx_live_url(profile),
        )
        self.assertEqual(
            "https://pbx.example.com/api/sipnotify/desktop?limit=25",
            app.pbx_recent_url(profile),
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

    def test_installer_rejects_unrelated_nonempty_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "unrelated"
            target.mkdir()
            (target / "important.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty folder"):
                installer.validate_install_dir(target)

    def test_installer_allows_existing_managed_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "SLS Mass Notify"
            target.mkdir()
            (target / installer.EXE_NAME).write_bytes(b"test")
            self.assertEqual(target.resolve(), installer.validate_install_dir(target))


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
        worker.signature = app.endpoint_worker_signature(profile)
        instance = app.MassNotifyApp.__new__(app.MassNotifyApp)
        instance.worker_lock = threading.RLock()
        instance.workers = {0: worker}
        instance.transport_statuses = {0: ("AUTHENTICATED", "live authenticated")}

        self.assertTrue(instance.transport_is_authenticated(0, profile))
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
        live_worker.signature = app.endpoint_worker_signature(profile)
        instance = app.MassNotifyApp.__new__(app.MassNotifyApp)
        instance.config_lock = threading.RLock()
        instance.worker_lock = threading.RLock()
        instance.connection_test_event = threading.Event()
        instance.config = {"enabled": True, "endpoints": [profile]}
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
        worker = app.EndpointTransportWorker(FakeApp(), 0, profile)
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
        with self.assertRaisesRegex(ValueError, "HTTPS"):
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
        )
        stream = io.BytesIO(
            b"event: notification\n"
            b'data: {"desktop_all":true,"message":"too early"}\n\n'
        )
        with self.assertRaises(app.StreamProtocolError):
            worker._consume_stream(stream)


class NetworkSecurityTests(unittest.TestCase):
    def test_same_origin_redirect_handler_blocks_credential_forwarding(self):
        handler = app.SameOriginRedirectHandler()
        request = app.urllib.request.Request("https://pbx.example.com/stream")
        with self.assertRaisesRegex(app.urllib.error.HTTPError, "different origin"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://attacker.example.net/collect",
            )

    def test_alert_media_is_limited_to_same_https_origin(self):
        alert = app.extract_alert(
            {"message": "test", "image_url": "https://media.example.net/alert.png"},
            "",
        )
        app.normalize_alert_urls(alert, "https://pbx.example.com")
        self.assertEqual("", alert.image_url)

        alert = app.extract_alert(
            {"message": "test", "image_url": "/media/alert.png"},
            "",
        )
        app.normalize_alert_urls(alert, "https://pbx.example.com")
        self.assertEqual("https://pbx.example.com/media/alert.png", alert.image_url)

    def test_fetch_endpoint_rejects_oversize_response(self):
        class FakeResponse(io.BytesIO):
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        opener = mock.Mock()
        opener.open.return_value = FakeResponse(b"x" * (1024 * 1024 + 1))
        with mock.patch.object(app, "build_http_opener", return_value=opener):
            with self.assertRaisesRegex(app.ApiError, "safety limit"):
                app.fetch_endpoint("https://pbx.example.com/api/sipnotify/desktop")


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
        self.assertFalse(app.notification_routed_to_client({"message": "unrouted"}, profile))

    def test_reconciliation_does_not_replay_current_latest(self):
        data = {
            "events": [{"id": "one"}, {"id": "two"}],
            "latest": {"id": "two"},
        }
        self.assertEqual([], app.reconciliation_records(data, "two"))
        self.assertEqual([{"id": "two"}], app.reconciliation_records(data, ""))


if __name__ == "__main__":
    unittest.main()
