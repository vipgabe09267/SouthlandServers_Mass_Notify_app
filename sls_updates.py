"""Release selection, constrained downloads and fail-closed Windows trust checks.

This module deliberately does not elevate downloaded programs. The onedir installer
must be deployed from an administrator-controlled directory by management tooling.
"""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import http.client
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from packaging.version import InvalidVersion, Version

DEFAULT_ASSETS = ("SLS_Mass_Notify_Installer.exe", "SLS_Mass_Notify_Installer.zip")
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
HTTP_HEADER_DEADLINE_SECONDS = 30
DOWNLOAD_DEADLINE_SECONDS = 600
CDN_HOSTS = frozenset({"release-assets.githubusercontent.com", "objects.githubusercontent.com"})


class UpdateError(RuntimeError):
    pass


class DownloadDeadline:
    """Interrupt a blocked socket read even when a peer continually trickles bytes."""
    def __init__(self, sock, seconds: float, message: str):
        self.sock, self.seconds, self.message = sock, seconds, message
        self.lock = threading.Lock()
        self.cancelled = False
        self.expired = False
        self.timer = None
        self.deadline = 0.0

    def _expire(self):
        with self.lock:
            if self.cancelled:
                return
            self.expired = True
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except (OSError, AttributeError):
                    pass

    def __enter__(self):
        self.deadline = time.monotonic() + self.seconds
        if self.sock is not None:
            self.timer = threading.Timer(max(0.001, self.seconds), self._expire)
            self.timer.daemon = True
            self.timer.name = "UpdateResponseDeadline"
            self.timer.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        with self.lock:
            self.cancelled = True
            if self.timer is not None:
                self.timer.cancel()
            self.sock = None
            expired = self.expired or time.monotonic() >= self.deadline
        if expired:
            raise UpdateError(self.message) from exc


class UpdateHTTPSConnection(http.client.HTTPSConnection):
    def getresponse(self):
        response = None
        try:
            with DownloadDeadline(self.sock, HTTP_HEADER_DEADLINE_SECONDS, "Update response headers exceeded their deadline"):
                response = super().getresponse()
            return response
        except BaseException:
            # A peer's EOF can make HTTPResponse.begin return a partial header
            # response just as the watchdog expires. Close that response too.
            if response is not None:
                response.close()
            self.close()
            raise


class UpdateHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(UpdateHTTPSConnection, request, context=self._context)


def _response_socket(response):
    for candidate in (getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
                      getattr(getattr(response, "fp", None), "_sock", None)):
        if candidate is not None:
            return candidate
    return None


def parse_version(value: object) -> Version | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    # Reject labels containing an incidental version, epochs, local builds and
    # post/dev releases. Accept legacy 1.0.8-Beta as well as beta.1 and beta.2.
    if not re.fullmatch(r"[vV]?\d+\.\d+\.\d+(?:[-_.]?(?:a|alpha|b|beta|rc)[-_.]?\d*)?", value, re.I):
        return None
    try:
        return Version(value)
    except InvalidVersion:
        return None


def compare_versions(candidate: object, current: object) -> int | None:
    left, right = parse_version(candidate), parse_version(current)
    return None if left is None or right is None else (left > right) - (left < right)


def trusted_download_url(value: object, owner: str, repo: str, *, allow_cdn: bool = False) -> bool:
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
            return False
        if parsed.hostname in CDN_HOSTS:
            return allow_cdn
        prefix = f"/{owner}/{repo}/releases/download/"
        path = urllib.parse.unquote(parsed.path)
        return (parsed.hostname == "github.com" and path.startswith(prefix)
                and not any(part in (".", "..") for part in path.split("/"))
                and "\\" not in path and len(path[len(prefix):].split("/")) == 2)
    except (ValueError, TypeError):
        return False


