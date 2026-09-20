"""Fresh-only fallback delivery, using isolated storage and simulated snapshots."""
import io
import json
import threading
from datetime import datetime, timezone
from unittest import mock
import sls_mass_notify as client
from sls_protocol import LiveSnapshotBaseline
from test_sls_client_reliability import IsolatedClientTest, notification, authenticated


def notice(name, when, **extra):
    return notification(name, kind="announcement", created_at=datetime.fromtimestamp(when, timezone.utc).isoformat(), **extra)


class PollingTests(IsolatedClientTest):
    def test_working_fallback_is_remembered_across_restart(self):
        now = client.time.time()
        self.run_snapshots([([], now)])
        saved = json.loads(client.CONFIG_PATH.read_text())
        profile = client.normalize_endpoint(saved["endpoints"][0], 0)
        self.assertGreater(profile["live_polling_until"], now)
        worker = client.EndpointTransportWorker(self.instance, 0, profile)
        with mock.patch.object(worker, "_run_live_polling") as polling, mock.patch.object(worker, "_run_live") as stream:
            worker.run()
        polling.assert_called_once()
        stream.assert_not_called()

    def test_expired_transport_hint_does_not_disable_sse_forever(self):
        worker = self.worker()
        worker.endpoint_cfg["live_polling_until"] = client.time.time()-1
        with mock.patch.object(worker, "_run_live_polling") as polling, mock.patch.object(worker, "_run_live") as stream:
            worker.run()
        stream.assert_called_once()
        polling.assert_not_called()

    def run_snapshots(self, snapshots):
        worker = self.worker()
        calls = iter(snapshots)
        def fetch(*args, **kwargs):
            item = next(calls)
            if isinstance(item, Exception):
                raise item
            records, server = item
            return {"ok": True, "events": records}, "", server
        waits = [False] * (len(snapshots)-1) + [True]
        with mock.patch.object(client, "fetch_endpoint", side_effect=fetch), \
             mock.patch.object(worker.stop_event, "wait", side_effect=waits):
            worker._run_live_polling()
        return worker

    def test_baseline_skips_old_but_new_publication_reaches_presenter_and_receipt(self):
        now = client.time.time()
        old, fresh = notice("old", now-10), notice("fresh", now+1)
        self.run_snapshots([([old], now), ([old, fresh], now+5)])
        self.instance.pump_inbox()
        self.assertEqual([x["event_id"] for x in self.instance.inbox.history()], ["fresh"])
        self.assertEqual(self.instance.presenter.submit.call_args.args[1].event_id, "fresh")
        self.assertEqual(self.instance.inbox.pending_receipts(self.namespace)[0]["event_id"], "fresh")

    def test_outage_resets_baseline_and_never_replays_missed_publications(self):
        now = client.time.time()
        old, missed, fresh = notice("old", now-20), notice("missed", now+5), notice("fresh", now+11)
        self.run_snapshots([([old], now), OSError("offline"), ([old, missed], now+10), ([old, missed, fresh], now+15)])
        self.assertEqual([x["event_id"] for x in self.instance.inbox.history()], ["fresh"])

    def test_backfilled_old_records_wrong_targets_and_duplicates_are_not_delivered(self):
        now = client.time.time()
        old = notice("backfill", now-600)
        fresh = notice("fresh", now+1)
        wrong = notice("wrong", now+1, desktop_all=False, desktop_recipients=["other"])
        self.run_snapshots([([], now), ([old, fresh, wrong], now+5), ([old, fresh, wrong], now+10)])
        self.assertEqual([x["event_id"] for x in self.instance.inbox.history()], ["fresh"])

    def test_resume_gap_and_server_clock_reversal_reset_baseline(self):
        baseline = LiveSnapshotBaseline()
        self.assertEqual(baseline.observe([], 100, 1000), [])
        self.assertEqual(baseline.observe([notice("missed", 110)], 120, 1020), [])
        self.assertEqual(baseline.observe([notice("clock", 90)], 90, 1025), [])

    def test_handshake_and_padding_alone_do_not_establish_stream_health(self):
        stream = io.BytesIO(authenticated() + b":" + b" "*8192 + b"\n")
        events = list(client.iter_sse_events(stream, emit_heartbeats=True))
        self.assertEqual([e.name for e in events], ["authenticated"])
        events = list(client.iter_sse_events(io.BytesIO(b": keepalive 123\n\n"), emit_heartbeats=True))
        self.assertEqual([e.name for e in events], ["_heartbeat"])

    def test_stale_monitor_test_does_not_open_an_extra_server_stream(self):
        worker = self.worker()
        self.instance.connection_test_event = threading.Event()
        worker.last_activity = client.time.monotonic()
        worker.last_stream_signal = 0
        self.instance.transport_statuses[0] = ("AUTHENTICATED", "connected")
        with mock.patch.object(worker.thread, "is_alive", return_value=True), \
             mock.patch.object(worker.stream_ready, "wait", return_value=False), \
             mock.patch.object(client, "open_sse_response") as opened:
            self.instance.test_now(lambda _: None)
            while True:
                kind, value = self.instance.ui_queue.get(timeout=2)
                if kind == "test_result":
                    self.assertIn("no recent heartbeat", value[1])
                    break
            opened.assert_not_called()

    def test_stream_capacity_backoff_precedes_polling(self):
        worker = self.worker()
        order = []
        with mock.patch.object(client, "open_sse_response", side_effect=client.RateLimitedError("capacity", 15)), \
             mock.patch.object(worker.stop_event, "wait", side_effect=lambda delay: order.append(delay) or False), \
             mock.patch.object(worker, "_run_live_polling", side_effect=lambda: order.append("poll")):
            worker._run_live()
        self.assertEqual(order, [15, "poll"])
