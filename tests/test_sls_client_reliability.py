"""Client integration regressions with isolated SQLite/settings and fake transport.

No test creates GUI windows, accesses Windows credentials/registry, plays sound,
or contacts a PBX. Production configuration and logs are never written.
"""
import base64
from contextlib import closing
import copy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import sls_mass_notify as client
from sls_presentation import explicit_test_mode, test_marker
from sls_protocol import profile_namespace
from sls_store import Inbox, InboxFullError


def notification(event_id="event-1", **fields):
    data = dict(id=event_id, kind="alert", title="Evacuate", message="Use the east stairwell.",
                severity="Severe", desktop_all=True)
    data.update(fields)
    return data


def sse(name, data, event_id=None):
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return (prefix + f"event: {name}\ndata: {json.dumps(data)}\n\n").encode()


def authenticated(event_id=None):
    return sse("authenticated", dict(ok=True, transport="live_sse", protocol_version=2,
                                      client_id="desktop-1", heartbeat_seconds=15), event_id)


class IsolatedClientTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.enterContext(mock.patch.object(client, "CONFIG_DIR", self.path))
        self.enterContext(mock.patch.object(client, "CONFIG_PATH", self.path / "settings.json"))
        self.enterContext(mock.patch.object(client, "log"))
        self.enterContext(mock.patch.object(client, "set_startup_enabled"))
        self.enterContext(mock.patch.object(client, "_write_windows_credential", side_effect=AssertionError("Unexpected real credential write")))
        self.enterContext(mock.patch.object(client, "_delete_windows_credential", side_effect=AssertionError("Unexpected real credential deletion")))
        self.profile = client.normalize_endpoint(dict(endpoint="https://pbx.example.test", username="desktop-1",
                                                       password="old password", enabled=True), 0)
        self.instance = client.MassNotifyApp.__new__(client.MassNotifyApp)
        instance = self.instance
        instance.config = client.normalize_config({"enabled": True, "endpoints": [self.profile]})
        instance.config_lock = threading.RLock()
        instance.stop_event = threading.Event()
        instance.wakeup_event = threading.Event()
        instance.worker_lock = threading.RLock()
        instance.workers = {}
        instance.faults_lock = threading.RLock()
        instance.faults = {}
        instance.transport_statuses = {}
        instance.status_text = "Waiting for PBX connection."
        instance.ui_queue = client.ControlQueue()
        instance.inbox = Inbox(self.path / "test.sqlite3")
        self.addCleanup(instance.inbox.close)
        instance._scheduled_records = set()
        instance.presenter = mock.Mock()
        instance.presenter.submit.return_value = True
        self.namespace = profile_namespace(self.profile)

    def alert(self, event_id="event-1", stream_id="stream-1", **fields):
        data = notification(event_id, **fields)
        result = client.extract_alert(data, json.dumps(data))
        result.stream_id = stream_id
        return result

    def worker(self):
        worker = client.EndpointTransportWorker(self.instance, 0, self.instance.config["endpoints"][0])
        self.instance.workers[0] = worker
        return worker