def select_release(releases: object, current_version: str, *, channel: str = "stable",
                   owner: str, repo: str, asset_names: tuple[str, ...] = DEFAULT_ASSETS) -> dict | None:
    """Choose the highest valid newer release; malformed metadata never upgrades."""
    current = parse_version(current_version)
    if current is None or channel not in {"stable", "beta"}:
        raise UpdateError("Invalid current version or update channel")
    if not isinstance(releases, list):
        raise UpdateError("GitHub did not return a release list")
    candidates: list[tuple[Version, dict]] = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        version = parse_version(release.get("tag_name"))
        if version is None or version <= current:
            continue
        if channel == "stable" and (release.get("prerelease") or version.is_prerelease):
            continue
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("name") not in asset_names:
                continue
            digest = asset.get("digest", "")
            size = asset.get("size")
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
                continue
            if type(size) is not int or not 0 < size <= MAX_DOWNLOAD_BYTES:
                continue
            if not trusted_download_url(asset.get("browser_download_url"), owner, repo):
                continue
            release_id = str(release.get("id", ""))
            if not re.fullmatch(r"[0-9]{1,30}", release_id):
                continue
            candidates.append((version, {
                "id": release_id, "tag_name": release["tag_name"],
                "name": str(release.get("name") or release["tag_name"])[:256],
                "published_at": str(release.get("published_at", ""))[:64],
                "asset_name": asset["name"], "asset_size": size,
                "asset_digest": digest.lower(), "download_url": asset["browser_download_url"],
            }))
    return max(candidates, key=lambda item: (item[0], -asset_names.index(item[1]["asset_name"])))[1] if candidates else None


class ReleaseRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every hop, not merely the initial or final URL."""
    max_redirections = 5

    def __init__(self, owner: str, repo: str):
        self.owner, self.repo = owner, repo

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted_download_url(newurl, self.owner, self.repo, allow_cdn=True):
            raise UpdateError("Update redirect left the trusted GitHub download hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_release(release: dict, directory: Path, *, owner: str, repo: str,
                     user_agent: str = "SLS-Mass-Notify") -> Path:
    """Download an exact-size/hash installer asset without executing it."""
    digest = release.get("asset_digest", "")
    expected_size = release.get("asset_size")
    release_id = str(release.get("id", ""))
    if (not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
            or type(expected_size) is not int or not 0 < expected_size <= MAX_DOWNLOAD_BYTES
            or not re.fullmatch(r"\d{1,30}", release_id)
            or not trusted_download_url(release.get("download_url"), owner, repo)
            or release.get("asset_name") not in DEFAULT_ASSETS):
        raise UpdateError("Invalid release asset metadata")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"SLS_Update_{release_id}{Path(release['asset_name']).suffix}"
    request = urllib.request.Request(release["download_url"], headers={"User-Agent": user_agent, "Accept": "application/octet-stream"})
    opener = urllib.request.build_opener(ReleaseRedirectHandler(owner, repo), UpdateHTTPSHandler())
    handle, temporary = tempfile.mkstemp(prefix="SLS_Update_", suffix=".part", dir=directory)
    partial = Path(temporary)
    started = time.monotonic()
    try:
        with os.fdopen(handle, "wb") as stream, opener.open(request, timeout=30) as response:
            if not trusted_download_url(response.geturl(), owner, repo, allow_cdn=True):
                raise UpdateError("Untrusted final download URL")
            total, checksum = 0, hashlib.sha256()
            remaining = DOWNLOAD_DEADLINE_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise UpdateError("Update download exceeded its deadline")
            with DownloadDeadline(_response_socket(response), remaining, "Update download exceeded its deadline"):
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > expected_size:
                        raise UpdateError("Update download exceeded its expected size")
                    checksum.update(block)
                    stream.write(block)
            if total != expected_size or checksum.hexdigest() != digest[7:]:
                raise UpdateError("Update size or SHA-256 verification failed")
            stream.flush()
            os.fsync(stream.fileno())
        partial.replace(destination)
        return destination
    finally:
        partial.unlink(missing_ok=True)


def system_executable(relative: str) -> Path:
    if os.name != "nt":
        raise UpdateError("This action requires Windows")
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise UpdateError("Unable to locate the Windows system directory")
    return Path(buffer.value) / relative


@contextlib.contextmanager
def _locked_windows_file(path: Path):
    """Deny writing/replacing the file until OS trust and signer checks finish."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise UpdateError("Unable to lock executable for trust verification")
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def verify_authenticode(path: Path, trusted_sha256: list[str] | tuple[str, ...]) -> str:
    """Verify OS Authenticode trust and SHA-256 of the actual signing certificate.

    Pins must come from administrator-controlled deployment policy or the signed
    installed application. A downloaded release's publisher name is not a pin.
    This check alone does not make a user-writable executable safe to elevate.
    """
    pins = {str(pin).replace(":", "").upper() for pin in trusted_sha256}
    if not pins or any(not re.fullmatch(r"[0-9A-F]{64}", pin) for pin in pins):
        raise UpdateError("Trusted publisher SHA-256 certificate fingerprints are not configured")
    executable = system_executable(r"WindowsPowerShell\v1.0\powershell.exe")
    path = Path(path).resolve(strict=True)
    # Constant script; no interpolation of the path or user data into code.
    script = "$ErrorActionPreference='Stop'; $s=Get-AuthenticodeSignature -LiteralPath $env:SLS_VERIFY_PATH; if ($s.Status -ne 'Valid' -or $null -eq $s.SignerCertificate) { throw 'Invalid Authenticode signature' }; $h=[Security.Cryptography.SHA256]::Create(); try { [BitConverter]::ToString($h.ComputeHash($s.SignerCertificate.RawData)).Replace('-','') } finally { $h.Dispose() }"
    environment = os.environ.copy()
    environment["SLS_VERIFY_PATH"] = str(path)
    environment["PSModulePath"] = str(executable.parent / "Modules")
    with _locked_windows_file(path):
        try:
            result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command", script],
                                    env=environment, cwd=str(executable.parent), capture_output=True,
                                    text=True, check=True, timeout=60,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (subprocess.SubprocessError, OSError) as exc:
            raise UpdateError("Windows could not verify the update publisher") from exc
        fingerprint = result.stdout.strip().upper()
        if fingerprint not in pins:
            raise UpdateError("The executable was signed by an unapproved publisher")
        return fingerprint


