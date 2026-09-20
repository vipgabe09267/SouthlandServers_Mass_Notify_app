"""Live-only desktop policy regressions; temporary state and simulated transport."""
from datetime import datetime, timezone
import io
from unittest import mock

import sls_mass_notify as client
from sls_presentation import presentation_policy
from sls_store import Inbox
from test_sls_client_reliability import IsolatedClientTest, authenticated, notification, sse


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class LiveDeliveryTests(IsolatedClientTest):
    def test_startup_expires_previous_queue_but_preserves_history_and_receipts(self):
        path = self.path / "notifications.sqlite3"
        previous = Inbox(path)
        pending, _ = previous.receive(self.namespace, "pending", {}, receipt_event_id="pending")
        shown, _ = previous.receive(self.namespace, "shown", {})
        previous.mark(shown, "displayed")
        previous.close()
        with mock.patch.object(client, "load_config", return_value=self.instance.config), \
             mock.patch.object(client, "AlertPresenter"), mock.patch.object(client, "TrayIcon"), \
             mock.patch.object(client.threading.Thread, "start"):
            restarted = client.MassNotifyApp(mock.Mock(), False)
        self.addCleanup(restarted.inbox.close)
        self.assertEqual(restarted.inbox.pending(), [])
        self.assertEqual(restarted.inbox.record(pending)["state"], "expired")
        self.assertEqual(restarted.inbox.record(shown)["state"], "expired")
        self.assertEqual(len(restarted.inbox.pending_receipts(self.namespace)), 1)

    def test_announcement_at_ten_minutes_is_never_presented(self):
        now = 1800000000
        with mock.patch.object(client.time, "time", return_value=now):
            self.instance.accept_alert(0, self.alert(kind="announcement", created_at=stamp(now - 600),
                                                   display_timeout_seconds=0))
            self.instance.pump_inbox()
        self.instance.presenter.submit.assert_not_called()
        self.assertEqual(self.instance.inbox.history()[0]["state"], "expired")

    def test_fresh_announcement_expires_at_original_send_time_plus_ten_minutes(self):
        now = 1800000000
        with mock.patch.object(client.time, "time", return_value=now):
            alert = self.alert(kind="announcement", created_at=stamp(now - 599),
                               timestamp=stamp(now - 9000), display_timeout_seconds=0)
            self.instance.accept_alert(0, alert)
            self.instance.pump_inbox()
            record = self.instance.inbox.history()[0]
            self.assertEqual(record["expires"], now + 1)
            self.assertEqual(presentation_policy(alert).expires.timestamp(), now + 1)
            self.assertTrue(self.instance.can_display_record(record["record_id"]))
        self.instance.presenter.submit.assert_called_once()
        with mock.patch.object(client.time, "time", return_value=now + 1):
            self.assertFalse(self.instance.can_display_record(record["record_id"]))
            self.assertEqual(self.instance.inbox.pending(), [])
            self.assertEqual(self.instance.inbox.record(record["record_id"])["state"], "expired")

    def test_sender_cannot_extend_cutoff_and_shorter_expiry_is_respected(self):
        now = 1800000000
        for index, (timeout, deadline, expected) in enumerate((
            (0, now + 3600, now + 600),
            (3600, now + 3600, now + 600),
            (30, now + 30, now + 30),
        )):
            with self.subTest(timeout=timeout), mock.patch.object(client.time, "time", return_value=now):
                alert = self.alert(f"notice-{index}", kind="announcement", created_at=stamp(now),
                                   display_timeout_seconds=timeout, display_expires_at=stamp(deadline))
                self.instance.accept_alert(0, alert)
                record = next(row for row in self.instance.inbox.history() if row["event_id"] == alert.event_id)
                self.assertEqual(record["expires"], expected)
                self.assertEqual(client.alert_expiry(alert), expected)
                self.assertEqual(presentation_policy(alert).expires.timestamp(), expected)

    def test_missing_or_future_publish_time_cannot_make_unbounded_queue(self):
        now = 1800000000
        with mock.patch.object(client.time, "time", return_value=now):
            for index, published in enumerate(("", stamp(now + 9000))):
                self.instance.accept_alert(0, self.alert(f"notice-{index}", kind="announcement",
                    created_at=published, display_timeout_seconds=0))
            self.assertEqual([row["expires"] for row in self.instance.inbox.history()], [now + 600] * 2)

    def test_notification_scheduled_beyond_delivery_deadline_is_expired(self):
        now = 1800000000
        with mock.patch.object(client.time, "time", return_value=now):
            self.instance.accept_alert(0, self.alert(kind="announcement", created_at=stamp(now),
                effective=stamp(now + 900), display_timeout_seconds=0))
            self.assertEqual(self.instance.inbox.pending(), [])
            self.assertEqual(self.instance.inbox.history()[0]["state"], "expired")

    def test_weather_keeps_source_validity_when_received_live(self):
        now = 1800000000
        with mock.patch.object(client.time, "time", return_value=now):
            self.instance.accept_alert(0, self.alert(created_at=stamp(now), expires=stamp(now + 3600)))
            self.assertEqual(self.instance.inbox.history()[0]["expires"], now + 3600)

    def test_resume_retires_queue_and_old_socket_before_any_popup_callback(self):
        now = 1800000000
        with mock.patch.object(client.time, "time", return_value=now):
            self.instance._last_delivery_tick = now
            worker = self.worker()
            self.instance.accept_alert(0, self.alert())
            record = self.instance.inbox.history()[0]
            self.instance.inbox.mark(record["record_id"], "displayed")
        with mock.patch.object(client.time, "time", return_value=now + 60):
            self.assertFalse(worker._accept_payload(notification("buffered"), "", "buffered"))
            self.assertFalse(self.instance.can_display_record(record["record_id"]))
            self.assertTrue(worker.stop_event.is_set())
            self.assertEqual(self.instance.workers, {})
            self.assertEqual(self.instance.inbox.record(record["record_id"])["state"], "expired")
            self.assertFalse(worker._accept_payload(notification("late"), "", "late"))
            fresh = self.worker()
            self.assertTrue(fresh._accept_payload(notification("fresh"), "", "fresh"))
            self.assertEqual([row["event_id"] for row in self.instance.inbox.pending()], ["fresh"])

    def test_reconnect_drops_waiting_items_only_for_its_profile(self):
        self.instance.accept_alert(0, self.alert("waiting"))
        self.instance.accept_alert(0, self.alert("visible"))
        rows = {row["event_id"]: row for row in self.instance.inbox.history()}
        self.instance.inbox.mark(rows["visible"]["record_id"], "displayed")
        other, _ = self.instance.inbox.receive("other-profile", "other", {})
        worker = self.worker()
        def opening(*_args):
            worker.stop_event.set()
            raise OSError("End simulated reconnect")
        with mock.patch.object(client, "open_sse_response", side_effect=opening):
            worker._run_live()
        self.assertEqual(self.instance.inbox.record(rows["waiting"]["record_id"])["state"], "expired")
        self.assertEqual(self.instance.inbox.record(rows["visible"]["record_id"])["state"], "displayed")
        self.assertEqual(self.instance.inbox.record(other)["state"], "pending")

    def test_stale_socket_buffer_is_rejected_even_before_ui_resumes(self):
        clock = [1800000000]
        class PausedResponse(io.BytesIO):
            def readline(self, size):
                line = super().readline(size)
                if line.startswith(b"id: buffered"):
                    clock[0] += 120
                return line
        response = PausedResponse(authenticated() + sse("notification", notification("buffered"), "buffered"))
        with mock.patch.object(client.time, "time", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(client.StreamProtocolError, "buffered notifications discarded"):
                self.worker()._consume_stream(response)
        self.assertEqual(self.instance.inbox.history(), [])
