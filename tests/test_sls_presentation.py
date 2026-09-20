import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import sls_presentation as presentation


def alert(**kwargs):
    fields = dict(title="Evacuate", body="Use the east stairwell.", severity="critical",
                  kind="alert", raw={}, incident_id="", revision=None)
    fields.update(kwargs)
    return SimpleNamespace(**fields)


class FakeRoot:
    def after(self, milliseconds, callback):
        return "timer"

    def after_idle(self, callback):
        return "idle"

    def after_cancel(self, timer):
        pass


class FakeView:
    def __init__(self, presenter, item, offset):
        self.item = item
        self.closed = False
        self.status_var = Mock()
        self.queue_var = Mock()
        self.shown_at = 0

    def close(self):
        self.closed = True

    def poll_image(self):
        pass


class PolicyTests(unittest.TestCase):
    def test_test_mode_requires_explicit_server_field(self):
        notification = alert(title="TEST ONLY lightning TEST", test_only=True)
        self.assertIsNone(presentation.explicit_test_mode(notification))
        self.assertIn("not specified", presentation.test_marker(notification))
        for source, expected in (({"test_only": True}, True), ({"test": False}, False),
                                 ({"status": "Exercise"}, True), ({"status": "Actual"}, False),
                                 ({"latest": {"test_only": "true"}}, True)):
            with self.subTest(source=source):
                self.assertIs(presentation.explicit_test_mode(alert(raw=source)), expected)

    def test_explicit_false_is_not_treated_as_truthy_string(self):
        self.assertIs(presentation.explicit_test_mode(alert(raw={"test": "false"})), False)
        self.assertIsNone(presentation.explicit_test_mode(alert(raw={"test": "perhaps"})))

    def test_structured_test_severity_is_explicit_but_legacy_title_is_not(self):
        self.assertTrue(presentation.explicit_test_mode(alert(severity="Test")))
        self.assertTrue(presentation.explicit_test_mode(alert(raw={"severity": "Test"})))
        legacy = alert(title="Lightning Test", body="TEST ONLY lightning TEST", severity="severe")
        self.assertIsNone(presentation.explicit_test_mode(legacy))
        self.assertIn("not specified", presentation.test_marker(legacy))
        self.assertFalse(presentation.explicit_test_mode(alert(raw={"test_only": False, "severity": "Test"})))

    def test_critical_unknown_and_numeric_priority_persist(self):
        for values in ({"severity": "extreme"}, {"severity": "urgent"},
                       {"severity": "", "priority": "1"}, {"severity": "unknown"}):
            with self.subTest(values=values):
                policy = presentation.presentation_policy(alert(**values))
                self.assertTrue(policy.persistent)
                self.assertIsNone(policy.timeout_seconds)

    def test_routine_alert_has_timeout(self):
        policy = presentation.presentation_policy(alert(severity="advisory"))
        self.assertFalse(policy.persistent)
        self.assertEqual(policy.timeout_seconds, 120)
        self.assertFalse(presentation.presentation_policy(alert(kind="announcement", severity="")).persistent)

    def test_announcement_zero_timeout_has_no_auto_expiry(self):
        policy = presentation.presentation_policy(alert(kind="announcement", severity="notice",
            display_timeout_seconds=0, display_expires_at="2000-01-01T00:00:00Z", expires="2000-01-01T00:00:00Z"))
        self.assertIsNone(policy.timeout_seconds)
        self.assertIsNone(policy.expires)
        self.assertFalse(policy.persistent)

    def test_positive_announcement_timeout_uses_absolute_deadline_without_reset(self):
        notification = alert(kind="announcement", severity="notice", raw={"display_timeout_seconds": 60,
            "display_expires_at": "2026-09-20T18:01:00Z"})
        policy = presentation.presentation_policy(notification)
        replay = presentation.presentation_policy(notification)
        self.assertIsNone(policy.timeout_seconds)
        self.assertEqual(policy.expires, datetime(2026, 9, 20, 18, 1, tzinfo=timezone.utc))
        self.assertEqual(policy.expires, replay.expires)

    def test_missing_absolute_announcement_deadline_does_not_start_new_relative_timer(self):
        policy = presentation.presentation_policy(alert(kind="announcement", severity="notice", display_timeout_seconds=30))
        self.assertIsNone(policy.timeout_seconds)
        self.assertIsNone(policy.expires)
        self.assertIn("expiration time", policy.timing_warning)

    def test_weather_expiry_always_applies_even_for_severe_and_extreme(self):
        for severity in ("Severe", "Extreme"):
            with self.subTest(severity=severity):
                policy = presentation.presentation_policy(alert(kind="alert", severity=severity,
                    effective="2026-09-20T18:00:00Z", expires="2026-09-20T19:00:00Z"))
                self.assertEqual(policy.priority, 0)
                self.assertEqual(policy.expires, datetime(2026, 9, 20, 19, tzinfo=timezone.utc))

    def test_moderate_weather_uses_sender_validity_instead_of_popup_timeout(self):
        policy = presentation.presentation_policy(alert(kind="alert", severity="Moderate",
                                                       expires="2026-09-20T19:00:00Z"))
        self.assertFalse(policy.persistent)
        self.assertIsNone(policy.timeout_seconds)
        self.assertIsNotNone(policy.expires)

    def test_title_and_body_cannot_create_lifecycle_actions(self):
        notification = alert(title="All clear: cancelled incident", body="Cancel warning; test only",
                             severity="severe", incident_id="", revision=None)
        self.assertEqual(presentation.incident_key(notification), "")
        self.assertFalse(presentation.can_supersede(notification, notification))
        self.assertEqual(presentation.presentation_policy(notification).priority, 0)

    def test_dates_are_absolute_and_timezone_aware(self):
        self.assertIsNone(presentation.parse_timestamp("2026-09-20T10:00:00"))
        self.assertIsNone(presentation.parse_timestamp("broken"))
        self.assertEqual(presentation.parse_timestamp("2026-09-20T10:00:00-05:00"),
                         datetime(2026, 9, 20, 15, tzinfo=timezone.utc))

    def test_ready_queue_excludes_future_and_expired_and_orders_priority_fifo(self):
        now = datetime.now(timezone.utc)
        alerts = [alert(severity="advisory"), alert(severity="critical"), alert(severity="critical"),
                  alert(effective=(now + timedelta(hours=1)).isoformat()),
                  alert(expires=(now - timedelta(hours=1)).isoformat())]
        items = [presentation.PendingAlert(str(i), value, i, presentation.presentation_policy(value))
                 for i, value in enumerate(alerts)]
        self.assertEqual([item.record_id for item in presentation.ordered_ready(items, now)], ["1", "2", "0"])

    def test_coalescing_requires_scoped_incident_and_explicit_order(self):
        old = alert(incident_id="pbx-a:fire", revision=2)
        self.assertTrue(presentation.can_supersede(alert(incident_id="pbx-a:fire", revision=3), old))
        for candidate in (alert(incident_id="pbx-b:fire", revision=3),
                          alert(incident_id="pbx-a:fire", revision=None),
                          alert(incident_id="pbx-a:fire", revision=1),
                          alert(incident_id="pbx-a:fire", revision=True)):
            self.assertFalse(presentation.can_supersede(candidate, old))

    def test_audio_preemption_only_higher_priority_or_after_completion(self):
        self.assertFalse(presentation.should_play_audio(3, 0, 5, 30))
        self.assertFalse(presentation.should_play_audio(0, 0, 5, 30))
        self.assertTrue(presentation.should_play_audio(0, 3, 5, 30))
        self.assertTrue(presentation.should_play_audio(3, 0, 31, 30))


