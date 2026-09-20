"""Bounded, accessible desktop alert presentation.

The caller owns authentication, durable delivery state and network policy. This
module runs on the Tk thread; optional image downloads never touch Tk objects.
Acknowledgments are local callbacks, not claims of a successful server response.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import logging
import math
import queue
import threading
import time
from typing import Any, Callable, Iterable, Mapping

from sls_protocol import notification_priority
from sls_windowing import fit_window


LOG = logging.getLogger(__name__)
MAX_PENDING = 128
MAX_HISTORY = 500
MAX_VISIBLE = 2
NONCRITICAL_SECONDS = 120
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000


def field(alert: Any, name: str, default: Any = "") -> Any:
    return alert.get(name, default) if isinstance(alert, Mapping) else getattr(alert, name, default)


def text(value: Any) -> str:
    return "" if value is None else str(value)


def notification_source(alert: Any) -> Mapping[str, Any]:
    raw = field(alert, "raw", {})
    if not isinstance(raw, Mapping):
        return {}
    for key in ("latest", "latest_alert", "latestAlert", "latest_notification", "notification"):
        if isinstance(raw.get(key), Mapping):
            return raw[key]
    return raw


def explicit_test_mode(alert: Any) -> bool | None:
    """Never infer drills from free-text titles or instructions."""
    source = notification_source(alert)
    for key in ("test_only", "test", "is_test", "isTest"):
        value = source.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower().strip() in ("true", "false"):
            return value.lower().strip() == "true"
    status = text(source.get("status", "")).lower().strip()
    if status in ("test", "exercise", "drill"):
        return True
    if status in ("actual", "live"):
        return False
    # Some existing PBX test payloads use a structured severity of "Test".
    # Text in a title or message remains insufficient evidence of a drill.
    if text(source.get("severity", field(alert, "severity"))).strip().lower() == "test":
        return True
    return None


def test_marker(alert: Any) -> str:
    mode = explicit_test_mode(alert)
    if mode is True:
        return "TEST / EXERCISE — follow your organization's drill instructions"
    if mode is False:
        return "LIVE ALERT"
    return "ALERT — sender has not specified test or live mode"


def parse_timestamp(value: Any) -> datetime | None:
    """Only compare absolute timestamps; guessing a sender timezone is unsafe."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(text(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class PresentationPolicy:
    priority: int
    persistent: bool
    timeout_seconds: int | None
    effective: datetime | None
    expires: datetime | None
    timing_warning: str = ""


def presentation_policy(alert: Any) -> PresentationPolicy:
    priority = notification_priority(**{key: field(alert, key) for key in
                                       ("severity", "priority", "priority_label", "kind")})
    persistent = priority <= 1
    effective = parse_timestamp(field(alert, "effective"))
    expires = parse_timestamp(field(alert, "expires"))
    # A sender's absolute validity takes precedence over the local routine-popup
    # default, irrespective of severity. Its deadline is checked on every tick.
    timeout_seconds = None if persistent or expires is not None else NONCRITICAL_SECONDS
    timing_warning = ""
    source = notification_source(alert)
    if text(field(alert, "kind")).lower() == "announcement":
        timeout = field(alert, "display_timeout_seconds", None)
        if timeout is None:
            timeout = source.get("display_timeout_seconds")
        if isinstance(timeout, str) and timeout.isdigit() and len(timeout) <= 9:
            timeout = int(timeout)
        absolute = field(alert, "display_expires_at", None) or source.get("display_expires_at")
        if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout >= 0:
            # This is an absolute server policy. Never restart it on replay,
            # process restart, queue preemption, or a newly authenticated stream.
            timeout_seconds = None
            expires = None if timeout == 0 else parse_timestamp(absolute)
            if timeout > 0 and expires is None:
                timing_warning = "The sender's expiration time is unavailable. Close this notification manually."
        elif absolute:
            timeout_seconds = None
            expires = parse_timestamp(absolute)
            if expires is None:
                timing_warning = "The sender's expiration time is invalid. Close this notification manually."
    delivery_expires = parse_timestamp(field(alert, "delivery_expires_at"))
    if delivery_expires is not None:
        expires = min(expires, delivery_expires) if expires is not None else delivery_expires
        timing_warning = ""
    return PresentationPolicy(priority, persistent, timeout_seconds, effective, expires, timing_warning)


