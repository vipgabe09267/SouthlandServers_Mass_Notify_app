import hashlib
import io
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import sls_updates as updates

OWNER, REPO = "owner", "repo"
URL = "https://github.com/owner/repo/releases/download/v1.2.0/SLS_Mass_Notify_Installer.zip"


def release(tag="v1.2.0", *, prerelease=False, digest=None, size=3, asset_name="SLS_Mass_Notify_Installer.zip"):
    return {"id": 10, "tag_name": tag, "prerelease": prerelease, "assets": [
        {"name": asset_name, "size": size,
         "digest": digest if digest is not None else "sha256:" + hashlib.sha256(b"abc").hexdigest(),
         "browser_download_url": URL.rsplit("/", 1)[0] + "/" + asset_name}]}


class VersionTests(unittest.TestCase):
    def test_exe_only_release_and_preference_over_zip(self):
        exe = release(asset_name="SLS_Mass_Notify_Installer.exe")
        picked = updates.select_release([exe], "1.0.0", owner=OWNER, repo=REPO)
        self.assertEqual("SLS_Mass_Notify_Installer.exe", picked["asset_name"])
        mixed = release()
        mixed["assets"].extend(exe["assets"])
        self.assertEqual(picked, updates.select_release([mixed], "1.0.0", owner=OWNER, repo=REPO))

    def test_beta_sequence_and_stable_order(self):
        self.assertEqual(1, updates.compare_versions("1.0.8-beta.2", "1.0.8-beta.1"))
        self.assertEqual(1, updates.compare_versions("1.0.8", "1.0.8-rc.99"))
        self.assertEqual(0, updates.compare_versions("1.0.8-Beta", "1.0.8b0"))

    def test_invalid_versions_cannot_upgrade(self):
        for value in (None, "Release 9.0.0", "1.0.0evil", "1.0.0+malicious", "1!1.0.0", "1.0.0\n"):
            self.assertIsNone(updates.parse_version(value))
            self.assertIsNone(updates.compare_versions(value, "1.0.0"))

    def test_highest_release_not_api_order(self):
        picked = updates.select_release([release("1.1.0"), release("1.9.0"), release("1.2.0")], "1.0.0", owner=OWNER, repo=REPO)
        self.assertEqual("1.9.0", picked["tag_name"])

    def test_channel_downgrade_and_metadata(self):
        items = [release("1.2.0-beta.1"), release("1.1.0", prerelease=True), release("1.0.0")]
        self.assertIsNone(updates.select_release(items, "1.0.0", owner=OWNER, repo=REPO))
        self.assertEqual("1.2.0-beta.1", updates.select_release(items, "1.0.0", channel="beta", owner=OWNER, repo=REPO)["tag_name"])
        for bad in (release(digest=""), release(size=0), release(size=True), release("not-version")):
            self.assertIsNone(updates.select_release([bad], "1.0.0", owner=OWNER, repo=REPO))


