"""Consumer checks against synthetic records emitted by the actual PBX producers."""
import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import unittest

import sls_mass_notify as app
from sls_presentation import explicit_test_mode, presentation_policy
from sls_protocol import ordered_reconciliation, validate_payload


FIXTURE = json.loads((Path(__file__).parent / "tests/fixtures/desktop_contract.json").read_text(encoding="utf-8"))


def record(group, name):
    return copy.deepcopy(FIXTURE[group][name])


def parse(payload):
    validated = validate_payload(payload)
    return app.extract_alert(validated, json.dumps(validated))


class BackendContractTests(unittest.TestCase):
    def test_every_published_and_additive_producer_record_is_accepted(self):
        for group in ("published", "additive"):
            for name in FIXTURE[group]:
                with self.subTest(group=group, name=name):
                    payload = record(group, name)
                    alert = parse(payload)
                    self.assertEqual(alert.event_id, payload["id"])
                    self.assertEqual(alert.kind, payload["kind"])

    def test_weather_url_and_urn_ids_are_not_rewritten(self):
        for group, name in (("published", "weather"), ("additive", "weather_update_descriptive")):
            payload = record(group, name)
            self.assertEqual(parse(payload).event_id, payload["id"])
        self.assertIn("%2F", record("published", "weather")["id"])

    def test_weather_expiry_remains_authoritative_for_extreme_severity(self):
        alert = parse(record("published", "weather"))
        policy = presentation_policy(alert)
        self.assertEqual(policy.priority, 0)
        self.assertEqual(policy.expires, datetime(2026, 9, 20, 19, 0, tzinfo=timezone.utc))
        self.assertIsNone(policy.timeout_seconds)

    def test_upstream_nullable_and_missing_weather_fields_are_compatible(self):
        for name in ("weather_nullable", "weather_missing_timestamps"):
            with self.subTest(name=name):
                alert = parse(record("published", name))
                self.assertEqual(alert.effective, "")
                self.assertEqual(alert.expires, "")
                self.assertIsNone(presentation_policy(alert).expires)
        self.assertEqual(parse(record("published", "weather_nullable")).description, "")

    def test_zero_announcement_timeout_is_persistent_even_with_stray_expiry(self):
        payload = record("published", "announcement_persistent")
        self.assertIsNone(payload["display_expires_at"])
        payload["display_expires_at"] = "2000-01-01T00:00:00Z"
        policy = presentation_policy(parse(payload))
        self.assertIsNone(policy.timeout_seconds)
        self.assertIsNone(policy.expires)

    def test_timed_announcement_uses_absolute_deadline_not_new_countdown(self):
        alert = parse(record("published", "announcement_timed"))
        policy = presentation_policy(alert)
        self.assertEqual(alert.display_timeout_seconds, 300)
        self.assertEqual(policy.expires, datetime(2026, 9, 20, 18, 5, tzinfo=timezone.utc))
        self.assertIsNone(policy.timeout_seconds)
        # Re-extraction after replay preserves the same original absolute time.
        self.assertEqual(presentation_policy(parse(record("published", "announcement_timed"))).expires, policy.expires)

    def test_positive_timeout_without_absolute_expiry_reports_missing_deadline(self):
        payload = record("published", "announcement_timed")
        payload.pop("display_expires_at")
        policy = presentation_policy(parse(payload))
        self.assertIsNone(policy.timeout_seconds)
        self.assertIsNone(policy.expires)
        self.assertTrue(policy.timing_warning)

    def test_timeout_limit_and_wrong_types_match_backend_contract(self):
        self.assertEqual(parse(record("additive", "announcement_max_timeout")).display_timeout_seconds, 86400)
        for timeout in (-1, 86401, True, "300", 30.5):
            with self.subTest(timeout=timeout):
                payload = record("published", "announcement_timed")
                payload["display_timeout_seconds"] = timeout
                with self.assertRaises(ValueError):
                    validate_payload(payload)

    def test_expiry_requires_absolute_timezone_and_sensible_weather_interval(self):
        for expires in ("not-a-time", "2026-09-20T18:05:00", 1234):
            with self.subTest(expires=expires):
                payload = record("published", "announcement_timed")
                payload["display_expires_at"] = expires
                with self.assertRaises(ValueError):
                    validate_payload(payload)
        payload = record("published", "weather")
        payload["expires"] = payload["effective"]
        with self.assertRaises(ValueError):
            validate_payload(payload)

    def test_only_explicit_producer_metadata_marks_tests(self):
        self.assertIsNone(explicit_test_mode(parse(record("published", "weather"))))
        self.assertIs(explicit_test_mode(parse(record("additive", "weather_live"))), False)
        self.assertIs(explicit_test_mode(parse(record("additive", "weather_test"))), True)
        self.assertIs(explicit_test_mode(parse(record("additive", "announcement_test"))), True)
        malformed = record("additive", "announcement_test")
        malformed["is_test"] = "false"
        with self.assertRaises(ValueError):
            validate_payload(malformed)

    def test_descriptive_weather_updates_never_become_unversioned_closures(self):
        live = parse(record("additive", "weather_live"))
        for name in ("weather_update_descriptive", "weather_cancel_descriptive"):
            payload = record("additive", name)
            alert = parse(payload)
            self.assertEqual(alert.action, "notify")
            self.assertEqual(alert.incident_id, live.incident_id)
            self.assertEqual(alert.revision, 0)
            payload["action"] = "cancel"
            with self.assertRaises(ValueError):
                validate_payload(payload)

    def test_recent_endpoint_order_and_matching_latest_do_not_hide_earlier_records(self):
        response = copy.deepcopy(FIXTURE["fallback"])
        events = ordered_reconciliation(response, response["latest"]["id"])
        self.assertEqual([item["id"] for item in events], [item["id"] for item in response["events"]])
        self.assertEqual(len(events), 2)
        self.assertEqual(app.extract_alert(response, json.dumps(response)).event_id, response["latest"]["id"])

    def test_sse_baseline_and_controls_remain_distinct_from_notification_identity(self):
        handshake = FIXTURE["authenticated"]
        payload = record("published", "weather")
        transcript = (
            "retry: 1000\nid: " + handshake["id"] + "\nevent: authenticated\ndata: " + json.dumps(handshake["data"]) + "\n\n"
            ": keepalive 1789927200\n\n"
            "id: " + payload["id"] + "\nevent: notification\ndata: " + json.dumps(payload) + "\n\n"
            'event: revoked\ndata: {"ok":false,"error":"credentials_revoked"}\n\n'
            'event: reconnect\ndata: {"ok":true,"session_id":"fixture-session"}\n\n'
        )
        events = list(app.iter_sse_events(io.BytesIO(transcript.encode("utf-8"))))
        self.assertEqual([value.name for value in events], ["authenticated", "notification", "revoked", "reconnect"])
        self.assertEqual(events[0].event_id, "@sls:empty")
        self.assertEqual(events[1].event_id, payload["id"])


if __name__ == "__main__":
    unittest.main()
