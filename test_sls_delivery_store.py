import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sls_protocol import is_test, ordered_reconciliation, timestamp, validate_id, validate_payload
from sls_store import Inbox, InboxFullError


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.inbox = Inbox(":memory:")

    def tearDown(self):
        self.inbox.close()

    def test_receipt_survives_restart_before_presentation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inbox.db"
            inbox = Inbox(path)
            record_id, created = inbox.receive("profile", "event", {"body": "evacuate"}, stream_id="journal")
            inbox.close()
            inbox = Inbox(path)
            try:
                self.assertTrue(created)
                self.assertEqual(record_id, inbox.pending()[0]["record_id"])
                self.assertEqual("journal", inbox.cursor("profile"))
                inbox.mark(record_id, "displayed")
            finally:
                inbox.close()
            inbox = Inbox(path)
            try:
                self.assertEqual("displayed", inbox.pending()[0]["state"])
            finally:
                inbox.close()

    def test_failed_commit_rolls_back_notification_cursor_and_ack(self):
        self.inbox.db.execute("CREATE TRIGGER fail_cursor BEFORE INSERT ON cursors BEGIN SELECT RAISE(ABORT,'disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.inbox.receive("profile", "event", {}, stream_id="cursor", receipt_event_id="event")
        self.assertEqual([], self.inbox.pending())
        self.assertEqual("", self.inbox.cursor("profile"))
        self.assertEqual([], self.inbox.pending_receipts("profile"))
        self.inbox.db.execute("DROP TRIGGER fail_cursor")
        self.assertTrue(self.inbox.receive("profile", "event", {})[1])

    def test_namespace_and_sse_cursor_are_independent_of_catchup_ids(self):
        one, _ = self.inbox.receive("one", "9", {}, stream_id="opaque-resume")
        two, _ = self.inbox.receive("two", "9", {})
        self.assertNotEqual(one, two)
        self.inbox.receive("one", "earlier", {})
        self.assertEqual("opaque-resume", self.inbox.cursor("one"))
        self.assertEqual(2, len(self.inbox.pending(namespaces={"one"})))
        self.assertFalse(self.inbox.advance_cursor("one", "stale-handshake"))
        self.assertTrue(self.inbox.advance_cursor("two", "@sls:empty"))

    def test_new_critical_precedes_more_than_page_of_routine(self):
        for index in range(130):
            self.inbox.receive("one", str(index), {"priority": "routine"})
        critical, _ = self.inbox.receive("one", "urgent", {"severity": "Extreme"})
        self.assertEqual(critical, self.inbox.pending(limit=1)[0]["record_id"])

    def test_critical_precedes_urgent_backlog_and_unknown_weather_precedes_notice(self):
        for index in range(130):
            self.inbox.receive("one", str(index), {"priority": "urgent"})
        critical, _ = self.inbox.receive("one", "critical", {"priority": "critical"})
        self.assertEqual(critical, self.inbox.pending(limit=1)[0]["record_id"])
        self.inbox.receive("two", "notice", {"kind": "announcement", "priority": "notice"})
        unknown, _ = self.inbox.receive("two", "weather", {"kind": "alert"})
        self.assertEqual(unknown, self.inbox.pending(limit=1, namespaces={"two"})[0]["record_id"])

    def test_future_and_expired_records_are_not_presented(self):
        with mock.patch("sls_store.time.time", return_value=1000):
            self.inbox.receive("one", "future", {"effective": iso(2000), "expires": iso(3000)})
            expired, _ = self.inbox.receive("one", "expired", {"expires": iso(999)})
            self.assertEqual([], self.inbox.pending())
            self.assertEqual("expired", self.inbox.record(expired)["state"])
        with mock.patch("sls_store.time.time", return_value=2001):
            self.assertEqual("future", self.inbox.pending()[0]["event_id"])
        with mock.patch("sls_store.time.time", return_value=3001):
            self.assertEqual([], self.inbox.pending())

    def test_zero_timeout_ignores_stale_announcement_deadline(self):
        with mock.patch("sls_store.time.time", return_value=1000):
            self.inbox.receive("one", "persist", {"kind": "announcement", "display_timeout_seconds": 0, "display_expires_at": iso(10)})
            expired, _ = self.inbox.receive("one", "stale", {"kind": "announcement", "display_timeout_seconds": 30, "display_expires_at": iso(10)})
            self.assertEqual("persist", self.inbox.pending()[0]["event_id"])
            self.assertEqual("expired", self.inbox.record(expired)["state"])

    def test_incident_revision_terminal_state_and_stale_callbacks(self):
        old, _ = self.inbox.receive("one", "old", {}, incident_id="incident", revision=1)
        clear, _ = self.inbox.receive("one", "clear", {"action": "all_clear"}, incident_id="incident", revision=3)
        late, _ = self.inbox.receive("one", "late", {}, incident_id="incident", revision=2)
        self.assertEqual("cancelled", self.inbox.record(old)["state"])
        self.assertEqual("superseded", self.inbox.record(late)["state"])
        self.inbox.failed(old, "late render callback")
        self.inbox.mark(old, "displayed")
        self.inbox.respond(old, "safe")
        self.assertEqual("cancelled", self.inbox.record(old)["state"])
        newer, _ = self.inbox.receive("one", "newer", {}, incident_id="incident", revision=4)
        self.inbox.cancel_incident("one", "incident", except_record=clear)
        self.assertEqual("pending", self.inbox.record(newer)["state"])

    def test_future_allclear_does_not_cancel_early(self):
        with mock.patch("sls_store.time.time", return_value=1000):
            old, _ = self.inbox.receive("one", "old", {}, incident_id="incident", revision=1)
            clear, _ = self.inbox.receive("one", "clear", {"action": "all_clear", "effective": iso(2000)}, incident_id="incident", revision=2)
            self.inbox.cancel_incident("one", "incident", except_record=clear)
            self.assertEqual("pending", self.inbox.record(old)["state"])
        with mock.patch("sls_store.time.time", return_value=2001):
            self.assertEqual([clear], [row["record_id"] for row in self.inbox.pending()])
            self.assertEqual("cancelled", self.inbox.record(old)["state"])

    def test_dedupe_and_incident_tombstones_survive_content_prune(self):
        with mock.patch("sls_store.time.time", return_value=1000):
            record, _ = self.inbox.receive("one", "done", {"action": "all_clear"}, incident_id="incident", revision=2)
            self.inbox.mark(record, "dismissed")
        with mock.patch("sls_store.time.time", return_value=1000 + 40 * 86400):
            self.assertEqual(1, self.inbox.prune())
            self.assertFalse(self.inbox.receive("one", "done", {}, incident_id="incident", revision=2)[1])
            stale, _ = self.inbox.receive("one", "old", {}, incident_id="incident", revision=1)
            self.assertEqual("superseded", self.inbox.record(stale)["state"])

    def test_full_queue_rolls_back_but_incident_replacement_fits(self):
        inbox = Inbox(":memory:", max_pending=1)
        try:
            inbox.receive("one", "one", {}, incident_id="incident", revision=1, stream_id="first")
            with self.assertRaises(InboxFullError):
                inbox.receive("one", "overflow", {}, stream_id="second", receipt_event_id="overflow")
            self.assertEqual("first", inbox.cursor("one"))
            self.assertEqual([], inbox.pending_receipts("one"))
            inbox.receive("one", "replacement", {}, incident_id="incident", revision=2)
            self.assertEqual("replacement", inbox.pending()[0]["event_id"])
        finally:
            inbox.close()

    def test_concurrent_connections_enforce_capacity(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inbox.db"
            stores = [Inbox(path, max_pending=1), Inbox(path, max_pending=1)]
            barrier, outcomes = threading.Barrier(2), []
            def receive(index):
                barrier.wait()
                try:
                    stores[index].receive("one", str(index), {})
                    outcomes.append("ok")
                except InboxFullError:
                    outcomes.append("full")
            threads = [threading.Thread(target=receive, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
                self.assertFalse(thread.is_alive())
            try:
                self.assertCountEqual(["ok", "full"], outcomes)
            finally:
                for inbox in stores:
                    inbox.close()

    def test_receipt_ack_independent_from_human_ack_and_retried(self):
        with mock.patch("sls_store.time.time", return_value=1000):
            record, _ = self.inbox.receive("one", "event", {}, receipt_event_id="event")
            outbox = self.inbox.pending_receipts("one")
            self.assertEqual(1, len(outbox))
            receipt = outbox[0]["receipt_id"]
            self.inbox.receipt_failed(receipt, "network down")
            self.assertEqual([], self.inbox.pending_receipts("one"))
            self.assertFalse(self.inbox.receive("one", "event", {}, receipt_event_id="event")[1])
        with mock.patch("sls_store.time.time", return_value=1003):
            self.assertEqual(receipt, self.inbox.pending_receipts("one")[0]["receipt_id"])
            self.inbox.receipt_sent(receipt)
            self.assertEqual("pending", self.inbox.record(record)["state"])
            self.inbox.receive("one", "event", {}, receipt_event_id="event")
            self.assertEqual([], self.inbox.pending_receipts("one"))
            self.inbox.respond(record, "safe")
            self.inbox.respond(record, "safe")
            self.assertEqual(1, self.inbox.db.execute("SELECT COUNT(*) FROM responses").fetchone()[0])
            self.assertEqual("acknowledged", self.inbox.record(record)["state"])

    def test_version_one_schema_migrates_without_losing_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inbox.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE notifications(record_id TEXT PRIMARY KEY,namespace TEXT,event_id TEXT,revision INTEGER,incident_id TEXT,payload TEXT,state TEXT,received REAL,displayed REAL,acknowledged REAL,retry_after REAL,attempts INTEGER,error TEXT)")
            db.execute("INSERT INTO notifications VALUES('record','one','event',1,'incident',?,'pending',1000,NULL,NULL,0,0,'')", (json.dumps({"severity": "critical"}),))
            db.execute("PRAGMA user_version=1")
            db.commit()
            db.close()
            inbox = Inbox(path)
            try:
                self.assertEqual("event", inbox.pending()[0]["event_id"])
                self.assertEqual(3, inbox.pending()[0]["priority"])
                self.assertEqual(2, inbox.db.execute("PRAGMA user_version").fetchone()[0])
            finally:
                inbox.close()


class ProtocolTests(unittest.TestCase):
    def test_latest_match_does_not_drop_partial_catchup_and_default_order(self):
        events = [{"id": "earlier"}, {"id": "latest"}]
        self.assertEqual(events, ordered_reconciliation({"events": events, "latest": events[-1]}, "latest"))
        self.assertEqual(events, ordered_reconciliation({"events": list(reversed(events)), "order": "descending"}))

    def test_reconciliation_includes_new_revision_and_explicit_sequence(self):
        events = [{"id": "a", "revision": 1, "sequence": 1}, {"id": "b", "sequence": 2}]
        latest = {"id": "a", "revision": 2, "sequence": 3}
        self.assertEqual([1, 2, 3], [item["sequence"] for item in ordered_reconciliation({"events": list(reversed(events)), "latest": latest})])

    def test_schema_bounds_and_boolean_contract(self):
        invalid = [{"schema_version": True}, {"test_only": "false"}, {"is_test": 1}, {"message": "a" * 131073},
                   {"id": "a", "event_id": "b"}, {"revision": True}, {"sequence": -1},
                   {"action": "cancel"}, {"display_timeout_seconds": -1}, {"display_timeout_seconds": True}]
        for payload in invalid:
            with self.subTest(payload=list(payload)), self.assertRaises(ValueError):
                validate_payload(payload)
        self.assertEqual({"display_timeout_seconds": 0, "display_expires_at": "obsolete"},
                         validate_payload({"display_timeout_seconds": 0, "display_expires_at": "obsolete"}))
        self.assertEqual("", validate_payload({"severity": None})["severity"])

    def test_explicit_test_fields_only_and_header_safe_ids(self):
        for payload in ({"is_test": True}, {"severity": "Test"}, {"status": "exercise"}, {"test_only": True}):
            self.assertTrue(is_test(payload))
        self.assertFalse(is_test({"message": "The word test does not classify this alert"}))
        value = "urn:oid:https://alerts.weather.gov/cap?event=a%2Fb&v=1"
        self.assertEqual(value, validate_id(value))
        for invalid in ("abc\r\nHeader: x", "abc\x00", "a" * 1025):
            with self.assertRaises(ValueError):
                validate_id(invalid)
        with self.assertRaises(ValueError):
            timestamp("2026-09-20T00:00:00")


if __name__ == "__main__":
    unittest.main()