def launch_update_installer(*_args, **_kwargs) -> None:
    raise UpdateError("Automatic installation is disabled until a signed installer is deployed through an administrator-controlled management channel. Downloaded packages must not be elevated from a user-writable directory.")


def maintenance_allowed(policy: dict, *, active_incident: bool, device_id: str,
                        now: datetime | None = None) -> bool:
    """Fail closed on invalid rollout/window policy, always defer active incidents."""
    if active_incident or policy.get("hold", False):
        return False
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        return False
    try:
        percent = policy.get("rollout_percent", 100)
        if type(percent) is not int or not 0 <= percent <= 100:
            return False
        cohort = int.from_bytes(hashlib.sha256((str(policy.get("rollout_salt", "")) + device_id).encode()).digest()[:4], "big") % 100
        if cohort >= percent:
            return False
        start, end = policy.get("start", "00:00"), policy.get("end", "00:00")
        if not all(isinstance(v, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v) for v in (start, end)):
            return False
        minute = now.hour * 60 + now.minute
        low, high = (int(v[:2]) * 60 + int(v[3:]) for v in (start, end))
        return low == high or (low <= minute < high if low < high else minute >= low or minute < high)
    except (TypeError, ValueError, AttributeError):
        return False


def update_state_after_restart(state: dict, current_version: str, *, healthy: bool) -> dict:
    result = dict(state)
    pending = result.get("pending_version")
    if pending and compare_versions(current_version, pending) in (0, 1) and healthy:
        result.update(status="healthy", pending_version="", last_error="", installed_version=current_version)
    elif pending:
        result.update(status="verification_pending")
    return result


def cleanup_downloads(directory: Path, *, max_age_days: int = 7, max_bytes: int = 512 * 1024 * 1024,
                      keep: tuple[Path, ...] = ()) -> int:
    """Only remove this updater's flat regular files; never traverse directories."""
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        return 0
    keep_set = {Path(path).absolute() for path in keep}
    files = []
    for path in directory.iterdir():
        if (re.fullmatch(r"SLS_Update_[A-Za-z0-9_\-]+\.(exe|zip|part)", path.name)
                and path.is_file() and not path.is_symlink()):
            files.append((path, path.stat()))
    files.sort(key=lambda item: item[1].st_mtime, reverse=True)
    retained, removed, cutoff = 0, 0, time.time() - max_age_days * 86400
    for path, stat in files:
        if path.absolute() in keep_set:
            retained += stat.st_size
        elif stat.st_mtime < cutoff or retained + stat.st_size > max_bytes:
            path.unlink(missing_ok=True)
            removed += 1
        else:
            retained += stat.st_size
    return removed