class PresenterTests(unittest.TestCase):
    def setUp(self):
        self.view_patch = patch.object(presentation, "_AlertView", FakeView)
        self.view_patch.start()
        self.displayed = Mock()
        self.acknowledged = Mock()
        self.failed = Mock()
        self.play_sound = Mock(return_value=10)
        self.presenter = presentation.AlertPresenter(FakeRoot(), self.displayed, self.acknowledged,
                                                      self.failed, play_sound=self.play_sound, max_pending=3)

    def tearDown(self):
        self.presenter.shutdown()
        self.view_patch.stop()

    def test_critical_is_displayed_and_second_critical_waits_until_ack(self):
        self.assertTrue(self.presenter.submit("first", alert()))
        self.assertTrue(self.presenter.submit("second", alert()))
        self.assertEqual(list(self.presenter.visible), ["first"])
        self.assertTrue(self.presenter.has_active_critical)
        self.assertEqual(self.presenter.pending_count, 1)
        self.presenter._respond("first", "acknowledged")
        self.assertEqual(list(self.presenter.visible), ["second"])
        self.assertEqual(self.displayed.call_count, 2)

    def test_capacity_rejection_leaves_record_for_durable_retry(self):
        for key in ("first", "second", "third"):
            self.assertTrue(self.presenter.submit(key, alert()))
        self.assertFalse(self.presenter.submit("fourth", alert()))
        self.failed.assert_not_called()
        self.assertEqual(len(self.presenter.visible) + self.presenter.pending_count, 3)

    def test_full_routine_queue_admits_critical_and_releases_pending_item_for_retry(self):
        self.presenter.max_pending = 128
        scheduled = set()
        self.presenter.on_discarded = scheduled.discard
        routine = alert(kind="announcement", severity="notice", display_timeout_seconds=0)
        for index in range(128):
            key = f"routine-{index}"
            scheduled.add(key)
            self.assertTrue(self.presenter.submit(key, routine))
        self.assertEqual(len(self.presenter.visible), 2)
        self.assertEqual(self.presenter.pending_count, 126)
        self.assertTrue(self.presenter.submit("critical", alert(severity="critical")))
        self.assertEqual(list(self.presenter.visible), ["critical"])
        self.assertEqual(len(self.presenter.visible) + self.presenter.pending_count, 128)
        self.assertNotIn("routine-127", scheduled)
        self.assertIn("routine-0", scheduled)
        self.assertIn("routine-1", scheduled)
        self.assertFalse(any(item.record_id == "routine-127" for item in self.presenter.history))
        self.acknowledged.assert_not_called()
        self.presenter._respond("critical", "acknowledged")
        self.assertTrue(self.presenter.submit("routine-127", routine))
        self.assertIn("routine-127", self.presenter.pending)
        self.acknowledged.assert_called_once_with("critical", "acknowledged")

    def test_full_urgent_queue_admits_more_severe_critical(self):
        self.presenter.on_discarded = Mock()
        for key in ("urgent-one", "urgent-two", "urgent-three"):
            self.presenter.submit(key, alert(severity="urgent"))
        self.assertTrue(self.presenter.submit("critical", alert(severity="critical")))
        self.assertEqual(list(self.presenter.visible), ["critical"])
        self.assertEqual(self.presenter.pending_count, 2)
        self.presenter.on_discarded.assert_called_once_with("urgent-three")
        self.acknowledged.assert_not_called()

    def test_priority_admission_never_evicts_equal_or_more_important_alert(self):
        self.presenter.on_discarded = Mock()
        for key in ("one", "two", "three"):
            self.presenter.submit(key, alert(severity="critical"))
        self.assertFalse(self.presenter.submit("equal", alert(severity="critical")))
        self.assertFalse(self.presenter.submit("less", alert(severity="urgent")))
        self.presenter.on_discarded.assert_not_called()

    def test_capacity_one_can_suspend_visible_routine_without_acknowledging_it(self):
        self.presenter.max_pending = 1
        self.presenter.on_discarded = Mock()
        self.presenter.submit("routine", alert(severity="notice"))
        routine_view = self.presenter.visible["routine"]
        self.assertTrue(self.presenter.submit("critical", alert(severity="critical")))
        self.assertTrue(routine_view.closed)
        self.assertEqual(list(self.presenter.visible), ["critical"])
        self.presenter.on_discarded.assert_called_once_with("routine")
        self.acknowledged.assert_not_called()

    def test_expired_critical_does_not_displace_valid_lower_priority_notification(self):
        self.presenter.max_pending = 1
        self.presenter.on_discarded = Mock()
        self.presenter.submit("routine", alert(severity="notice"))
        self.assertTrue(self.presenter.submit("expired", alert(expires="2000-01-01T00:00:00Z")))
        self.assertEqual(list(self.presenter.visible), ["routine"])
        self.presenter.on_discarded.assert_not_called()
        self.acknowledged.assert_called_once_with("expired", "expired")

    def test_priority_eviction_requires_durable_retry_callback(self):
        self.presenter.max_pending = 1
        self.presenter.submit("routine", alert(severity="notice"))
        self.assertFalse(self.presenter.submit("critical", alert(severity="critical")))
        self.assertEqual(list(self.presenter.visible), ["routine"])
        self.acknowledged.assert_not_called()

    def test_routine_views_are_bounded_and_critical_preempts_without_acknowledging(self):
        self.presenter.submit("one", alert(severity="advisory"))
        self.presenter.submit("two", alert(severity="advisory"))
        self.assertEqual(len(self.presenter.visible), 2)
        self.presenter.submit("urgent", alert())
        self.assertEqual(list(self.presenter.visible), ["urgent"])
        self.assertEqual(self.presenter.pending_count, 2)
        self.acknowledged.assert_not_called()
        self.presenter._respond("urgent", "acknowledged")
        self.assertEqual(set(self.presenter.visible), {"one", "two"})

    def test_new_revision_supersedes_active_incident(self):
        self.presenter.submit("old", alert(incident_id="pbx:incident", revision=1))
        self.presenter.submit("new", alert(incident_id="pbx:incident", revision=2))
        self.acknowledged.assert_called_once_with("old", "superseded")
        self.assertEqual(list(self.presenter.visible), ["new"])

    def test_older_revision_is_never_displayed_after_newer_revision(self):
        self.presenter.submit("new", alert(incident_id="pbx:incident", revision=2))
        self.presenter.submit("old", alert(incident_id="pbx:incident", revision=1))
        self.acknowledged.assert_called_once_with("old", "superseded")
        self.displayed.assert_called_once_with("new")

    def test_older_revision_is_suppressed_after_newer_revision_was_acknowledged(self):
        self.presenter.submit("new", alert(incident_id="pbx:incident", revision=2))
        self.presenter._respond("new", "acknowledged")
        self.presenter.submit("old", alert(incident_id="pbx:incident", revision=1))
        self.acknowledged.assert_called_with("old", "superseded")
        self.displayed.assert_called_once_with("new")

    def test_failed_supersession_does_not_remove_current_alert_or_overfill_queue(self):
        self.presenter.submit("old", alert(incident_id="pbx:incident", revision=1))
        self.acknowledged.side_effect = OSError("disk full")
        with self.assertLogs(presentation.LOG, level="ERROR"):
            accepted = self.presenter.submit("new", alert(incident_id="pbx:incident", revision=2))
        self.assertFalse(accepted)
        self.assertEqual(list(self.presenter.visible), ["old"])

    def test_history_does_not_retain_raw_network_payload(self):
        self.presenter.submit("first", alert(raw={"large_unused_payload": "x" * 100000}))
        self.presenter._respond("first", "acknowledged")
        cached = self.presenter.history[0].alert
        self.assertEqual(cached["body"], "Use the east stairwell.")
        self.assertNotIn("raw", cached)

    def test_cancellation_only_closes_the_scoped_incident(self):
        self.presenter.submit("a", alert(incident_id="pbx-a:fire", revision=1))
        self.presenter.submit("b", alert(incident_id="pbx-b:fire", revision=1))
        self.presenter.cancel("pbx-a:fire")
        self.acknowledged.assert_called_once_with("a", "cancelled")
        self.assertEqual(list(self.presenter.visible), ["b"])

    def test_expired_notification_is_recorded_without_display_or_audio(self):
        self.presenter.submit("expired", alert(expires="2000-01-01T00:00:00Z"))
        self.displayed.assert_not_called()
        self.play_sound.assert_not_called()
        self.acknowledged.assert_called_once_with("expired", "expired")

    def test_future_notification_waits_without_being_marked_displayed(self):
        self.presenter.submit("future", alert(effective="2099-01-01T00:00:00Z"))
        self.displayed.assert_not_called()
        self.assertEqual(self.presenter.pending_count, 1)

    def test_failed_persistence_keeps_alert_visible(self):
        self.presenter.submit("first", alert())
        self.acknowledged.side_effect = OSError("disk full")
        with self.assertLogs(presentation.LOG, level="ERROR"):
            self.presenter._respond("first", "acknowledged")
        self.assertIn("first", self.presenter.visible)
        self.assertEqual(len(self.presenter.history), 0)
        self.failed.assert_called_once()

    def test_failed_render_is_retryable_without_duplicate_suppression(self):
        with patch.object(presentation, "_AlertView", side_effect=RuntimeError("display failed")):
            with self.assertLogs(presentation.LOG, level="ERROR"):
                self.presenter.submit("first", alert())
        self.displayed.assert_not_called()
        self.failed.assert_called_once_with("first", "display failed")
        self.presenter.submit("first", alert())
        self.displayed.assert_called_once_with("first")

    def test_duplicate_pending_displayed_or_completed_is_not_redisplayed(self):
        self.presenter.submit("first", alert())
        self.presenter.submit("first", alert())
        self.presenter._respond("first", "acknowledged")
        self.presenter.submit("first", alert())
        self.displayed.assert_called_once_with("first")

    def test_shutdown_leaves_delivery_state_for_replay(self):
        self.presenter.submit("first", alert())
        self.presenter.submit("second", alert())
        self.presenter.shutdown()
        self.acknowledged.assert_not_called()
        self.assertFalse(self.presenter.submit("third", alert()))

    def test_guard_blocks_retired_record_without_acknowledgment_or_display(self):
        self.presenter.can_display = Mock(return_value=False)
        self.presenter.on_discarded = Mock()
        self.assertTrue(self.presenter.submit("retired", alert()))
        self.displayed.assert_not_called()
        self.acknowledged.assert_not_called()
        self.presenter.on_discarded.assert_called_once_with("retired")
        self.assertEqual(self.presenter.pending_count, 0)

    def test_queued_record_retired_by_store_never_appears_when_critical_closes(self):
        eligible = {"current", "queued"}
        self.presenter.can_display = lambda record_id: record_id in eligible
        self.presenter.on_discarded = Mock()
        self.presenter.submit("current", alert())
        self.presenter.submit("queued", alert())
        eligible.remove("queued")
        self.presenter._respond("current", "acknowledged")
        self.displayed.assert_called_once_with("current")
        self.acknowledged.assert_called_once_with("current", "acknowledged")
        self.presenter.on_discarded.assert_called_once_with("queued")
        self.assertFalse(self.presenter.visible)

    def test_guard_discards_visible_cancelled_record_without_overwriting_store_state(self):
        self.presenter.submit("current", alert())
        self.presenter.can_display = Mock(return_value=False)
        self.presenter.on_discarded = Mock()
        self.presenter._tick()
        self.presenter.on_discarded.assert_called_once_with("current")
        self.acknowledged.assert_not_called()
        self.assertFalse(self.presenter.visible)

    def test_guard_prevents_human_callback_from_overwriting_already_cancelled_state(self):
        self.presenter.submit("current", alert())
        self.presenter.can_display = Mock(return_value=False)
        self.presenter._respond("current", "acknowledged")
        self.acknowledged.assert_not_called()
        self.assertFalse(self.presenter.visible)

    def test_severe_weather_window_ends_at_absolute_expiry(self):
        now = datetime.now(timezone.utc)
        self.presenter.submit("weather", alert(severity="Severe", expires=(now + timedelta(seconds=2)).isoformat()))
        self.assertIn("weather", self.presenter.visible)
        with patch.object(presentation, "datetime") as clock:
            clock.now.return_value = now + timedelta(seconds=3)
            self.presenter._tick()
        self.acknowledged.assert_called_once_with("weather", "expired")
        self.assertFalse(self.presenter.visible)

    def test_zero_timeout_announcement_does_not_auto_dismiss_on_tick(self):
        self.presenter.submit("announcement", alert(kind="announcement", severity="notice", display_timeout_seconds=0))
        self.presenter._tick()
        self.acknowledged.assert_not_called()
        self.assertIn("announcement", self.presenter.visible)

    def test_displayed_receipt_does_not_invoke_local_human_response(self):
        self.presenter.submit("first", alert())
        self.displayed.assert_called_once_with("first")
        self.acknowledged.assert_not_called()
        self.presenter._respond("first", "acknowledged")
        self.acknowledged.assert_called_once_with("first", "acknowledged")


if __name__ == "__main__":
    unittest.main()