class DurableDeliveryIntegrationTests(IsolatedClientTest):
    def test_sqlite_commits_receipt_before_profile_cursor_changes(self):
        self.instance.inbox.advance_cursor(self.namespace, "stream-old")
        profile = self.instance.config["endpoints"][0]
        profile.update(last_stream_id="stream-old", last_event_id="event-old")
        real_receive = self.instance.inbox.receive

        def observe_commit(*args, **kwargs):
            self.assertEqual(profile["last_stream_id"], "stream-old")
            result = real_receive(*args, **kwargs)
            self.assertEqual(profile["last_stream_id"], "stream-old")
            with closing(sqlite3.connect(self.path / "test.sqlite3")) as observer:
                saved = observer.execute("SELECT stream_id FROM cursors WHERE namespace=?", (self.namespace,)).fetchone()[0]
                pending = observer.execute("SELECT state FROM notifications WHERE event_id='event-new'").fetchone()[0]
            self.assertEqual(saved, "stream-new")
            self.assertEqual(pending, "pending")
            return result

        with mock.patch.object(self.instance.inbox, "receive", side_effect=observe_commit):
            self.assertTrue(self.instance.accept_alert(0, self.alert("event-new", "stream-new")))
        self.assertEqual(profile["last_stream_id"], "stream-new")
        self.assertEqual(profile["last_event_id"], "event-new")

    def test_database_failure_does_not_advance_worker_or_config_cursor(self):
        worker = self.worker()
        before_worker = dict(worker.endpoint_cfg)
        before_config = copy.deepcopy(self.instance.config)
        with mock.patch.object(self.instance.inbox, "receive", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                worker._accept_payload(notification(), json.dumps(notification()), "event-1")
        self.assertEqual(worker.endpoint_cfg, before_worker)
        self.assertEqual(self.instance.config, before_config)
        self.assertEqual(self.instance.inbox.cursor(self.namespace), "")
        self.assertEqual(self.instance.inbox.pending_receipts(self.namespace), [])

    def test_capacity_failure_rolls_back_event_receipt_and_cursor(self):
        self.instance.inbox.max_pending = 1
        self.instance.accept_alert(0, self.alert("first", "stream-first"))
        with self.assertRaises(InboxFullError):
            self.instance.accept_alert(0, self.alert("second", "stream-second"))
        self.assertEqual(self.instance.inbox.cursor(self.namespace), "stream-first")
        self.assertEqual(self.instance.config["endpoints"][0]["last_event_id"], "first")
        self.assertEqual([row["event_id"] for row in self.instance.inbox.pending_receipts(self.namespace)], ["first"])

    def test_replayed_notification_has_one_durable_event_and_one_receipt(self):
        self.assertTrue(self.instance.accept_alert(0, self.alert()))
        self.assertFalse(self.instance.accept_alert(0, self.alert()))
        self.assertEqual(self.instance.inbox.statistics(), {"pending": 1, "pending_receipts": 1})
        self.assertEqual(len(self.instance.inbox.pending_receipts(self.namespace)), 1)

    def test_disabled_stopped_and_stale_worker_cannot_accept(self):
        cases = ("monitoring disabled", "profile disabled", "stopped", "stale credential")
        for case in cases:
            with self.subTest(case=case):
                self.instance.config["enabled"] = case != "monitoring disabled"
                self.instance.config["endpoints"][0]["enabled"] = case != "profile disabled"
                self.instance.stop_event.clear()
                if case == "stopped":
                    self.instance.stop_event.set()
                signature = "obsolete-signature" if case == "stale credential" else ""
                self.assertFalse(self.instance.accept_alert(0, self.alert(), signature))
        self.assertEqual(self.instance.inbox.statistics(), {"pending_receipts": 0})
        self.assertEqual(self.instance.inbox.cursor(self.namespace), "")

    def test_receipt_ack_is_independent_of_human_response(self):
        self.instance.accept_alert(0, self.alert())
        record = self.instance.inbox.pending()[0]
        receipt = self.instance.inbox.pending_receipts(self.namespace)[0]
        self.instance.inbox.receipt_sent(receipt["receipt_id"])
        self.assertEqual(self.instance.inbox.record(record["record_id"])["state"], "pending")
        self.instance.alert_displayed(record["record_id"])
        self.assertEqual(self.instance.inbox.record(record["record_id"])["state"], "displayed")
        self.instance.alert_acknowledged(record["record_id"], "acknowledged")
        self.assertEqual(self.instance.inbox.record(record["record_id"])["state"], "acknowledged")

    def test_profile_switch_hides_previous_profile_inbox_without_destroying_history(self):
        self.instance.accept_alert(0, self.alert())
        record_id = self.instance.inbox.pending()[0]["record_id"]
        self.assertTrue(self.instance.can_display_record(record_id))
        self.instance.config["endpoints"][0]["endpoint"] = "https://other.example.test"
        self.assertFalse(self.instance.can_display_record(record_id))
        self.instance.pump_inbox()
        self.instance.presenter.submit.assert_not_called()
        self.assertEqual(len(self.instance.inbox.history()), 1)

    def test_announcement_absolute_expiry_and_weather_expiry_prevent_presentation(self):
        self.instance.accept_alert(0, self.alert("announcement", "stream-a", kind="announcement",
            display_timeout_seconds=60, display_expires_at="2000-01-01T00:00:00Z"))
        self.instance.accept_alert(0, self.alert("weather", "stream-b", expires="2000-01-01T00:00:00Z"))
        self.instance.pump_inbox()
        self.instance.presenter.submit.assert_not_called()
        self.assertEqual(self.instance.inbox.statistics(), {"expired": 2, "pending_receipts": 2})

    def test_announcement_zero_timeout_is_not_expired_by_stale_display_deadline(self):
        self.instance.accept_alert(0, self.alert(kind="announcement", severity="notice",
            display_timeout_seconds=0, display_expires_at="2000-01-01T00:00:00Z"))
        self.instance.pump_inbox()
        self.instance.presenter.submit.assert_called_once()
        self.assertEqual(self.instance.inbox.statistics(), {"pending": 1, "pending_receipts": 1})

    def test_explicit_test_metadata_survives_compact_durable_payload_roundtrip(self):
        for index, metadata in enumerate(({"test_only": True}, {"is_test": True},
                                          {"status": "exercise"}, {"severity": "Test"})):
            with self.subTest(metadata=metadata):
                event_id = f"test-{index}"
                original = self.alert(event_id, f"stream-{index}", **metadata)
                self.assertTrue(original.test_only)
                self.instance.accept_alert(0, original)
                record = next(row for row in self.instance.inbox.history() if row["event_id"] == event_id)
                replayed = client.AlertData(**record["payload"])
                self.assertTrue(replayed.test_only)
                self.assertTrue(explicit_test_mode(replayed))
                self.assertIn("TEST / EXERCISE", test_marker(replayed))
                self.assertEqual(replayed.raw, {"is_test": True})

    def test_explicit_live_metadata_survives_compact_durable_payload_roundtrip(self):
        self.instance.accept_alert(0, self.alert(test_only=False))
        replayed = client.AlertData(**self.instance.inbox.history()[0]["payload"])
        self.assertFalse(explicit_test_mode(replayed))
        self.assertEqual(test_marker(replayed), "LIVE ALERT")

    def test_legacy_test_title_remains_unclassified_after_durable_roundtrip(self):
        self.instance.accept_alert(0, self.alert(title="Lightning Test", message="TEST ONLY"))
        replayed = client.AlertData(**self.instance.inbox.history()[0]["payload"])
        self.assertFalse(replayed.test_only)
        self.assertIsNone(explicit_test_mode(replayed))
        self.assertIn("not specified", test_marker(replayed))

    def test_durable_payload_drops_duplicate_wire_and_xml_copies(self):
        original = self.alert(xml_payload="<YealinkIPPhoneTextScreen><Text>Use the east stairwell.</Text></YealinkIPPhoneTextScreen>")
        original.raw_text = "wire payload copied elsewhere"
        original.xml_payload = "<screen>duplicate XML representation</screen>"
        original.recent_events = "other recent notification contents"
        self.instance.accept_alert(0, original)
        saved = self.instance.inbox.history()[0]["payload"]
        self.assertEqual(saved["raw"], {})
        self.assertEqual(saved["raw_text"], "")
        self.assertEqual(saved["xml_payload"], "")
        self.assertEqual(saved["recent_events"], "")
        self.assertEqual(saved["body"], "Use the east stairwell.")
        self.assertEqual(saved["title"], "Evacuate")
        self.assertEqual(original.raw_text, "wire payload copied elsewhere")

    def test_human_acknowledgment_marks_local_state_without_server_response_outbox(self):
        self.instance.accept_alert(0, self.alert())
        record_id = self.instance.inbox.history()[0]["record_id"]
        self.instance._scheduled_records.add(record_id)
        with mock.patch.object(self.instance.inbox, "respond", side_effect=AssertionError("Human response API does not exist")):
            self.instance.alert_acknowledged(record_id, "acknowledged")
        self.assertEqual(self.instance.inbox.record(record_id)["state"], "acknowledged")
        self.assertNotIn(record_id, self.instance._scheduled_records)
        self.assertEqual(self.instance.inbox.db.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 0)
        self.assertEqual(len(self.instance.inbox.pending_receipts(self.namespace)), 1)


class ConfigurationIntegrationTests(IsolatedClientTest):
    def test_legacy_root_profile_is_migrated_and_plaintext_removed_during_load(self):
        original = dict(endpoint="https://pbx.example.test", username="legacy", password=" exact secret ")
        client.CONFIG_PATH.write_text(json.dumps(original), encoding="utf-8")
        with mock.patch.object(client, "protect_secret", return_value="dpapi:protected") as protect:
            result = client.load_config()
        protect.assert_called_once_with(" exact secret ")
        self.assertEqual(result["endpoints"][0]["username"], "legacy")
        self.assertEqual(result["endpoints"][0]["password"], "dpapi:protected")
        saved = client.CONFIG_PATH.read_text(encoding="utf-8")
        self.assertNotIn("exact secret", saved)
        self.assertEqual(json.loads(saved)["password"], "dpapi:protected")

    def test_failed_legacy_migration_preserves_original_file(self):
        original = json.dumps(dict(endpoint="https://pbx.example.test", username="legacy", password="secret"))
        client.CONFIG_PATH.write_text(original, encoding="utf-8")
        with mock.patch.object(client, "protect_secret", return_value="dpapi:protected"), \
             mock.patch.object(client, "save_config", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                client.load_config()
        self.assertEqual(client.CONFIG_PATH.read_text(encoding="utf-8"), original)

    def test_corrupt_settings_are_not_replaced_with_defaults(self):
        client.CONFIG_PATH.write_text('{"endpoints": broken', encoding="utf-8")
        with mock.patch.object(client, "save_config") as save:
            with self.assertRaisesRegex(ValueError, "Cannot load settings"):
                client.load_config()
        save.assert_not_called()
        self.assertEqual(client.CONFIG_PATH.read_text(encoding="utf-8"), '{"endpoints": broken')

    def test_future_settings_schema_is_rejected_without_overwrite(self):
        original = json.dumps({"schema_version": 99, "endpoints": []})
        client.CONFIG_PATH.write_text(original, encoding="utf-8")
        with mock.patch.object(client, "save_config") as save:
            with self.assertRaisesRegex(ValueError, "unsupported schema"):
                client.load_config()
        save.assert_not_called()
        self.assertEqual(client.CONFIG_PATH.read_text(encoding="utf-8"), original)

    def test_nonboolean_settings_are_rejected_without_truthiness_conversion(self):
        corrupt = ({"enabled": "false"}, {"startup_enabled": 0}, {"auto_update_enabled": "true"},
                   {"endpoints": [{"enabled": "false"}]},
                   {"endpoints": [{"reconnect_automatically": 1}]})
        for data in corrupt:
            with self.subTest(data=data):
                original = json.dumps(data)
                client.CONFIG_PATH.write_text(original, encoding="utf-8")
                with mock.patch.object(client, "save_config") as save:
                    with self.assertRaisesRegex(ValueError, "must be true or false"):
                        client.load_config()
                save.assert_not_called()
                self.assertEqual(client.CONFIG_PATH.read_text(encoding="utf-8"), original)

    def test_exact_password_survives_normalization_form_collection_and_basic_auth(self):
        secret = "  padded password\t "
        profile = client.normalize_endpoint(dict(self.profile, password=secret), 0)
        self.assertEqual(profile["password"], secret)
        decoded = base64.b64decode(client.endpoint_auth_headers(profile)["Authorization"][6:]).decode()
        self.assertEqual(decoded, f"desktop-1:{secret}")
        window = client.SettingsWindow.__new__(client.SettingsWindow)
        window.app = self.instance
        window.enabled_var = mock.Mock(get=mock.Mock(return_value=True))
        window.endpoint_forms = [{name: mock.Mock(get=mock.Mock(return_value=value)) for name, value in
                                 dict(name="Main PBX", endpoint=profile["endpoint"], username=profile["username"],
                                      password=secret, enabled=True, reconnect_automatically=True).items()}]
        self.assertEqual(window.collect_settings()[0]["password"], secret)

    def change_password(self, *, fail_save=False, change_profile=False):
        old_target = client.profile_credential_target(0, "password") + "/old"
        old_secret, new_secret = "old secret", "new secret"
        secrets = {old_target: old_secret}
        self.instance.config["endpoints"][0].update(password="cred:" + old_target, credential_revision=4,
            last_stream_id="old-stream", last_event_id="old-event", recent_event_ids=["old-event"])
        before = copy.deepcopy(self.instance.config)
        candidate = dict(self.instance.config["endpoints"][0], password=new_secret)
        if change_profile:
            candidate.update(endpoint="https://other.example.test", username="new-user")
        deleted = []

        def remove(target):
            deleted.append(target)
            secrets.pop(target, None)

        with mock.patch.object(client, "is_windows", return_value=True), \
             mock.patch.object(client, "_read_windows_credential", side_effect=lambda target: secrets[target]), \
             mock.patch.object(client, "_write_windows_credential", side_effect=lambda target, value: secrets.__setitem__(target, value)), \
             mock.patch.object(client, "_delete_windows_credential", side_effect=remove), \
             mock.patch.object(client, "save_config", side_effect=OSError("disk full") if fail_save else None):
            operation = lambda: self.instance.update_settings(endpoints=[candidate], enabled=True,
                startup_enabled=False, auto_update_enabled=False, audio_sound=client.DEFAULT_AUDIO_NAME)
            if fail_save:
                with self.assertRaises(OSError):
                    operation()
            else:
                operation()
        return before, secrets, deleted, old_target, new_secret

    def test_same_length_password_change_restarts_worker_via_revision(self):
        before, secrets, deleted, old_target, new_secret = self.change_password()
        current = self.instance.config["endpoints"][0]
        self.assertEqual(current["credential_revision"], 5)
        self.assertNotEqual(client.endpoint_worker_signature(current), client.endpoint_worker_signature(before["endpoints"][0]))
        self.assertEqual(secrets[current["password"][5:]], new_secret)
        self.assertIn(old_target, deleted)
        self.assertTrue(self.instance.wakeup_event.is_set())

    def test_failed_settings_save_rolls_back_staged_credentials(self):
        before, secrets, deleted, old_target, _ = self.change_password(fail_save=True)
        self.assertEqual(self.instance.config, before)
        self.assertEqual(secrets, {old_target: "old secret"})
        self.assertEqual(len(deleted), 1)
        self.assertNotIn(old_target, deleted)
        self.assertFalse(self.instance.wakeup_event.is_set())

    def test_changing_server_and_username_drops_legacy_cursor_history(self):
        self.change_password(change_profile=True)
        current = self.instance.config["endpoints"][0]
        self.assertEqual(current["last_event_id"], "")
        self.assertEqual(current["last_stream_id"], "")
        self.assertEqual(current["recent_event_ids"], [])
        self.assertNotEqual(profile_namespace(current), self.namespace)


class StreamAndReceiptIntegrationTests(IsolatedClientTest):
    def test_authentication_updates_baseline_to_current_tail(self):
        worker = self.worker()
        self.assertEqual(worker._consume_stream(io.BytesIO(authenticated("tail-1"))), (True, False))
        self.assertEqual(self.instance.inbox.cursor(self.namespace), "tail-1")
        worker._consume_stream(io.BytesIO(authenticated("tail-2")))
        self.assertEqual(self.instance.inbox.cursor(self.namespace), "tail-2")
        self.assertEqual(worker.endpoint_cfg["last_stream_id"], "tail-2")

    def test_revoked_stops_stream_before_following_notification(self):
        wire = authenticated() + sse("revoked", {"reason": "credentials changed"}) + sse("notification", notification(), "stream-1")
        with self.assertRaises(client.UnauthorizedError):
            self.worker()._consume_stream(io.BytesIO(wire))
        self.assertEqual(self.instance.inbox.statistics(), {"pending_receipts": 0})

    def test_unexpected_cursor_reset_does_not_replay_retained_events(self):
        wire = authenticated() + sse("cursor_reset", {"reason": "cursor expired"}) + sse("notification", notification(), "event-1")
        self.assertEqual(self.worker()._consume_stream(io.BytesIO(wire)), (True, True))
        self.assertEqual(self.instance.inbox.statistics(), {"pending_receipts": 0})
        self.assertEqual(self.instance.inbox.cursor(self.namespace), "")

    def test_inherited_sse_id_does_not_replace_notification_identity(self):
        wire = authenticated() + sse("notification", notification("first"), "first") + sse("notification", notification("second"))
        self.worker()._consume_stream(io.BytesIO(wire))
        self.assertEqual({row["event_id"] for row in self.instance.inbox.history()}, {"first", "second"})
        self.assertEqual(self.instance.inbox.cursor(self.namespace), "first")

    def test_server_reconnect_preserves_last_committed_cursor(self):
        wire = authenticated() + sse("notification", notification(), "event-1") + sse("reconnect", {})
        worker = self.worker()
        self.assertEqual(worker._consume_stream(io.BytesIO(wire)), (True, True))
        self.assertEqual(worker.endpoint_cfg["last_stream_id"], "event-1")

    def test_mismatched_explicit_sse_id_is_quarantined_without_receipt_ack(self):
        worker = self.worker()
        self.assertFalse(worker._accept_payload(notification("payload-id"), json.dumps(notification("payload-id")), "different-sse-id"))
        self.assertEqual(self.instance.inbox.history(), [])
        rejected = self.instance.inbox.quarantined()
        self.assertEqual(len(rejected), 1)
        self.assertIn("does not match", rejected[0]["error"])
        self.assertEqual(self.instance.inbox.cursor(self.namespace), "different-sse-id")
        self.assertEqual(self.instance.inbox.pending_receipts(self.namespace), [])
        self.assertEqual(self.instance.inbox.db.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 0)
        self.assertIn("protocol-0", self.instance.faults)

    def test_invalid_notification_does_not_block_next_valid_stream_notification(self):
        invalid = notification("bad", desktop_all="true")
        wire = authenticated() + sse("notification", invalid, "bad") + sse("notification", notification("good"), "good")
        worker = self.worker()
        self.assertEqual(worker._consume_stream(io.BytesIO(wire)), (True, False))
        self.assertEqual([row["event_id"] for row in self.instance.inbox.history()], ["good"])
        self.assertEqual([row["event_id"] for row in self.instance.inbox.quarantined()], ["bad"])
        self.assertEqual([row["event_id"] for row in self.instance.inbox.pending_receipts(self.namespace)], ["good"])
        self.assertEqual(self.instance.inbox.cursor(self.namespace), "good")
        self.assertEqual(worker.endpoint_cfg["last_stream_id"], "good")

    def test_malformed_json_does_not_block_next_valid_stream_notification(self):
        malformed = b'id: bad-json\nevent: notification\ndata: {"title": broken\n\n'
        wire = authenticated() + malformed + sse("notification", notification("good"), "good")
        self.worker()._consume_stream(io.BytesIO(wire))
        self.assertEqual([row["event_id"] for row in self.instance.inbox.history()], ["good"])
        rejected = self.instance.inbox.quarantined()[0]
        self.assertEqual(rejected["event_id"], "bad-json")
        self.assertIn("invalid notification JSON", rejected["error"])
        self.assertEqual(self.instance.inbox.cursor(self.namespace), "good")
        self.assertEqual(self.instance.inbox.statistics(), {"pending": 1, "pending_receipts": 1, "quarantined": 1})

    def test_quarantine_database_failure_does_not_advance_cursor_or_continue_stream(self):
        worker = self.worker()
        self.instance.inbox.advance_cursor(self.namespace, "previous")
        worker.endpoint_cfg["last_stream_id"] = "previous"
        invalid = notification("bad", desktop_all="true")
        wire = authenticated() + sse("notification", invalid, "bad") + sse("notification", notification("later"), "later")
        with mock.patch.object(self.instance.inbox, "quarantine", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                worker._consume_stream(io.BytesIO(wire))
        self.assertEqual(self.instance.inbox.cursor(self.namespace), "previous")
        self.assertEqual(worker.endpoint_cfg["last_stream_id"], "previous")
        self.assertEqual(self.instance.inbox.history(), [])
        self.assertEqual(self.instance.inbox.pending_receipts(self.namespace), [])
        self.assertEqual(self.instance.inbox.quarantined(), [])

    def test_corrected_payload_with_same_event_id_can_be_accepted_after_quarantine(self):
        worker = self.worker()
        invalid = notification("repaired", desktop_all="true")
        self.assertFalse(worker._accept_payload(invalid, json.dumps(invalid), "repaired"))
        self.assertEqual(self.instance.inbox.pending_receipts(self.namespace), [])
        corrected = notification("repaired")
        self.assertTrue(worker._accept_payload(corrected, json.dumps(corrected), "repaired"))
        self.assertEqual([row["event_id"] for row in self.instance.inbox.history()], ["repaired"])
        self.assertEqual(len(self.instance.inbox.pending_receipts(self.namespace)), 1)
        self.assertEqual(len(self.instance.inbox.quarantined()), 1)

    def test_retired_worker_cannot_deliver_even_when_credentials_signature_matches(self):
        retired = self.worker()
        current = self.worker()
        self.assertEqual(retired.signature, current.signature)
        self.assertFalse(retired._accept_payload(notification(), json.dumps(notification()), "event-1"))
        self.assertEqual(self.instance.inbox.history(), [])
        self.assertEqual(self.instance.inbox.cursor(self.namespace), "")
        retired._status("AUTHENTICATED", "Obsolete connection")
        self.assertNotIn(0, self.instance.transport_statuses)
        self.assertTrue(current._accept_payload(notification(), json.dumps(notification()), "event-1"))

    def test_comment_traffic_cannot_extend_authentication_deadline(self):
        worker = self.worker()
        with mock.patch.object(client.time, "monotonic", side_effect=[100.0, 101.0, 1000.0]):
            with self.assertRaisesRegex(client.StreamProtocolError, "deadline"):
                worker._consume_stream(io.BytesIO(b": heartbeat\n: heartbeat\n"))
        self.assertFalse(worker.authenticated_event.is_set())

    def test_ack_posts_only_event_id_with_basic_auth_and_same_origin_policy(self):
        response = io.BytesIO(json.dumps({"ok": True, "event_id": "event-1"}).encode())
        with mock.patch.object(client, "open_http_request", return_value=response) as request:
            client.send_receipt_ack(self.profile, "event-1")
        req = request.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://pbx.example.test/api/sipnotify/desktop/ack")
        self.assertEqual(json.loads(req.data), {"event_id": "event-1"})
        self.assertTrue(req.get_header("Authorization").startswith("Basic "))
        self.assertEqual(request.call_args.kwargs, {"timeout": 10, "same_origin": True})

    def test_ack_requires_explicit_matching_server_confirmation(self):
        for response in ({"ok": False, "event_id": "event-1"}, {"ok": True, "event_id": "other"}, {"ok": True}):
            with self.subTest(response=response):
                with mock.patch.object(client, "open_http_request", return_value=io.BytesIO(json.dumps(response).encode())):
                    with self.assertRaises(client.StreamProtocolError):
                        client.send_receipt_ack(self.profile, "event-1")

    def test_authentication_baseline_commit_failure_does_not_advance_cursor(self):
        worker = self.worker()
        with mock.patch.object(self.instance.inbox, "advance_cursor", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                worker._consume_stream(io.BytesIO(authenticated("tail-1")))
        self.assertEqual(self.instance.inbox.cursor(self.namespace), "")
        self.assertEqual(worker.endpoint_cfg["last_stream_id"], "")
        self.assertFalse(worker.authenticated_event.is_set())

    def test_connection_test_does_not_reuse_worker_with_stale_heartbeat(self):
        worker = self.worker()
        self.instance.workers[0] = worker
        self.instance.transport_statuses[0] = ("AUTHENTICATED", "Connected")
        with mock.patch.object(worker.thread, "is_alive", return_value=True), \
             mock.patch.object(client.time, "monotonic", return_value=1000.0):
            worker.last_activity = worker.last_stream_signal = 999.0
            self.assertTrue(self.instance.transport_is_authenticated(0, worker.endpoint_cfg))
            worker.last_activity = 1000.0 - client.SSE_READ_TIMEOUT_SECONDS - 1
            self.assertFalse(self.instance.transport_is_authenticated(0, worker.endpoint_cfg))


if __name__ == "__main__":
    unittest.main()
