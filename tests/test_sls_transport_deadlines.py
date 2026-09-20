"""Local-only regression tests for stalled reads and revoked session races.

Socket pairs emulate partial HTTP/SSE responses. No DNS, PBX, credentials,
registry, GUI, sound, or production database is accessed.
"""
import io
import json
import socket
import ssl
import threading
import time
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

import sls_mass_notify as client


def wire(name, payload):
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()


AUTH = wire("authenticated", {"ok": True, "transport": "live_sse", "protocol_version": 2})
NOTICE = {"id": "local-test-1", "kind": "announcement", "title": "Local fixture", "desktop_all": True}


class SocketResponse:
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, sock):
        self.fp = sock.makefile("rb")

    def readline(self, size):
        return self.fp.readline(size)

    def read(self, size):
        return self.fp.read(size)

    def close(self):
        self.fp.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class TransportSafetyTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(client, "log"))
        self.profile = {"endpoint": "https://example.invalid", "username": "local-test", "password": "fixture"}
        self.app = SimpleNamespace(
            accept_alert=mock.Mock(return_value=True), get_endpoint_state=mock.Mock(return_value=""),
            set_transport_status=mock.Mock(), record_fault=mock.Mock(), clear_fault=mock.Mock(),
        )
        self.worker = client.EndpointTransportWorker(self.app, 0, self.profile)
        self.addCleanup(self.worker.stop)

    def sockets(self):
        reader, writer = socket.socketpair()
        reader.settimeout(1)
        writer.settimeout(1)
        response = SocketResponse(reader)
        stopped = threading.Event()
        threads = []

        def cleanup():
            stopped.set()
            client.shutdown_socket(reader)
            client.shutdown_socket(writer)
            for thread in threads:
                thread.join(1)
            response.close()
            reader.close()
            writer.close()

        self.addCleanup(cleanup)

        def trickle(prefix=b"", part=b"x", duration=1.0, suffix=b""):
            def send():
                try:
                    writer.sendall(prefix)
                    end = time.monotonic() + duration
                    while time.monotonic() < end and not stopped.wait(0.01):
                        writer.sendall(part)
                    if not stopped.is_set():
                        writer.sendall(suffix)
                except OSError:
                    pass
            thread = threading.Thread(target=send, daemon=True)
            threads.append(thread)
            thread.start()

        return reader, writer, response, trickle

    def test_https_header_deadline_interrupts_partial_header_trickle(self):
        reader, _, _, trickle = self.sockets()
        connection = client.DeadlineHTTPSConnection("example.invalid", timeout=0.12)
        self.addCleanup(connection.close)
        # Inject an already-connected local socket: exercise real HTTP header
        # parsing without TLS certificates or any external network connection.
        connection.sock = reader
        connection.putrequest("GET", "/")
        connection.endheaders()
        trickle(b"HTTP/1.1 200 OK\r\nX-Partial: ")
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "headers.*deadline"):
            connection.getresponse()
        self.assertLess(time.monotonic() - started, 0.7)

    def test_custom_https_handler_retains_certificate_and_hostname_verification(self):
        opener = client.build_http_opener(same_origin=True)
        handler = next(item for item in opener.handlers if isinstance(item, client.DeadlineHTTPSHandler))
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(handler._context.check_hostname)

    def test_partial_authentication_line_cannot_extend_deadline(self):
        _, _, response, trickle = self.sockets()
        trickle(b"event: authenticated\ndata: {")
        with mock.patch.object(client, "SSE_TEST_TIMEOUT_SECONDS", 0.12):
            started = time.monotonic()
            with self.assertRaisesRegex(client.StreamProtocolError, "deadline"):
                self.worker._consume_stream(response)
        self.assertLess(time.monotonic() - started, 0.7)
        self.assertFalse(self.worker.authenticated_event.is_set())

    def test_complete_comments_cannot_extend_authentication_deadline(self):
        _, _, response, trickle = self.sockets()
        trickle(part=b": heartbeat\n")
        with mock.patch.object(client, "SSE_TEST_TIMEOUT_SECONDS", 0.12):
            with self.assertRaisesRegex(client.StreamProtocolError, "deadline"):
                self.worker._consume_stream(response)
        self.assertFalse(self.worker.authenticated_event.is_set())

    def test_partial_heartbeat_does_not_renew_authenticated_stream(self):
        _, _, response, trickle = self.sockets()
        trickle(AUTH + b": incomplete ")
        with mock.patch.object(client, "SSE_TEST_TIMEOUT_SECONDS", 0.08), \
             mock.patch.object(client, "SSE_READ_TIMEOUT_SECONDS", 0.18):
            started = time.monotonic()
            with self.assertRaisesRegex(client.StreamProtocolError, "deadline"):
                self.worker._consume_stream(response)
        elapsed = time.monotonic() - started
        self.assertGreater(elapsed, 0.12)  # Authentication transitions to heartbeat deadline.
        self.assertLess(elapsed, 0.7)
        self.assertFalse(self.worker.authenticated_event.is_set())

    def test_complete_heartbeats_renew_single_stream_watchdog(self):
        _, _, response, trickle = self.sockets()
        trickle(AUTH, part=b": heartbeat\n", duration=0.32, suffix=wire("reconnect", {}))
        with mock.patch.object(client, "SSE_TEST_TIMEOUT_SECONDS", 0.08), \
             mock.patch.object(client, "SSE_READ_TIMEOUT_SECONDS", 0.12):
            self.assertEqual(self.worker._consume_stream(response), (True, True))
        self.assertFalse(self.worker.authenticated_event.is_set())

    def test_body_read_deadline_interrupts_partial_json(self):
        reader, _, response, trickle = self.sockets()
        trickle(b'{"events": [')
        with self.assertRaisesRegex(TimeoutError, "deadline"):
            with client.SocketDeadline(reader, 0.12):
                response.read(1024)

    def test_cancelled_watchdog_never_shuts_down_released_socket(self):
        reader, writer, response, _ = self.sockets()
        watchdog = client.SocketDeadline(reader, 0.06)
        watchdog.cancel()
        watchdog.timer.join(0.2)
        writer.sendall(b"still usable\n")
        self.assertEqual(response.readline(128), b"still usable\n")
        self.assertFalse(watchdog.expired)
        self.assertIsNone(watchdog.sock)

    def test_line_renewals_do_not_allocate_additional_timer_threads(self):
        with mock.patch.object(client.threading, "Timer") as timer:
            watchdog = client.SocketDeadline(mock.Mock(), 60)
            for _ in range(1000):
                watchdog.renew(time.monotonic() + 60)
            self.assertEqual(timer.call_count, 1)
            watchdog.cancel()
            timer.return_value.cancel.assert_called_once()

    def test_error_response_is_closed_and_retry_after_is_preserved(self):
        body = io.BytesIO(b"unused")
        error = urllib.error.HTTPError("https://example.invalid", 429, "Limited", {"Retry-After": "7"}, body)
        with self.assertRaises(client.RateLimitedError) as caught:
            client.raise_http_status(error)
        self.assertTrue(body.closed)
        self.assertEqual(caught.exception.retry_after, 7)

    def test_receipt_worker_never_fetches_retained_notifications(self):
        self.worker.authenticated_event.set()
        def receipts(*_args, **_kwargs):
            self.worker.stop_event.set()
            return []
        self.app.inbox = SimpleNamespace(pending_receipts=receipts)
        with mock.patch.object(client, "fetch_endpoint") as fetch:
            self.worker._run_receipts()
        fetch.assert_not_called()
        self.app.accept_alert.assert_not_called()

    def test_inflight_receipt_is_not_confirmed_after_session_change(self):
        for transition in ("revoked", "new-session", "inactive", "stopped"):
            with self.subTest(transition=transition):
                worker = client.EndpointTransportWorker(self.app, 0, self.profile)
                worker.authenticated_event.set()
                entered, release = threading.Event(), threading.Event()
                self.app.inbox = mock.Mock()
                self.app.inbox.pending_receipts.return_value = [{"event_id": "notice", "receipt_id": "receipt"}]
                def send(*_args):
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError("Test receipt timed out")
                with mock.patch.object(client, "send_receipt_ack", side_effect=send):
                    thread = threading.Thread(target=worker._run_receipts, daemon=True)
                    thread.start()
                    self.assertTrue(entered.wait(1))
                    if transition == "revoked":
                        worker._invalidate_auth(revoked=True)
                    elif transition == "stopped":
                        worker.stop()
                    else:
                        worker._invalidate_auth()
                        if transition == "new-session":
                            worker.authenticated_event.set()
                    release.set()
                    worker.stop_event.set()
                    thread.join(2)
                self.assertFalse(thread.is_alive())
                self.app.inbox.receipt_sent.assert_not_called()
                worker.stop()

    def test_actual_revoked_event_blocks_subsequent_notifications_and_receipts(self):
        with self.assertRaises(client.UnauthorizedError):
            self.worker._consume_stream(io.BytesIO(AUTH + wire("revoked", {})))
        self.assertTrue(self.worker.revoked_event.is_set())
        self.assertFalse(self.worker.stop_event.is_set())
        self.assertFalse(self.worker.authenticated_event.is_set())
        self.assertFalse(self.worker._accept_payload(NOTICE, json.dumps(NOTICE)))
        with mock.patch.object(client, "fetch_endpoint") as fetch, \
             mock.patch.object(client, "send_receipt_ack") as send:
            self.worker.authenticated_event.set()  # A stale wake-up cannot bypass the fence.
            thread = threading.Thread(target=self.worker._run_receipts, daemon=True)
            thread.start()
            self.worker.stop()
            thread.join(1)
        fetch.assert_not_called()
        send.assert_not_called()

    def test_revocation_during_receipt_enumeration_prevents_new_ack(self):
        self.worker.authenticated_event.set()

        def receipts(*_args, **_kwargs):
            self.worker._invalidate_auth(revoked=True)
            self.worker.stop_event.set()  # Finish the test loop after this batch.
            return [{"event_id": "receipt-1", "receipt_id": 1}]

        self.app.inbox = SimpleNamespace(pending_receipts=receipts)
        with mock.patch.object(client, "send_receipt_ack") as send:
            self.worker._run_receipts()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
