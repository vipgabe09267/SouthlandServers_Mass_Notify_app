import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import sls_ipc


class LocalControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.stop = threading.Event()
        self.commands = []
        self.scope = mock.patch.object(sls_ipc, "session_scope", return_value="test-session")
        self.scope.start()
        self.server = threading.Thread(target=sls_ipc.serve,
            args=(self.directory, self.stop, self.commands.append, lambda value: "protected:" + value), daemon=True)
        self.server.start()
        self.metadata = self.directory / "ipc-test-session.json"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if sls_ipc.send(self.directory, "PING", self.unprotect):
                break
            time.sleep(0.01)
        else:
            self.fail("Local control listener did not become ready")

    @staticmethod
    def unprotect(value):
        return value.removeprefix("protected:")

    def tearDown(self):
        self.stop.set()
        self.server.join(timeout=2)
        self.scope.stop()
        self.assertFalse(self.server.is_alive())
        self.temporary.cleanup()

    def test_valid_token_required_and_action_throttled(self):
        info = json.loads(self.metadata.read_text())
        with socket.create_connection(("127.0.0.1", info["port"]), timeout=1) as client:
            client.sendall(b'{"token":"invalid","command":"SHUTDOWN"}\n')
            self.assertEqual(b"", client.recv(16))
        self.assertEqual([], self.commands)
        self.assertTrue(sls_ipc.send(self.directory, "SHOW_SETTINGS", self.unprotect))
        self.assertFalse(sls_ipc.send(self.directory, "SHUTDOWN", self.unprotect))
        self.assertEqual(["SHOW_SETTINGS"], self.commands)
        self.assertTrue(sls_ipc.send(self.directory, "PING", self.unprotect))

    def test_slow_client_cannot_hold_listener_indefinitely(self):
        info = json.loads(self.metadata.read_text())
        with socket.create_connection(("127.0.0.1", info["port"]), timeout=1) as client:
            client.settimeout(1)
            started = time.monotonic()
            # Dribble faster than the per-read timeout. An absolute deadline must
            # still disconnect this client well before its endless input ends.
            for _ in range(40):
                try:
                    client.sendall(b" ")
                except OSError:
                    break
                time.sleep(0.025)
            self.assertLess(time.monotonic() - started, 1.25)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not sls_ipc.send(self.directory, "PING", self.unprotect):
            time.sleep(0.01)
        self.assertTrue(sls_ipc.send(self.directory, "PING", self.unprotect))
        self.assertEqual([], self.commands)

    def test_oversized_metadata_and_invalid_command_do_not_connect(self):
        self.metadata.write_text("x" * 4097)
        with mock.patch.object(sls_ipc.socket, "create_connection") as connection:
            self.assertFalse(sls_ipc.send(self.directory, "PING", self.unprotect))
            self.assertFalse(sls_ipc.send(self.directory, "EXECUTE", self.unprotect))
            connection.assert_not_called()

    def test_oversized_request_does_not_block_following_ping(self):
        info = json.loads(self.metadata.read_text())
        with socket.create_connection(("127.0.0.1", info["port"]), timeout=1) as client:
            client.sendall(b"{" + b"x" * 1024)
            try:
                client.recv(16)
            except ConnectionResetError:
                pass
        self.assertTrue(sls_ipc.send(self.directory, "PING", self.unprotect))
        self.assertEqual([], self.commands)


if __name__ == "__main__":
    unittest.main()
