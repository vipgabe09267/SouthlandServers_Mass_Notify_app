"""Durable local inbox with transactional receipt, replay, and incident ordering."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import sqlite3
import threading
import time
from pathlib import Path

from sls_protocol import timestamp, validate_id, notification_priority


class InboxFullError(RuntimeError):
    pass


def _priority(payload: dict) -> int:
    return 3 - notification_priority(**{key: payload.get(key, "") for key in ("severity", "priority", "priority_label", "kind")})


def _times(payload: dict) -> tuple[float | None, float | None]:
    effective, expires = timestamp(payload.get("effective")), timestamp(payload.get("expires"))
    if str(payload.get("kind", "")).lower() == "announcement":
        if payload.get("display_timeout_seconds") == 0:
            expires = None
        elif payload.get("display_timeout_seconds") is not None:
            expires = timestamp(payload.get("display_expires_at"))
        elif payload.get("display_expires_at"):
            expires = timestamp(payload["display_expires_at"])
    delivery_expires = timestamp(payload.get("delivery_expires_at"))
    if delivery_expires is not None:
        expires = min(expires, delivery_expires) if expires is not None else delivery_expires
    return effective, expires


class Inbox:
    TERMINAL = {"acknowledged", "dismissed", "expired", "superseded", "cancelled"}

    def __init__(self, path: Path | str, *, max_pending: int = 10000) -> None:
        if type(max_pending) is not int or not 1 <= max_pending <= 100000:
            raise ValueError("Invalid pending inbox capacity")
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.max_pending = max_pending
        self.db = sqlite3.connect(str(path), timeout=10, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @contextlib.contextmanager
    def _transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def _migrate(self) -> None:
        with self._transaction():
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("This inbox was written by a newer application; refusing downgrade")
            self.db.execute("""CREATE TABLE IF NOT EXISTS notifications (
                record_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, event_id TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0, incident_id TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', received REAL NOT NULL,
                displayed REAL, acknowledged REAL, retry_after REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 0, effective REAL, expires REAL,
                UNIQUE(namespace,event_id,revision))""")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(notifications)")}
            for name, definition in (("priority", "INTEGER NOT NULL DEFAULT 0"), ("effective", "REAL"), ("expires", "REAL")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE notifications ADD COLUMN {name} {definition}")
            self.db.execute("CREATE INDEX IF NOT EXISTS inbox_priority ON notifications(state,priority DESC,retry_after,received)")
            self.db.execute("CREATE INDEX IF NOT EXISTS inbox_incident ON notifications(namespace,incident_id,revision)")
            self.db.execute("CREATE TABLE IF NOT EXISTS cursors(namespace TEXT PRIMARY KEY,stream_id TEXT NOT NULL DEFAULT '',event_id TEXT NOT NULL DEFAULT '')")
            self.db.execute("""CREATE TABLE IF NOT EXISTS receipts(record_id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,event_id TEXT NOT NULL,revision INTEGER NOT NULL,received REAL NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS incidents(namespace TEXT NOT NULL,
                incident_id TEXT NOT NULL,revision INTEGER NOT NULL,closed INTEGER NOT NULL DEFAULT 0,
                record_id TEXT NOT NULL,PRIMARY KEY(namespace,incident_id))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS responses(response_id TEXT PRIMARY KEY,
                record_id TEXT NOT NULL,response TEXT NOT NULL,created REAL NOT NULL,sent REAL,
                attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '')""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS receipt_outbox(receipt_id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,event_id TEXT NOT NULL,created REAL NOT NULL,sent REAL,
                attempts INTEGER NOT NULL DEFAULT 0,retry_after REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',UNIQUE(namespace,event_id))""")
            self.db.execute("CREATE INDEX IF NOT EXISTS receipts_to_send ON receipt_outbox(namespace,sent,retry_after)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS quarantine (
                entry_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, event_id TEXT NOT NULL,
                payload TEXT NOT NULL, error TEXT NOT NULL, received REAL NOT NULL)""")
            if version < 2:
                self.db.execute("INSERT OR IGNORE INTO receipts SELECT record_id,namespace,event_id,revision,received FROM notifications")
                for row in self.db.execute("SELECT * FROM notifications ORDER BY received,record_id").fetchall():
                    try:
                        payload = json.loads(row["payload"])
                        effective, expires = _times(payload)
                        self.db.execute("UPDATE notifications SET priority=?,effective=?,expires=?,retry_after=MAX(retry_after,?) WHERE record_id=?",
                                        (_priority(payload), effective, expires, effective or 0, row["record_id"]))
                        # Retain past closures even if their displayed history was acknowledged.
                        if row["incident_id"] and (not effective or effective <= time.time()):
                            self._activate_incident(dict(row), payload)
                    except (ValueError, TypeError, AttributeError):
                        self.db.execute("UPDATE notifications SET state='expired',error='Invalid stored notification during schema migration' WHERE record_id=?", (row["record_id"],))
            self.db.execute("PRAGMA user_version=2")

    def _activate_incident(self, row: dict, payload: dict) -> None:
        """Called only within the receipt/activation transaction, never by the UI."""
        incident_id = row["incident_id"]
        if not incident_id or row["state"] == "expired":
            return
        namespace, revision, record_id = row["namespace"], row["revision"], row["record_id"]
        action = payload.get("action", "notify")
        closed = action in {"cancel", "all_clear"}
        previous = self.db.execute("SELECT * FROM incidents WHERE namespace=? AND incident_id=?", (namespace, incident_id)).fetchone()
        if previous and previous["record_id"] != record_id:
            stale = revision < previous["revision"] or (revision == previous["revision"] and
                    (previous["closed"] or (revision > 0 and not closed)))
            if stale:
                self.db.execute("UPDATE notifications SET state='superseded' WHERE record_id=? AND state IN ('pending','displayed')", (record_id,))
                return
        if previous and previous["record_id"] == record_id:
            return
        self.db.execute("""INSERT INTO incidents(namespace,incident_id,revision,closed,record_id) VALUES(?,?,?,?,?)
            ON CONFLICT(namespace,incident_id) DO UPDATE SET revision=excluded.revision,closed=excluded.closed,record_id=excluded.record_id""",
                        (namespace, incident_id, revision, int(closed), record_id))
        if revision or closed:
            self.db.execute("""UPDATE notifications SET state=? WHERE namespace=? AND incident_id=?
                AND record_id!=? AND revision<=? AND state IN ('pending','displayed')
                AND (effective IS NULL OR effective<=?)""",
                            ("cancelled" if closed else "superseded", namespace, incident_id, record_id, revision, time.time()))

    def receive(self, namespace: str, event_id: str, payload: dict, *, revision: int = 0,
                incident_id: str = "", stream_id: str = "", state: str = "pending",
                receipt_event_id: str = "") -> tuple[str, bool]:
        validate_id(namespace, optional=False)
        validate_id(event_id, optional=False)
        validate_id(incident_id)
        validate_id(stream_id)
        validate_id(receipt_event_id)
        if type(revision) is not int or not 0 <= revision <= 2147483647:
            raise ValueError("Invalid notification revision")
        if state not in {"pending", "expired", "cancelled"} or not isinstance(payload, dict):
            raise ValueError("Invalid initial inbox state or payload")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Notification exceeds inbox size limit")
        effective, expires = _times(payload)
        if state != "expired" and effective is not None and expires is not None and expires <= effective:
            raise ValueError("Expiry must follow effective time")
        now = time.time()
        if expires is not None and expires <= now:
            state = "expired"
        record_id = hashlib.sha256(json.dumps([namespace, event_id, revision]).encode()).hexdigest()
        with self._transaction():
            exists = self.db.execute("SELECT 1 FROM receipts WHERE record_id=?", (record_id,)).fetchone()
            if not exists:
                self.db.execute("""INSERT INTO notifications(record_id,namespace,event_id,revision,incident_id,payload,state,received,priority,effective,expires,retry_after)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (record_id, namespace, event_id, revision, incident_id, encoded, state, now, _priority(payload), effective, expires, effective or 0))
                if effective is None or effective <= now:
                    self._activate_incident({"namespace": namespace, "revision": revision, "record_id": record_id,
                                             "incident_id": incident_id, "state": state}, payload)
                # Count after supersession so replacing an existing incident does not
                # fail at capacity. Any overflow rolls back BOTH receipt and cursor.
                count = self.db.execute("SELECT COUNT(*) FROM notifications WHERE state IN ('pending','displayed')").fetchone()[0]
                if count > self.max_pending:
                    raise InboxFullError("Notification inbox is full; this live delivery could not be stored")
                self.db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)", (record_id, namespace, event_id, revision, now))
            if receipt_event_id:
                receipt_id = hashlib.sha256(json.dumps([namespace, receipt_event_id]).encode()).hexdigest()
                self.db.execute("INSERT OR IGNORE INTO receipt_outbox(receipt_id,namespace,event_id,created) VALUES(?,?,?,?)",
                                (receipt_id, namespace, receipt_event_id, now))
            # Opaque SSE IDs are ordered by the single live reader, never by event
            # IDs or catch-up ordering. No catch-up receipt changes the SSE cursor.
            self.db.execute("""INSERT INTO cursors(namespace,stream_id,event_id) VALUES(?,?,?)
                ON CONFLICT(namespace) DO UPDATE SET stream_id=CASE WHEN excluded.stream_id!=''
                THEN excluded.stream_id ELSE cursors.stream_id END,event_id=excluded.event_id""", (namespace, stream_id, event_id))
        return record_id, not bool(exists)

    def pending_receipts(self, namespace: str, *, limit: int = 16) -> list[dict]:
        with self.lock:
            rows = self.db.execute("""SELECT * FROM receipt_outbox WHERE namespace=? AND sent IS NULL
                AND retry_after<=? ORDER BY created,receipt_id LIMIT ?""", (namespace, time.time(), max(1, min(limit, 256)))).fetchall()
        return [dict(row) for row in rows]

    def receipt_sent(self, receipt_id: str) -> None:
        with self._transaction():
            self.db.execute("UPDATE receipt_outbox SET sent=COALESCE(sent,?),error='' WHERE receipt_id=?", (time.time(), receipt_id))

    def receipt_failed(self, receipt_id: str, error: str = "") -> None:
        with self._transaction():
            row = self.db.execute("SELECT attempts FROM receipt_outbox WHERE receipt_id=? AND sent IS NULL", (receipt_id,)).fetchone()
            if row is None:
                return
            attempts = row[0] + 1
            self.db.execute("UPDATE receipt_outbox SET attempts=?,retry_after=?,error=? WHERE receipt_id=?",
                            (attempts, time.time() + min(300, 2 ** min(attempts, 9)), str(error)[:500], receipt_id))

    def cursor(self, namespace: str) -> str:
        with self.lock:
            row = self.db.execute("SELECT stream_id FROM cursors WHERE namespace=?", (namespace,)).fetchone()
            return row[0] if row else ""

    def quarantine(self, namespace: str, event_id: str, payload: object, error: str,
                   *, stream_id: str = "") -> None:
        """Retain a rejected record without poisoning valid later stream events.

        Quarantine does not create a delivery receipt or normal deduplication
        tombstone. A repaired copy with the same event ID can still be accepted.
        """
        validate_id(namespace, optional=False)
        validate_id(event_id, optional=False)
        validate_id(stream_id)
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        if len(encoded) > 1024 * 1024:
            raise ValueError("Rejected notification exceeds quarantine size limit")
        entry_id = hashlib.sha256(json.dumps([namespace, event_id, encoded]).encode()).hexdigest()
        with self._transaction():
            if not self.db.execute("SELECT 1 FROM quarantine WHERE entry_id=?", (entry_id,)).fetchone():
                if self.db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] >= 1000:
                    raise InboxFullError("Rejected notification storage is full; operator review is required")
                self.db.execute("INSERT INTO quarantine VALUES(?,?,?,?,?,?)",
                                (entry_id, namespace, event_id, encoded, str(error)[:500], time.time()))
            if stream_id:
                self.db.execute("""INSERT INTO cursors(namespace,stream_id,event_id) VALUES(?,?,?)
                    ON CONFLICT(namespace) DO UPDATE SET stream_id=excluded.stream_id""", (namespace, stream_id, event_id))

    def quarantined(self, *, limit: int = 20) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.db.execute(
                "SELECT event_id,error,received FROM quarantine ORDER BY received DESC LIMIT ?", (max(1, min(limit, 100)),))]

    def advance_cursor(self, namespace: str, stream_id: str, *, force: bool = False) -> bool:
        """Capture authenticated baseline only when no durable stream cursor exists.

        Normal notification receipt must use receive(), which atomically stores the
        alert and cursor. force exists for an explicit operator/reset workflow only.
        """
        validate_id(namespace, optional=False)
        validate_id(stream_id, optional=False)
        with self._transaction():
            current = self.db.execute("SELECT stream_id FROM cursors WHERE namespace=?", (namespace,)).fetchone()
            if current and current[0] and not force:
                return False
            self.db.execute("""INSERT INTO cursors(namespace,stream_id,event_id) VALUES(?,?,'')
                ON CONFLICT(namespace) DO UPDATE SET stream_id=excluded.stream_id""", (namespace, stream_id))
        return True

    def record(self, record_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM notifications WHERE record_id=?", (record_id,)).fetchone()
        return self._decode(row) if row else None

    def expire_active(self, namespace: str | None = None, *, include_displayed: bool = True) -> None:
        """Retire the delivery queue while preserving history and receipt ACKs."""
        states = "state IN ('pending','displayed')" if include_displayed else "state='pending'"
        parameters = () if namespace is None else (namespace,)
        scope = "" if namespace is None else " AND namespace=?"
        with self._transaction():
            self.db.execute("UPDATE notifications SET state='expired' WHERE " + states + scope, parameters)

    @staticmethod
    def _decode(row) -> dict:
        record = dict(row)
        record["payload"] = json.loads(record["payload"])
        return record

    def pending(self, *, limit: int = 32, excluding=(), namespaces=None) -> list[dict]:
        excluding = tuple(excluding)
        namespaces = None if namespaces is None else tuple(namespaces)
        if namespaces == ():
            return []
        now = time.time()
        with self._transaction():
            self.db.execute("UPDATE notifications SET state='expired' WHERE state IN ('pending','displayed') AND expires<=?", (now,))
            # Activate all due lifecycle changes before selecting a page, including
            # a low-priority all-clear that follows many urgent older records.
            due = self.db.execute("SELECT * FROM notifications WHERE state IN ('pending','displayed') AND retry_after<=? AND (effective IS NULL OR effective<=?) AND incident_id!='' ORDER BY received,record_id", (now, now)).fetchall()
            for row in due:
                self._activate_incident(dict(row), json.loads(row["payload"]))
            clauses, parameters = ["state IN ('pending','displayed')", "retry_after<=?", "(effective IS NULL OR effective<=?)"], [now, now]
            if excluding:
                clauses.append("record_id NOT IN (" + ",".join("?" for _ in excluding) + ")")
                parameters.extend(excluding)
            if namespaces is not None:
                clauses.append("namespace IN (" + ",".join("?" for _ in namespaces) + ")")
                parameters.extend(namespaces)
            parameters.append(max(1, min(int(limit), 256)))
            rows = self.db.execute("SELECT * FROM notifications WHERE " + " AND ".join(clauses) + " ORDER BY priority DESC,received,record_id LIMIT ?", parameters).fetchall()
        return [self._decode(row) for row in rows]

    def mark(self, record_id: str, state: str, *, error: str = "") -> None:
        if state not in self.TERMINAL | {"pending", "displayed"}:
            raise ValueError("Unknown inbox state")
        now = time.time()
        with self._transaction():
            self.db.execute("""UPDATE notifications SET state=?,displayed=CASE WHEN ?='displayed' THEN COALESCE(displayed,?) ELSE displayed END,
                acknowledged=CASE WHEN ?='acknowledged' THEN COALESCE(acknowledged,?) ELSE acknowledged END,error=?
                WHERE record_id=? AND state IN ('pending','displayed')""", (state, state, now, state, now, str(error)[:500], record_id))

    def failed(self, record_id: str, error: str) -> None:
        with self._transaction():
            row = self.db.execute("SELECT attempts FROM notifications WHERE record_id=? AND state IN ('pending','displayed')", (record_id,)).fetchone()
            if row is None:
                return
            attempts = row[0] + 1
            self.db.execute("UPDATE notifications SET state='pending',attempts=?,retry_after=MAX(COALESCE(effective,0),?),error=? WHERE record_id=?", (attempts, time.time() + min(60, 2 ** min(attempts, 6)), str(error)[:500], record_id))

    def defer(self, record_id: str, until: float) -> None:
        if not isinstance(until, (int, float)) or not math.isfinite(until):
            raise ValueError("Invalid notification retry time")
        with self._transaction():
            self.db.execute("UPDATE notifications SET retry_after=MAX(COALESCE(effective,0),?) WHERE record_id=? AND state IN ('pending','displayed')", (until, record_id))

    def cancel_incident(self, namespace: str, incident_id: str, *, except_record: str = "") -> None:
        if not incident_id:
            return
        with self._transaction():
            current = self.db.execute("SELECT * FROM incidents WHERE namespace=? AND incident_id=?", (namespace, incident_id)).fetchone()
            if except_record:
                # The receive transaction establishes which control is authoritative.
                # A duplicate older control must never cancel a newer incident.
                if current is None or current["record_id"] != except_record or not current["closed"]:
                    return
                self.db.execute("""UPDATE notifications SET state='cancelled' WHERE namespace=? AND incident_id=?
                    AND record_id!=? AND revision<=? AND state IN ('pending','displayed') AND (effective IS NULL OR effective<=?)""",
                                (namespace, incident_id, except_record, current["revision"], time.time()))
            elif current:
                self.db.execute("UPDATE incidents SET closed=1 WHERE namespace=? AND incident_id=?", (namespace, incident_id))
                self.db.execute("UPDATE notifications SET state='cancelled' WHERE namespace=? AND incident_id=? AND state IN ('pending','displayed')", (namespace, incident_id))

    def respond(self, record_id: str, response: str) -> None:
        if response not in {"acknowledged", "safe", "need_help", "evacuated"}:
            raise ValueError("Unsupported response")
        response_id = hashlib.sha256(f"{record_id}:{response}".encode()).hexdigest()
        with self._transaction():
            row = self.db.execute("SELECT state FROM notifications WHERE record_id=?", (record_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown notification")
            if row["state"] in {"cancelled", "expired", "superseded"}:
                return
            self.db.execute("INSERT OR IGNORE INTO responses(response_id,record_id,response,created) VALUES(?,?,?,?)", (response_id, record_id, response, time.time()))
            self.db.execute("UPDATE notifications SET state='acknowledged',acknowledged=COALESCE(acknowledged,?) WHERE record_id=?", (time.time(), record_id))

    def history(self, query: str = "", *, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM notifications WHERE instr(lower(payload),lower(?))>0 ORDER BY received DESC LIMIT ?", (query[:200], max(1, min(limit, 1000)))).fetchall()
        return [self._decode(row) for row in rows]

    def statistics(self) -> dict:
        with self.lock:
            counts = dict(self.db.execute("SELECT state,COUNT(*) FROM notifications GROUP BY state").fetchall())
            counts["pending_receipts"] = self.db.execute("SELECT COUNT(*) FROM receipt_outbox WHERE sent IS NULL").fetchone()[0]
            rejected = self.db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
            if rejected:
                counts["quarantined"] = rejected
            return counts

    stats = statistics

    def prune(self, *, retention_days: int = 30) -> int:
        cutoff = time.time() - max(1, retention_days) * 86400
        with self._transaction():
            # Minimal receipts and incident high-water marks survive content retention.
            # Pending delivery and unsent responses must never be silently discarded.
            result = self.db.execute("DELETE FROM notifications WHERE received<? AND state IN ('acknowledged','dismissed','expired','superseded','cancelled') AND record_id NOT IN (SELECT record_id FROM responses WHERE sent IS NULL)", (cutoff,))
            self.db.execute("DELETE FROM responses WHERE sent IS NOT NULL AND record_id NOT IN (SELECT record_id FROM notifications)")
            self.db.execute("DELETE FROM receipt_outbox WHERE sent IS NOT NULL AND created<?", (cutoff,))
            self.db.execute("DELETE FROM quarantine WHERE received<?", (cutoff,))
            return result.rowcount

    def close(self) -> None:
        with self.lock:
            self.db.close()