def incident_key(alert: Any) -> str:
    return text(field(alert, "incident_id")).strip()


def revision(alert: Any) -> int | None:
    value = field(alert, "revision", None)
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit() and len(value) <= 18:
        return int(value)
    return None


def can_supersede(new: Any, old: Any) -> bool:
    """Coalesce only explicitly ordered revisions of the same scoped incident."""
    key = incident_key(new)
    new_rev, old_rev = revision(new), revision(old)
    return bool(key and key == incident_key(old) and new_rev is not None
                and old_rev is not None and new_rev > old_rev)


@dataclass
class PendingAlert:
    record_id: str
    alert: Any
    sequence: int
    policy: PresentationPolicy
    status: str = "pending"
    received_at: str = ""


def ordered_ready(items: Iterable[PendingAlert], now: datetime) -> list[PendingAlert]:
    return sorted((item for item in items
                   if (item.policy.effective is None or item.policy.effective <= now)
                   and (item.policy.expires is None or item.policy.expires > now)),
                  key=lambda item: (item.policy.priority, item.sequence))


def should_play_audio(new_priority: int, active_priority: int | None,
                      now: float, active_until: float) -> bool:
    """Lower-priority or equal-priority sounds cannot interrupt active audio."""
    return active_priority is None or now >= active_until or new_priority < active_priority




