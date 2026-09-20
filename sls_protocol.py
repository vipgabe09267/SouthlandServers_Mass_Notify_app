"""Bounded, explicit desktop notification contract; no recursive field guessing."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlparse


ID_PATTERN = re.compile(r"[\x21-\x7e]{1,1024}\Z")


def validate_id(value: object, *, optional: bool = True) -> str:
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError("Event identifier must contain 1-1024 printable ASCII characters without whitespace")
    return value


def profile_namespace(profile: dict) -> str:
    parsed = urlparse(str(profile.get("endpoint", "")))
    origin = (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or 443)
    return hashlib.sha256(json.dumps([origin, profile.get("username", "")], separators=(",", ":")).encode()).hexdigest()


def timestamp(value: object) -> float | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("Invalid notification timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Notification timestamps must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("Notification timestamps must specify a timezone")
    try:
        result = parsed.astimezone(timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("Notification timestamp is outside the supported range") from exc
    if not math.isfinite(result):
        raise ValueError("Invalid notification timestamp")
    return result


def validate_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Notification must be an object")
    # Published weather producer forwards nullable NWS properties unchanged.
    # Normalize only these documented optional text fields, never routing flags.
    for name in ("severity", "message_type", "area", "effective", "expires", "description"):
        if name in payload and payload[name] is None:
            payload[name] = ""
    stack = [(payload, 0)]
    nodes = 0
    text_bytes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > 15000 or depth > 8:
            raise ValueError("Notification structure exceeds safety limits")
        if isinstance(value, dict):
            if len(value) > 1000:
                raise ValueError("Too many notification fields")
            if any(not isinstance(k, str) or len(k) > 100 for k in value):
                raise ValueError("Invalid notification field")
            stack.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            if len(value) > 10000:
                raise ValueError("Notification list exceeds safety limits")
            stack.extend((v, depth + 1) for v in value)
        elif isinstance(value, str):
            text_bytes += len(value.encode("utf-8"))
            if len(value) > 128 * 1024 or text_bytes > 256 * 1024:
                raise ValueError("Notification text exceeds safety limits")
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Non-finite numbers are not supported")
        elif not isinstance(value, (str, int, float, bool, type(None))):
            raise ValueError("Unsupported notification value")
    if type(payload.get("schema_version", 1)) is not int or payload.get("schema_version", 1) != 1:
        raise ValueError("Unsupported desktop notification schema version")
    if "kind" in payload and (not isinstance(payload["kind"], str) or payload["kind"] not in {"alert", "announcement"}):
        raise ValueError("Unsupported notification category")
    for name in ("desktop_all", "test_only", "test", "is_test"):
        if name in payload and not isinstance(payload[name], bool):
            raise ValueError(f"{name} must be a boolean")
    recipients = payload.get("desktop_recipients", [])
    if isinstance(recipients, str):
        recipients = [recipients]  # Existing PBX contract compatibility.
    if not isinstance(recipients, list) or len(recipients) > 10000 or any(not isinstance(v, str) or len(v) > 256 for v in recipients):
        raise ValueError("Invalid desktop recipient list")
    for name in ("id", "event_id", "incident_id"):
        validate_id(payload.get(name))
    if payload.get("id") and payload.get("event_id") and payload["id"] != payload["event_id"]:
        raise ValueError("Notification id and event_id disagree")
    revision = payload.get("revision", 0)
    if type(revision) is not int or revision < 0 or revision > 2147483647:
        raise ValueError("Invalid notification revision")
    if payload.get("action", "notify") not in {"notify", "update", "cancel", "all_clear"}:
        raise ValueError("Unsupported notification action")
    if payload.get("action", "notify") in {"update", "cancel", "all_clear"}:
        if not payload.get("incident_id") or revision < 1:
            raise ValueError("Incident updates and closures require an incident_id and positive revision")
    for name in ("title", "message", "body", "description", "effective", "expires", "severity", "priority_label", "language", "status"):
        if name in payload and not isinstance(payload[name], str):
            raise ValueError(f"{name} must be text")
    presentation = payload.get("presentation", {})
    if not isinstance(presentation, dict):
        raise ValueError("Notification presentation must be an object")
    if "sequence" in payload and (type(payload["sequence"]) is not int or not 0 <= payload["sequence"] <= 2**63 - 1):
        raise ValueError("Invalid notification sequence")
    effective, expires = timestamp(payload.get("effective")), timestamp(payload.get("expires"))
    timestamp(payload.get("created_at"))
    if effective is not None and expires is not None and expires <= effective:
        raise ValueError("Notification expiry must follow its effective time")
    if "display_timeout_seconds" in payload:
        timeout = payload["display_timeout_seconds"]
        if type(timeout) is not int or not 0 <= timeout <= 86400:
            raise ValueError("Display timeout must be an integer from 0 to 86400 seconds")
    if payload.get("display_timeout_seconds") != 0:
        timestamp(payload.get("display_expires_at"))
    return payload


def is_test(payload: object) -> bool:
    return isinstance(payload, dict) and (
        payload.get("test_only") is True or payload.get("test") is True or payload.get("is_test") is True
        or str(payload.get("severity", "")).lower() == "test"
        or str(payload.get("status", "")).lower() in {"test", "exercise"}
    )


def notification_priority(*, severity: object = "", priority: object = "", priority_label: object = "", kind: object = "") -> int:
    """Shared delivery/display order: zero is most urgent; never guess numeric scales."""
    values = {str(value).strip().lower() for value in (severity, priority, priority_label)}
    if values & {"extreme", "severe", "critical", "emergency", "immediate"}:
        return 0
    if values & {"urgent", "warning", "high"}:
        return 1
    if values & {"moderate", "normal", "medium"}:
        return 2
    if values & {"minor", "low", "advisory", "notice", "info", "informational", "test"} or str(kind).lower() == "announcement":
        return 3
    return 1


class LiveSnapshotBaseline:
    """Only new publications observed during uninterrupted polling are eligible."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.keys = None
        self.server_time = None
        self.local_time = None

    def observe(self, records, server_time, local_time):
        keys = {(str(row.get("id", row.get("event_id", ""))), str(row.get("revision", 0))) for row in records}
        fresh = []
        continuous = (self.keys is not None and self.server_time is not None
                      and 0 <= local_time - self.local_time <= 15
                      and server_time >= self.server_time)
        if continuous:
            for row in records:
                key = (str(row.get("id", row.get("event_id", ""))), str(row.get("revision", 0)))
                try:
                    published = timestamp(row.get("created_at"))
                except (ValueError, TypeError):
                    continue
                if (key not in self.keys and published is not None
                        and self.server_time - 1 <= published <= server_time + 1):
                    fresh.append(row)
        self.keys, self.server_time, self.local_time = keys, server_time, local_time
        return fresh


def ordered_reconciliation(data: object, last_event_id: str = "") -> list[dict]:
    """Return the complete retained window, oldest first. Inbox performs deduplication."""
    if not isinstance(data, dict):
        raise ValueError("Recent-event response must be an object")
    raw = data.get("events", [])
    if not isinstance(raw, list) or len(raw) > 1000:
        raise ValueError("Invalid recent-event list")
    if any(not isinstance(v, dict) for v in raw):
        raise ValueError("Recent events must all be objects")
    events = list(raw)
    if data.get("order") not in (None, "ascending", "descending"):
        raise ValueError("Unsupported recent-event ordering")
    if data.get("order") == "descending":
        events.reverse()
    latest = data.get("latest")
    # A latest-only older PBX is supported, but cannot prove gap-free replay.
    if isinstance(latest, dict):
        latest_id = (latest.get("id", latest.get("event_id")), latest.get("revision", 0))
        if not any((v.get("id", v.get("event_id")), v.get("revision", 0)) == latest_id for v in events):
            events.append(latest)
    if events and all(type(v.get("sequence")) is int for v in events):
        events.sort(key=lambda v: v["sequence"])
    return events