class DownloadTests(unittest.TestCase):
    def test_exe_download_preserves_extension_without_launching(self):
        metadata = updates.select_release([release(asset_name="SLS_Mass_Notify_Installer.exe")],
                                          "1.0.0", owner=OWNER, repo=REPO)
        response = io.BytesIO(b"abc")
        response.geturl = lambda: metadata["download_url"]
        opener = mock.Mock()
        opener.open.return_value = response
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(updates.urllib.request, "build_opener", return_value=opener), \
             mock.patch.object(updates.subprocess, "Popen") as launch:
            path = updates.download_release(metadata, Path(temporary), owner=OWNER, repo=REPO)
            self.assertEqual(".exe", path.suffix)
            self.assertEqual(b"abc", path.read_bytes())
            launch.assert_not_called()

    def test_completed_watchdog_cannot_later_abort_socket(self):
        sock = mock.Mock()
        watchdog = updates.DownloadDeadline(sock, 30, "timeout")
        with watchdog:
            pass
        watchdog._expire()
        sock.shutdown.assert_not_called()

    def test_partial_response_headers_have_absolute_deadline(self):
        client, peer = socket.socketpair()
        client.settimeout(2)
        stop = threading.Event()
        def trickle():
            try:
                peer.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                while not stop.wait(0.015):
                    peer.sendall(b"x")
            except OSError:
                pass
        writer = threading.Thread(target=trickle, daemon=True)
        writer.start()
        connection = updates.UpdateHTTPSConnection("github.com", timeout=2)
        connection.sock = client
        connection._HTTPConnection__state = updates.http.client._CS_REQ_SENT
        connection._method = "GET"
        started = time.monotonic()
        try:
            with mock.patch.object(updates, "HTTP_HEADER_DEADLINE_SECONDS", 0.1):
                with self.assertRaisesRegex(updates.UpdateError, "headers.*deadline"):
                    connection.getresponse()
            self.assertLess(time.monotonic() - started, 1)
        finally:
            stop.set()
            connection.close()
            peer.close()
            writer.join(timeout=1)

    def test_trickled_body_deadline_removes_partial_download(self):
        client, peer = socket.socketpair()
        client.settimeout(2)
        stop = threading.Event()
        class Response:
            def __init__(self):
                self.fp = client.makefile("rb")
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                self.fp.close()
            def read(self, size):
                return self.fp.read(size)
            def geturl(self):
                return URL
        response = Response()
        opener = mock.Mock()
        opener.open.return_value = response
        def trickle():
            try:
                while not stop.wait(0.015):
                    peer.sendall(b"x")
            except OSError:
                pass
        writer = threading.Thread(target=trickle, daemon=True)
        writer.start()
        metadata = updates.select_release([release(size=10000)], "1.0.0", owner=OWNER, repo=REPO)
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                with mock.patch.object(updates, "DOWNLOAD_DEADLINE_SECONDS", 0.1), mock.patch.object(updates.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaisesRegex(updates.UpdateError, "download.*deadline"):
                        updates.download_release(metadata, Path(temporary), owner=OWNER, repo=REPO)
                self.assertEqual([], list(Path(temporary).iterdir()))
            self.assertLess(time.monotonic() - started, 1)
        finally:
            stop.set()
            response.fp.close()
            client.close()
            peer.close()
            writer.join(timeout=1)

    def test_url_boundary(self):
        self.assertTrue(updates.trusted_download_url(URL, OWNER, REPO))
        for bad in (URL.replace("https:", "http:"), URL.replace("github.com", "github.com.attacker.test"),
                    URL.replace("github.com", "user@github.com"), URL.replace("github.com", "github.com:444"),
                    URL + "#fragment", URL.replace("/v1.2.0/", "/../"), URL + "\n"):
            self.assertFalse(updates.trusted_download_url(bad, OWNER, REPO), bad)
        cdn = "https://release-assets.githubusercontent.com/asset?token=xyz"
        self.assertFalse(updates.trusted_download_url(cdn, OWNER, REPO))
        self.assertTrue(updates.trusted_download_url(cdn, OWNER, REPO, allow_cdn=True))

    def test_redirect_hop_rejected(self):
        handler = updates.ReleaseRedirectHandler(OWNER, REPO)
        request = urllib.request.Request(URL)
        with self.assertRaises(updates.UpdateError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://attacker.test/installer")

    def test_download_requires_exact_size_and_digest_and_cleans_partial(self):
        normalized = updates.select_release([release()], "1.0.0", owner=OWNER, repo=REPO)
        with tempfile.TemporaryDirectory() as temporary:
            for payload in (b"ab", b"abcd", b"xyz"):
                response = io.BytesIO(payload)
                response.geturl = lambda: URL
                opener = mock.Mock()
                opener.open.return_value = response
                with mock.patch.object(updates.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaises(updates.UpdateError):
                        updates.download_release(normalized, Path(temporary), owner=OWNER, repo=REPO)
                self.assertEqual([], list(Path(temporary).iterdir()))
            response = io.BytesIO(b"abc")
            response.geturl = lambda: URL
            opener.open.return_value = response
            with mock.patch.object(updates.urllib.request, "build_opener", return_value=opener):
                path = updates.download_release(normalized, Path(temporary), owner=OWNER, repo=REPO)
            self.assertEqual(b"abc", path.read_bytes())

    def test_signature_pin_required_and_launch_disabled(self):
        with self.assertRaises(updates.UpdateError):
            updates.verify_authenticode(Path("anything.exe"), [])
        with mock.patch.object(updates.subprocess, "Popen") as launch:
            with self.assertRaises(updates.UpdateError):
                updates.launch_update_installer(Path("untrusted.exe"))
            launch.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows OS trust verification")
    def test_unsigned_file_rejected_by_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unsigned.exe"
            path.write_bytes(b"not a signed executable")
            with self.assertRaises(updates.UpdateError):
                updates.verify_authenticode(path, ["A" * 64])

    def test_cleanup_preserves_unowned_and_kept_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            owned, kept, unrelated = (directory / name for name in ("SLS_Update_1.exe", "SLS_Update_2.zip", "notes.txt"))
            for path in (owned, kept, unrelated):
                path.write_bytes(b"123")
            self.assertEqual(1, updates.cleanup_downloads(directory, max_bytes=0, keep=(kept,)))
            self.assertFalse(owned.exists())
            self.assertTrue(kept.exists())
            self.assertTrue(unrelated.exists())


class PolicyTests(unittest.TestCase):
    def test_incident_hold_and_rollout(self):
        now = datetime(2026, 9, 20, 2, tzinfo=timezone.utc)
        self.assertFalse(updates.maintenance_allowed({}, active_incident=True, device_id="device", now=now))
        self.assertFalse(updates.maintenance_allowed({"hold": True}, active_incident=False, device_id="device", now=now))
        self.assertFalse(updates.maintenance_allowed({"rollout_percent": 0}, active_incident=False, device_id="device", now=now))
        self.assertTrue(updates.maintenance_allowed({"rollout_percent": 100}, active_incident=False, device_id="device", now=now))

    def test_overnight_and_invalid_windows(self):
        for hour, expected in ((23, True), (1, True), (12, False), (3, False)):
            self.assertEqual(expected, updates.maintenance_allowed({"start": "22:00", "end": "03:00"}, active_incident=False,
                             device_id="a", now=datetime(2026, 9, 20, hour, tzinfo=timezone.utc)))
        self.assertFalse(updates.maintenance_allowed({"start": "25:00"}, active_incident=False, device_id="a"))

    def test_restart_requires_version_and_health(self):
        state = {"pending_version": "1.2.0", "status": "staged"}
        self.assertEqual("verification_pending", updates.update_state_after_restart(state, "1.1.0", healthy=True)["status"])
        self.assertEqual("verification_pending", updates.update_state_after_restart(state, "1.2.0", healthy=False)["status"])
        self.assertEqual("healthy", updates.update_state_after_restart(state, "1.2.0", healthy=True)["status"])


if __name__ == "__main__":
    unittest.main()