class AlertPresenter:
    def __init__(self, root: Any, on_displayed: Callable[[str], Any],
                 on_acknowledged: Callable[[str, str], Any],
                 on_failed: Callable[[str, str], Any], *,
                 play_sound: Callable[[Any], Any] | None = None,
                 stop_sound: Callable[[], Any] | None = None,
                 image_loader: Callable[[Any], bytes | None] | None = None,
                 on_state_changed: Callable[[], Any] | None = None,
                 can_display: Callable[[str], bool] | None = None,
                 on_discarded: Callable[[str], Any] | None = None,
                 max_pending: int = MAX_PENDING) -> None:
        self.root = root
        self.on_displayed = on_displayed
        self.on_acknowledged = on_acknowledged
        self.on_failed = on_failed
        self.play_sound = play_sound
        self.stop_sound = stop_sound
        self.image_loader = image_loader
        self.on_state_changed = on_state_changed
        self.can_display = can_display
        self.on_discarded = on_discarded
        self.max_pending = max(1, max_pending)
        self.pending: dict[str, PendingAlert] = {}
        self.visible: dict[str, _AlertView] = {}
        self.history: deque[PendingAlert] = deque(maxlen=MAX_HISTORY)
        self._sequence = 0
        self._closed = False
        self._draining = False
        self._audio_priority: int | None = None
        self._audio_until = 0.0
        self._audio_record = ""
        self._after_id: Any = self.root.after(250, self._tick)
        self._history_window: Any = None
        self._image_slots = threading.BoundedSemaphore(2)
        self._last_guard_check = 0.0

    @property
    def has_active_critical(self) -> bool:
        return any(view.item.policy.persistent for view in self.visible.values())

    @property
    def pending_count(self) -> int:
        return len(self.pending)

    def submit(self, record_id: str, alert: Any) -> bool:
        if self._closed:
            return False
        record_id = text(record_id)
        if not record_id:
            raise ValueError("Durable record ID is required")
        if self.can_display is not None and not self.can_display(record_id):
            self.discard(record_id)
            return True
        if record_id in self.pending or record_id in self.visible:
            return True
        if any(item.record_id == record_id for item in self.history):
            return True
        current = list(self.pending.values()) + [view.item for view in self.visible.values()]
        policy = presentation_policy(alert)
        superseded = [item for item in current if can_supersede(alert, item.alert)]
        for item in current + list(self.history):
            if can_supersede(item.alert, alert):
                return self._terminal(PendingAlert(record_id, alert, self._sequence,
                                                   policy), "superseded")
        if policy.expires is not None and policy.expires <= datetime.now(timezone.utc):
            return self._terminal(PendingAlert(record_id, alert, self._sequence, policy), "expired")
        for item in superseded:
            if not self._finish(item.record_id, "superseded"):
                return False
        current = list(self.pending.values()) + [view.item for view in self.visible.values()]
        if len(current) >= self.max_pending:
            lower_priority = [item for item in current if item.policy.priority > policy.priority]
            if not lower_priority or self.on_discarded is None:
                return False  # Caller retains this item in its durable inbox.
            # Release only a less important record. Among the least important,
            # release a queued item before an already visible one, then prefer
            # the newest item so earlier notifications keep their FIFO position.
            displaced = max(lower_priority, key=lambda item: (
                item.policy.priority, item.record_id in self.pending, item.sequence))
            self.discard(displaced.record_id)
        self._sequence += 1
        item = PendingAlert(record_id, alert, self._sequence, policy,
                            received_at=datetime.now(timezone.utc).isoformat())
        self.pending[record_id] = item
        self._drain()
        return True

    def cancel(self, incident_id: str) -> None:
        if not incident_id:
            return
        matches = [item.record_id for item in self.pending.values() if incident_key(item.alert) == incident_id]
        matches += [key for key, view in self.visible.items() if incident_key(view.item.alert) == incident_id]
        for key in matches:
            self._finish(key, "cancelled")
        self._drain()

    def discard(self, record_id: str) -> None:
        """Release a presenter slot without a new ACK or terminal-state write.

        The store owns delivery state. on_discarded releases the caller's scheduled
        bookkeeping, allowing displaced lower-priority records to be retried.
        Records retired by storage stay retired; active records stay pending.
        """
        self.pending.pop(record_id, None)
        view = self.visible.pop(record_id, None)
        if view is not None:
            view.close()
        if self._audio_record == record_id:
            self._stop_audio()
        if self.on_discarded:
            self.on_discarded(record_id)
        self._changed()

    def _discard_inactive(self) -> None:
        if self.can_display is None:
            return
        for record_id in list(self.pending) + list(self.visible):
            if not self.can_display(record_id):
                self.discard(record_id)

    def _changed(self) -> None:
        if self.on_state_changed and not self._closed:
            self.root.after_idle(self.on_state_changed)

    def _remember(self, item: PendingAlert) -> None:
        # Durable history belongs to the inbox; keep a small text-only UI cache.
        keys = ("title", "body", "description", "area", "incident_id", "revision", "severity", "kind")
        cached_alert = {key: field(item.alert, key) for key in keys}
        self.history.append(PendingAlert(item.record_id, cached_alert, item.sequence,
                                         item.policy, item.status, item.received_at))

    def _terminal(self, item: PendingAlert, status: str) -> bool:
        item.status = status
        try:
            self.on_acknowledged(item.record_id, status)
        except Exception as exc:
            # Do not claim a response was saved when its durable callback failed.
            LOG.exception("Unable to persist alert state %s", item.record_id)
            self.on_failed(item.record_id, f"Unable to persist {status}: {exc}")
            return False
        self._remember(item)
        self._changed()
        return True

    def _finish(self, record_id: str, status: str) -> bool:
        if self.can_display is not None and not self.can_display(record_id):
            self.discard(record_id)
            return True
        view = self.visible.get(record_id)
        item = view.item if view else self.pending.get(record_id)
        if item is None:
            return False
        try:
            self.on_acknowledged(record_id, status)
        except Exception as exc:
            LOG.exception("Unable to persist alert state %s", record_id)
            if view:
                view.status_var.set("Unable to save this response. The alert remains open; try again.")
            self.on_failed(record_id, f"Unable to persist {status}: {exc}")
            return False
        self.visible.pop(record_id, None)
        self.pending.pop(record_id, None)
        if view:
            view.close()
        if self._audio_record == record_id:
            self._stop_audio()
        item.status = status
        self._remember(item)
        self._changed()
        return True

    def _respond(self, record_id: str, status: str) -> None:
        self._finish(record_id, status)
        self._drain()

    def _stop_audio(self) -> None:
        if self.stop_sound:
            try:
                self.stop_sound()
            except Exception:
                LOG.exception("Unable to stop alert audio")
        self._audio_priority = None
        self._audio_until = 0.0
        self._audio_record = ""

    def _play_audio(self, item: PendingAlert, view: _AlertView) -> None:
        if not self.play_sound:
            return
        now = time.monotonic()
        if not should_play_audio(item.policy.priority, self._audio_priority, now, self._audio_until):
            view.status_var.set("Another alert's audio is playing. Read the instructions below.")
            return
        try:
            duration = self.play_sound(item.alert)
            # A playback adapter may report the validated sound's duration.
            duration = float(duration) if isinstance(duration, (int, float)) else 30.0
            if not math.isfinite(duration):
                duration = 30.0
            self._audio_priority = item.policy.priority
            self._audio_until = now + max(1.0, min(duration, 300.0))
            self._audio_record = item.record_id
        except Exception:
            LOG.exception("Alert audio failed")
            view.status_var.set("Alert sound could not play. Read the instructions below.")

    def _drain(self) -> None:
        if self._closed or self._draining:
            return
        self._draining = True
        try:
            now = datetime.now(timezone.utc)
            for item in list(self.pending.values()):
                if item.policy.expires and item.policy.expires <= now:
                    self._finish(item.record_id, "expired")
            ready = ordered_ready(self.pending.values(), now)
            if self.can_display is not None:
                for item in ready:
                    if not self.can_display(item.record_id):
                        self.discard(item.record_id)
                ready = [item for item in ready if item.record_id in self.pending]
            if not ready:
                return
            candidate = ready[0]
            critical = [view for view in self.visible.values() if view.item.policy.persistent]
            if candidate.policy.persistent:
                if critical and candidate.policy.priority >= critical[0].item.policy.priority:
                    return
                # Suspend visible items without treating them as acknowledged.
                for key, view in list(self.visible.items()):
                    self.visible.pop(key)
                    self.pending[key] = view.item
                    view.close()
                self._stop_audio()
                ready = [candidate]
            elif critical:
                return
            for item in ready[:max(0, MAX_VISIBLE - len(self.visible))]:
                if item.policy.persistent and self.visible:
                    break
                self.pending.pop(item.record_id, None)
                view = None
                try:
                    view = _AlertView(self, item, len(self.visible))
                    self.visible[item.record_id] = view
                    self.on_displayed(item.record_id)
                    item.status = "displayed"
                    self._play_audio(item, view)
                    self._changed()
                except Exception as exc:
                    self.visible.pop(item.record_id, None)
                    if view:
                        view.close()
                    LOG.exception("Unable to present alert %s", item.record_id)
                    self.on_failed(item.record_id, text(exc))
                    # Durable inbox remains responsible for retry after failure.
        finally:
            self._draining = False

    def _tick(self) -> None:
        if self._closed:
            return
        try:
            now = datetime.now(timezone.utc)
            monotonic_now = time.monotonic()
            if monotonic_now - self._last_guard_check >= 1.0:
                self._discard_inactive()
                self._last_guard_check = monotonic_now
            for key, view in list(self.visible.items()):
                view.poll_image()
                policy = view.item.policy
                if policy.expires and policy.expires <= now:
                    self._finish(key, "expired")
                elif policy.timeout_seconds and monotonic_now - view.shown_at >= policy.timeout_seconds:
                    self._finish(key, "dismissed")
            self._drain()
            for view in self.visible.values():
                view.queue_var.set(f"{self.pending_count} additional notification(s) waiting" if self.pending_count else "")
        except Exception:
            LOG.exception("Alert presentation tick failed")
        finally:
            if not self._closed:
                self._after_id = self.root.after(250, self._tick)

    def show_history(self, records: Iterable[Mapping[str, Any]] | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        if self._history_window is not None and self._history_window.winfo_exists():
            self._history_window.destroy()
        window = tk.Toplevel(self.root)
        self._history_window = window
        window.title("SLS Mass Notify — Notification history")
        fit_window(window, (850, 600))
        window.minsize(420, 300)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        search = tk.StringVar()
        ttk.Label(frame, text="Search notification history").pack(anchor="w")
        entry = ttk.Entry(frame, textvariable=search)
        entry.pack(fill="x", pady=(4, 8))
        rows = tk.Listbox(frame, height=9, exportselection=False)
        rows.pack(fill="both", expand=True)
        details = ScrolledText(frame, wrap="word", height=12, font="TkTextFont")
        details.pack(fill="both", expand=True, pady=(8, 0))
        ttk.Label(frame, text="Responses shown here are recorded on this device.").pack(anchor="w", pady=(8, 0))
        if records is None:
            items = list(self.history) + list(self.pending.values()) + [view.item for view in self.visible.values()]
            source = [{"record_id": item.record_id, "title": field(item.alert, "title"),
                       "body": field(item.alert, "body") or field(item.alert, "description"),
                       "status": item.status, "timestamp": item.received_at,
                       "area": field(item.alert, "area")} for item in reversed(items)]
        else:
            source = list(records)[:MAX_HISTORY]
        filtered: list[Mapping[str, Any]] = []

        def select(_event: Any = None) -> None:
            selection = rows.curselection()
            if not selection:
                return
            item = filtered[selection[0]]
            payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else item
            value = "\n\n".join(part for part in (
                text(payload.get("title")), text(item.get("status") or item.get("state")),
                text(payload.get("timestamp") or item.get("received_at")), text(payload.get("area")),
                text(payload.get("body") or payload.get("message") or payload.get("description"))) if part)
            details.configure(state="normal")
            details.delete("1.0", "end")
            details.insert("1.0", value)
            details.configure(state="disabled")

        def refresh(*_args: Any) -> None:
            needle = search.get().casefold()
            filtered[:] = [item for item in source if needle in text(item).casefold()]
            rows.delete(0, "end")
            for item in filtered:
                payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else item
                rows.insert("end", f"[{item.get('status', item.get('state', 'unknown'))}] {payload.get('title', 'Notification')}")
            if filtered:
                rows.selection_set(0)
                select()
            else:
                details.configure(state="normal")
                details.delete("1.0", "end")
                details.configure(state="disabled")

        rows.bind("<<ListboxSelect>>", select)
        search.trace_add("write", refresh)
        refresh()
        entry.focus_set()

    def shutdown(self) -> None:
        self._closed = True
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                LOG.debug("Presentation timer already stopped", exc_info=True)
        self._stop_audio()
        for view in list(self.visible.values()):
            view.close()
        self.visible.clear()
        self.pending.clear()
        if self._history_window is not None:
            try:
                self._history_window.destroy()
            except Exception:
                LOG.debug("History window already closed", exc_info=True)


class _AlertView:
    def __init__(self, presenter: AlertPresenter, item: PendingAlert, offset: int) -> None:
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self.presenter, self.item = presenter, item
        self.shown_at = time.monotonic()
        self.closed = False
        self._image_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._image_ref: Any = None
        self.window = tk.Toplevel(presenter.root)
        self.window.withdraw()
        try:
            self.window.title(text(field(item.alert, "title")) or "SLS Mass Notify")
            self.window.resizable(True, True)
            self.visual_announcement = (text(field(item.alert, "kind")).lower() == "announcement"
                                        and bool(field(item.alert, "image_url")))
            self._image_source: Any = None
            self._rendered_size: tuple[int, int] | None = None
            frame = ttk.Frame(self.window, padding=16)
            frame.pack(fill="both", expand=True)
            # Reserve the footer before allocating any space to message content.
            footer = ttk.Frame(frame)
            footer.pack(side="bottom", fill="x")
            controls = ttk.Frame(footer)
            controls.pack(side="bottom", fill="x", pady=(12, 0))
            ttk.Button(controls, text="Details", command=self.show_details).pack(side="left")
            ttk.Button(controls, text="History", command=presenter.show_history).pack(side="left", padx=8)
            self.read_label = ("I have read this announcement" if text(field(item.alert, "kind")).lower() == "announcement"
                               else "I have read this alert")
            self.ack = ttk.Button(controls, text=self.read_label if item.policy.persistent else "Dismiss",
                                  command=lambda: presenter._respond(item.record_id,
                                      "acknowledged" if item.policy.persistent else "dismissed"))
            self.ack.pack(side="right")
            self.status_var = tk.StringVar()
            self.queue_var = tk.StringVar()
            self.status_label = ttk.Label(footer, textvariable=self.status_var, wraplength=690)
            self.status_label.pack(fill="x", pady=(8, 0))
            if item.policy.timing_warning:
                ttk.Label(footer, text=item.policy.timing_warning, wraplength=690).pack(fill="x", pady=(4, 0))
            ttk.Label(footer, textvariable=self.queue_var, wraplength=690).pack(fill="x")
            header = ttk.Frame(frame)
            header.pack(fill="x")
            marker_label = ttk.Label(header, text=test_marker(item.alert), font=("Segoe UI", 12, "bold"), wraplength=690)
            # The designed announcement already includes its heading and message.
            # Keep explicit drill identification visible; other metadata is in Details.
            if not self.visual_announcement or explicit_test_mode(item.alert) is True:
                marker_label.pack(fill="x", pady=(0, 8))
            title = ttk.Label(header, text=text(field(item.alert, "title")) or "Notification",
                              font=("Segoe UI", 19, "bold"), wraplength=690)
            metadata = [text(field(item.alert, key)) for key in ("priority_label", "severity", "area") if field(item.alert, key)]
            metadata_label = ttk.Label(header, text=" | ".join(dict.fromkeys(metadata)), wraplength=690)
            if not self.visual_announcement:
                title.pack(fill="x", pady=(0, 8))
                metadata_label.pack(fill="x", pady=(0, 8))
            content = ttk.Frame(frame)
            content.pack(fill="both", expand=True)
            # Keep the viewport's allocation stable while asynchronous content
            # changes. Clam can otherwise leave the replacement canvas unmapped.
            content.grid_propagate(False)
            content.columnconfigure(0, weight=1)
            content.rowconfigure(0, weight=1)
            self.instructions = ScrolledText(content, wrap="word", height=1, width=1,
                                            font=("Segoe UI", 13), takefocus=True)
            self.instructions.grid(row=0, column=0, sticky="nsew")
            body = text(field(item.alert, "body")) or text(field(item.alert, "description"))
            self.instructions.insert("1.0", body or "No written instructions were supplied with this notification.")
            self.instructions.configure(state="disabled")
            background = text(field(item.alert, "background_color"))
            if (len(background) != 7 or not background.startswith("#")
                    or any(char not in "0123456789abcdefABCDEF" for char in background[1:])):
                background = "#1f2937"
            if self.visual_announcement:
                red, green, blue = (int(background[i:i+2], 16) for i in (1, 3, 5))
                foreground = "#ffffff" if .2126*red + .7152*green + .0722*blue < 140 else "#111111"
                self.instructions.configure(background=background, foreground=foreground)
            self.image_canvas = tk.Canvas(content, width=1, height=160, highlightthickness=0,
                                          background=background)
            self.image_canvas.bind("<Configure>", lambda _event: self._render_image())
            if not self.visual_announcement:
                content.bind("<Configure>", lambda event: self.image_canvas.configure(
                    height=min(160, max(1, event.height // 3))))
            self.window.protocol("WM_DELETE_WINDOW", self.close_requested)
            self.window.bind("<Escape>", lambda _event: self.close_requested())
            self.window.bind("<Control-d>", lambda _event: self.show_details())
            def resize(event: Any) -> None:
                if event.widget is self.window:
                    wrap = max(200, event.width - 56)
                    for label in (title, marker_label, metadata_label, self.status_label):
                        label.configure(wraplength=wrap)
            self.window.bind("<Configure>", resize)
            fit_window(self.window, (760, 640), minimum=(420, 320))
            self.window.attributes("-topmost", item.policy.persistent)
            self.window.deiconify()
            self.window.lift()
            self.window.update_idletasks()
            if item.policy.persistent:
                self.instructions.focus_set()
            if presenter.image_loader and field(item.alert, "image_url"):
                if presenter._image_slots.acquire(blocking=False):
                    self.status_var.set("Loading announcement design..." if self.visual_announcement else "Loading image...")
                    threading.Thread(target=self._load_image, name="alert-image", daemon=True).start()
                else:
                    self.status_var.set("Image unavailable. Showing the written announcement." if self.visual_announcement
                                        else "Image unavailable. Written instructions are shown above.")
        except Exception:
            self.window.destroy()
            raise

    def close_requested(self) -> None:
        if self.item.policy.persistent:
            self.status_var.set(f"This notification stays visible until you select '{self.read_label}' or it expires.")
            self.ack.focus_set()
        else:
            self.presenter._respond(self.item.record_id, "dismissed")

    def _load_image(self) -> None:
        try:
            from PIL import Image, ImageOps
            data = self.presenter.image_loader(self.item.alert)
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise ValueError("Image missing or too large")
            with Image.open(io.BytesIO(data)) as source:
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ValueError("Image dimensions exceed the display limit")
                source.load()
                resized = ImageOps.exif_transpose(source)
                resized.thumbnail((1920, 1080))
                result = resized.convert("RGB")
            self._image_queue.put_nowait(result)
        except Exception:
            LOG.info("Optional alert image could not load", exc_info=True)
            self._image_queue.put_nowait(None)
        finally:
            self.presenter._image_slots.release()

    def _render_image(self) -> None:
        try:
            self._draw_image()
        except Exception:
            LOG.exception("Unable to render alert image")
            self._image_source = None
            self._image_ref = None
            self._rendered_size = None
            if not self.closed:
                self.instructions.grid()
                self.image_canvas.grid_remove()
                self.status_var.set("Image unavailable. Written instructions are shown above.")

    def _draw_image(self) -> None:
        if self.closed or self._image_source is None:
            return
        from PIL import Image, ImageOps, ImageTk
        size = (max(1, self.image_canvas.winfo_width()), max(1, self.image_canvas.winfo_height()))
        if min(size) <= 1:
            return
        if size != self._rendered_size:
            result = ImageOps.contain(self._image_source, size, Image.Resampling.LANCZOS)
            self._image_ref = ImageTk.PhotoImage(result, master=self.window)
            self.image_canvas.delete("announcement")
            self.image_canvas.create_image(size[0]//2, size[1]//2, image=self._image_ref,
                                           anchor="center", tags="announcement")
            self._rendered_size = size
        if self.image_canvas.winfo_ismapped():
            if self.visual_announcement:
                self.instructions.grid_remove()
            self.status_var.set("")

    def poll_image(self) -> None:
        if self.closed:
            return
        try:
            result = self._image_queue.get_nowait()
        except queue.Empty:
            if self._image_source is not None:
                self._render_image()
            return
        if result is None:
            self.status_var.set("Image unavailable. Showing the written announcement." if self.visual_announcement
                                else "Image unavailable. Written instructions remain available above.")
            return
        try:
            self._image_source = result
            if not self.visual_announcement:
                self.image_canvas.configure(height=min(160, max(1, self.image_canvas.master.winfo_height() // 3)))
            self.image_canvas.grid(row=0 if self.visual_announcement else 1, column=0, sticky="nsew")
            self.window.update_idletasks()
            self._render_image()
        except Exception:
            LOG.exception("Unable to render alert image")
            self._image_source = None
            self.instructions.grid()
            self.image_canvas.grid_remove()
            self.status_var.set("Image unavailable. Written instructions are shown above.")

    def show_details(self) -> None:
        import tkinter as tk
        from tkinter.scrolledtext import ScrolledText
        window = tk.Toplevel(self.window)
        window.title("Notification details")
        fit_window(window, (650, 450))
        details = ScrolledText(window, wrap="word", font="TkTextFont")
        details.pack(fill="both", expand=True, padx=12, pady=12)
        lines = [test_marker(self.item.alert), "", "Acknowledgment is recorded on this device."]
        for name in ("title", "event", "severity", "priority", "area", "timestamp", "effective", "expires",
                     "display_timeout_seconds", "display_expires_at", "incident_id", "revision",
                     "source_endpoint", "body", "description"):
            value = field(self.item.alert, name)
            if value:
                lines.extend(("", f"{name.replace('_', ' ').capitalize()}: {value}"))
        details.insert("1.0", "\n".join(lines))
        details.configure(state="disabled")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.window.destroy()
