from __future__ import annotations

import base64
from contextlib import nullcontext
import ctypes
from ctypes import wintypes
import hashlib
import http.client
import json
import os
import queue
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from tkinter import BooleanVar, StringVar, Text, Tk, Toplevel, filedialog, messagebox
from tkinter import ttk

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from sls_store import Inbox
from sls_protocol import (validate_payload, validate_id, profile_namespace, timestamp,
                          ordered_reconciliation, is_test, LiveSnapshotBaseline)
from sls_diagnostics import write_log, export_diagnostics
from sls_presentation import AlertPresenter
from sls_windowing import fit_window, ScrollableBody, apply_window_icon
from sls_version import VERSION
import sls_ipc
import sls_updates
from sls_audio import inspect_wav, copy_validated_audio
from sls_tray import TrayIcon

try:
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover - build includes Pillow, but keep a fallback.
    Image = None
    ImageTk = None

try:
    import winreg
except ImportError:  # pragma: no cover - this app targets Windows.
    winreg = None

try:
    import winsound
except ImportError:  # pragma: no cover - this app targets Windows.
    winsound = None


APP_DISPLAY_NAME = "SouthlandServers Mass Notification App"
APP_SHORT_NAME = "SLS_Mass_Notify"
EXE_NAME = "SLS_Mass_Notify.exe"
COMPANY_NAME = "SouthlandServers"
COMPANY_DISPLAY_NAME = "Southland Servers Group"
APP_VERSION = VERSION
MAX_ENDPOINTS = 3
DELIVERY_LIVE = "live_sse"
PBX_LIVE_PATH = "/api/sipnotify/desktop/stream"
PBX_RECENT_PATH = "/api/sipnotify/desktop"
PBX_ACK_PATH = "/api/sipnotify/desktop/ack"
SSE_CONNECT_TIMEOUT_SECONDS = 25
SSE_TEST_TIMEOUT_SECONDS = 12
SSE_READ_TIMEOUT_SECONDS = 60
ANNOUNCEMENT_MAX_AGE_SECONDS = 10 * 60
DELIVERY_PAUSE_SECONDS = 5
SSE_MAX_LINE_BYTES = 64 * 1024
SSE_MAX_EVENT_BYTES = 512 * 1024
RECENT_EVENT_ID_LIMIT = 100
RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 20, 30)
FAULT_NOTIFY_SECONDS = 5 * 60
FAULT_TOAST_VISIBLE_MS = 18000
IMAGE_FETCH_LIMIT_BYTES = 5 * 1024 * 1024
UPDATE_CHECK_SECONDS = 15 * 60
UPDATE_RETRY_WAKE_SECONDS = 5 * 60
AUDIO_DIR_NAME = "audio"
DEFAULT_AUDIO_NAME = "Announcement.wav"
AUTH_BASIC = "basic"
GITHUB_OWNER = "vipgabe09267"
GITHUB_REPO = "SouthlandServers_Mass_Notify_app"
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases?per_page=10"

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / COMPANY_NAME / APP_SHORT_NAME
CONFIG_PATH = CONFIG_DIR / "settings.json"
LOG_PATH = CONFIG_DIR / "app.log"
UPDATE_DIR = CONFIG_DIR / "updates"
USER_AUDIO_DIR = CONFIG_DIR / AUDIO_DIR_NAME
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
SINGLE_INSTANCE_MUTEX = f"Local\\{APP_SHORT_NAME}_SingleInstance"
_MUTEX_HANDLE = None


XML_KEYS = {
    "xml",
    "yealinkxml",
    "yealinkxmlpayload",
    "yealinkpayload",
    "xmlpayload",
    "exactxmlpayload",
    "exactyealinkxmlpayload",
    "payloadxml",
    "notifyxml",
    "sipnotifyxml",
    "sipnotifypayload",
    "sipnotify",
    "payload",
}
KIND_KEYS = {"kind", "notifykind", "alertkind", "eventkind"}
TITLE_KEYS = {"title", "eventtitle", "headline", "subject", "name"}
EVENT_KEYS = {"event", "eventname", "alerttype", "warningtype"}
SEVERITY_KEYS = {"severity", "level", "alertseverity"}
PRIORITY_KEYS = {"priority", "urgency", "alertpriority"}
PRIORITY_LABEL_KEYS = {"prioritylabel", "priorityname"}
IMAGE_KEYS = {"imageurl", "imageuri", "image", "imgurl", "pictureurl"}
RECIPIENT_KEYS = {"recipients", "recipient", "phones", "extensions", "targets", "devices"}
TIMESTAMP_KEYS = {"timestamp", "timestamps", "sentat", "createdat", "updatedat", "time", "date"}
EVENT_ID_KEYS = {"id", "eventid", "alertid", "notifyid", "notificationid", "messageid"}
DESCRIPTION_KEYS = {"description", "desc", "message", "body", "text"}
BODY_KEYS = {"body", "text", "message", "description", "desc"}
AREA_KEYS = {"area", "areas", "zone", "county", "location"}
EFFECTIVE_KEYS = {"effective", "effectiveat", "starts", "startsat"}
EXPIRES_KEYS = {"expires", "expiresat", "ends", "endsat"}
RECENT_EVENT_KEYS = {"recentevents", "events", "eventlog", "history"}
LATEST_OBJECT_KEYS = {
    "latestsipnotify",
    "latestnotify",
    "latestalert",
    "latestannouncement",
    "announcement",
    "alert",
    "latest",
}


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    bundled = base / name
    if bundled.exists():
        return bundled
    if getattr(sys, "frozen", False):
        sibling = Path(sys.executable).resolve().parent / name
        if sibling.exists():
            return sibling
    return Path(__file__).resolve().parent / name


def app_command(background: bool = True) -> str:
    arg = " --background" if background else ""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"{arg}'
    return f'"{Path(sys.executable).resolve()}" "{Path(__file__).resolve()}"{arg}'


def log(message: str) -> None:
    write_log(LOG_PATH, message)


def is_windows() -> bool:
    return os.name == "nt"


def acquire_single_instance() -> bool:
    if not is_windows():
        return True
    global _MUTEX_HANDLE
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX + "_" + sls_ipc.session_scope())
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle(handle)
            return False
        _MUTEX_HANDLE = handle
        return True
    except Exception as exc:
        log(f"single-instance mutex failed: {exc}")
        return False


def set_startup_enabled(enabled: bool) -> None:
    if winreg is None:
        return
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            machine_startup = (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft" / "Windows"
                               / "Start Menu" / "Programs" / "Startup" / f"{APP_DISPLAY_NAME}.lnk")
            if enabled and not machine_startup.is_file():
                winreg.SetValueEx(key, APP_SHORT_NAME, 0, winreg.REG_SZ, app_command(True))
            else:
                try:
                    winreg.DeleteValue(key, APP_SHORT_NAME)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        log(f"startup registry update failed: {exc}")


class DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


class WindowsCredential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
CREDENTIAL_TARGET_PREFIX = f"{COMPANY_NAME}/{APP_SHORT_NAME}"


def profile_credential_target(index: int, kind: str) -> str:
    safe_kind = "password" if kind == "password" else "token"
    return f"{CREDENTIAL_TARGET_PREFIX}/pbx-{index + 1}/{safe_kind}"


def _write_windows_credential(target: str, secret: str) -> None:
    blob = secret.encode("utf-16-le")
    buffer = (wintypes.BYTE * len(blob)).from_buffer_copy(blob)
    credential = WindowsCredential()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE))
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = APP_SHORT_NAME
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(WindowsCredential), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    if not advapi32.CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def _read_windows_credential(target: str) -> str:
    credential_ptr = ctypes.POINTER(WindowsCredential)()
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(WindowsCredential)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    if not advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(credential_ptr)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        credential = credential_ptr.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return blob.decode("utf-16-le")
    finally:
        advapi32.CredFree(credential_ptr)


def _delete_windows_credential(target: str) -> None:
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    if not advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
        error = ctypes.get_last_error()
        if error != 1168:  # ERROR_NOT_FOUND
            raise ctypes.WinError(error)


def _protect_with_dpapi(secret: str) -> str:
    data = secret.encode("utf-8")
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = DataBlob()

    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _unprotect_with_dpapi(value: str) -> str:
    encrypted = base64.b64decode(value.removeprefix("dpapi:"))
    in_buffer = ctypes.create_string_buffer(encrypted)
    in_blob = DataBlob(len(encrypted), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = DataBlob()

    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError()
    try:
        decrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return decrypted.decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def protect_secret(secret: str) -> str:
    if not secret:
        return ""
    if is_windows():
        return _protect_with_dpapi(secret)
    return "plain:" + base64.b64encode(secret.encode("utf-8")).decode("ascii")


def unprotect_secret(value: str) -> str:
    if not value:
        return ""
    if value.startswith("cred:") and is_windows():
        target = value.removeprefix("cred:")
        if not target.startswith(CREDENTIAL_TARGET_PREFIX + "/"):
            return ""
        try:
            return _read_windows_credential(target)
        except Exception as exc:
            log(f"Windows Credential Manager read failed: {exc}")
            return ""
    if not value.startswith(("dpapi:", "plain:", "cred:")):
        # Older builds stored some secrets as raw text. Read once so Settings can
        # move a password into Windows Credential Manager on the next save.
        return value
    try:
        if value.startswith("dpapi:") and is_windows():
            return _unprotect_with_dpapi(value)
        if value.startswith("plain:"):
            return base64.b64decode(value.removeprefix("plain:")).decode("utf-8")
    except Exception as exc:
        log(f"secret decrypt failed: {exc}")
    return ""


def blank_endpoint(index: int) -> dict:
    return {
        "name": f"PBX {index + 1}",
        "endpoint": "",
        "enabled": index == 0,
        "auth_mode": AUTH_BASIC,
        "delivery_mode": DELIVERY_LIVE,
        "reconnect_automatically": True,
        "username": "",
        "password": "",
        "credential_revision": 0,
        "last_stream_id": "",
        "last_event_id": "",
        "last_fingerprint": "",
        "recent_event_ids": [],
        "live_polling_until": 0,
    }


def normalize_endpoint(value: object, index: int) -> dict:
    endpoint = blank_endpoint(index)
    if isinstance(value, dict):
        configured_url = safe_string(value.get("endpoint") or value.get("url"))
        recent_ids = value.get("recent_event_ids", value.get("recentEventIds", []))
        if not isinstance(recent_ids, list):
            recent_ids = []
        recent_ids = [safe_string(item) for item in recent_ids if safe_string(item)][-RECENT_EVENT_ID_LIMIT:]
        endpoint.update(
            {
                "name": safe_string(value.get("name")) or endpoint["name"],
                "endpoint": configured_url,
                "enabled": bool(value.get("enabled", endpoint["enabled"])),
                "auth_mode": AUTH_BASIC,
                "delivery_mode": DELIVERY_LIVE,
                "reconnect_automatically": bool(value.get("reconnect_automatically", True)),
                "username": safe_string(value.get("username", value.get("user", ""))),
                "password": value.get("password") if isinstance(value.get("password"), str) else "",
                "credential_revision": value.get("credential_revision", 0) if type(value.get("credential_revision", 0)) is int else 0,
                "last_stream_id": safe_string(value.get("last_stream_id", "")),
                "last_event_id": safe_string(value.get("last_event_id", value.get("lastEventId", ""))),
                "last_fingerprint": safe_string(
                    value.get("last_fingerprint", value.get("lastFingerprint", ""))
                ),
                "recent_event_ids": recent_ids,
                "live_polling_until": value.get("live_polling_until", 0)
                    if type(value.get("live_polling_until")) in (int, float)
                    and 0 <= value["live_polling_until"] < 10**11 else 0,
            }
        )
    return endpoint


def normalize_endpoints(config: dict) -> list[dict]:
    endpoints: list[dict] = []
    configured = config.get("endpoints")
    if isinstance(configured, list):
        for index, endpoint in enumerate(configured[:MAX_ENDPOINTS]):
            endpoints.append(normalize_endpoint(endpoint, index))

    if not endpoints and config.get("endpoint"):
        endpoints.append(
            normalize_endpoint(
                {
                    "name": "PBX 1",
                    "endpoint": config.get("endpoint", ""),
                    "enabled": True,
                    "username": config.get("username", ""),
                    "password": config.get("password", ""),
                    "last_event_id": config.get("last_event_id", ""),
                    "last_fingerprint": config.get("last_fingerprint", ""),
                },
                0,
            )
        )

    while len(endpoints) < MAX_ENDPOINTS:
        endpoints.append(blank_endpoint(len(endpoints)))
    return endpoints[:MAX_ENDPOINTS]


def normalize_config(config: dict) -> dict:
    had_endpoint_list = isinstance(config.get("endpoints"), list)
    normalized = default_config()
    if not had_endpoint_list:
        normalized.pop("endpoints", None)
    normalized.update(config)
    normalized["endpoints"] = normalize_endpoints(normalized)
    first = normalized["endpoints"][0]
    normalized["endpoint"] = first.get("endpoint", "")
    normalized["username"] = first.get("username", "")
    normalized["password"] = first.get("password", "")
    normalized["auth_mode"] = AUTH_BASIC
    normalized["last_event_id"] = first.get("last_event_id", "")
    normalized["last_fingerprint"] = first.get("last_fingerprint", "")
    normalized["audio_sound"] = safe_audio_name(safe_string(normalized.get("audio_sound")) or DEFAULT_AUDIO_NAME)
    for legacy_key in ("token", "no_token", "poll_seconds"):
        normalized.pop(legacy_key, None)
    return normalized


def endpoint_has_credentials(endpoint: dict) -> bool:
    return bool(safe_string(endpoint.get("username")) and unprotect_secret(endpoint.get("password", "")))


def endpoint_display_name(index: int, endpoint: dict) -> str:
    name = safe_string(endpoint.get("name")) or f"PBX {index + 1}"
    url = safe_string(endpoint.get("endpoint"))
    if url:
        parsed = urlparse(url)
        if parsed.netloc:
            return f"{name} ({parsed.netloc})"
    return name


def endpoint_url_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme == "https" and parsed.hostname and parsed.username is None
                    and parsed.password is None and (parsed.port is None or 1 <= parsed.port <= 65535))
    except (ValueError, TypeError):
        return False


def normalize_pbx_address(value: object) -> str:
    address = safe_string(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in address):
        raise ValueError("PBX address contains a control character.")
    if not address:
        return ""
    if "://" not in address:
        address = "https://" + address
    parsed = urlparse(address)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in parsed.hostname or "")
        or (parsed.port is not None and not 1 <= parsed.port <= 65535)
    ):
        raise ValueError("PBX address must be an HTTPS hostname or URL without credentials, query, or fragment.")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443
    port = f":{parsed.port}" if parsed.port and parsed.port != default_port else ""
    return f"{parsed.scheme.lower()}://{host}{port}"


def pbx_live_url(endpoint_cfg: dict) -> str:
    origin = normalize_pbx_address(endpoint_cfg.get("endpoint", ""))
    return origin + PBX_LIVE_PATH


def pbx_recent_url(endpoint_cfg: dict) -> str:
    """Snapshot endpoint for live polling; the initial retained set is discarded."""
    origin = normalize_pbx_address(endpoint_cfg.get("endpoint", ""))
    return origin + PBX_RECENT_PATH + "?limit=100"


def audio_search_dirs() -> list[Path]:
    dirs = [
        USER_AUDIO_DIR,
        resource_path(AUDIO_DIR_NAME),
        Path(__file__).resolve().parent / AUDIO_DIR_NAME,
    ]
    if getattr(sys, "frozen", False):
        dirs.insert(1, Path(sys.executable).resolve().parent / AUDIO_DIR_NAME)
    result: list[Path] = []
    seen: set[str] = set()
    for directory in dirs:
        try:
            key = str(directory.resolve()).lower()
        except OSError:
            key = str(directory).lower()
        if key not in seen:
            seen.add(key)
            result.append(directory)
    return result


def safe_audio_name(name: str) -> str:
    candidate = Path(safe_string(name)).name
    if Path(candidate).suffix.lower() != ".wav":
        return DEFAULT_AUDIO_NAME
    return candidate


def list_audio_choices() -> list[str]:
    choices: dict[str, Path] = {}
    for directory in audio_search_dirs():
        if not directory.exists() or not directory.is_dir():
            continue
        for path in directory.glob("*.wav"):
            if path.is_file():
                choices.setdefault(path.name, path)
    names = sorted(choices.keys(), key=str.lower)
    if DEFAULT_AUDIO_NAME in names:
        names.remove(DEFAULT_AUDIO_NAME)
    return [DEFAULT_AUDIO_NAME] + names if DEFAULT_AUDIO_NAME in choices else names


def find_audio_file(name: str) -> Path | None:
    filename = safe_audio_name(name)
    for directory in audio_search_dirs():
        candidate = directory / filename
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def import_custom_audio(source: Path) -> str:
    source = source.resolve(strict=True)
    if source.suffix.lower() != ".wav":
        raise ValueError("Only WAV audio files are supported.")
    inspect_wav(source)
    USER_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    destination = USER_AUDIO_DIR / source.name
    if source == destination.resolve():
        return destination.name
    for attempt in range(100):
        candidate = destination if attempt == 0 else destination.with_name(f"{source.stem}-{attempt}{source.suffix}")
        try:
            copy_validated_audio(source, candidate)
            return candidate.name
        except FileExistsError:
            continue
    raise ValueError("Too many imported sounds with this name.")


def active_endpoints(config: dict) -> list[tuple[int, dict]]:
    result: list[tuple[int, dict]] = []
    for index, endpoint in enumerate(normalize_endpoints(config)):
        url = safe_string(endpoint.get("endpoint"))
        if endpoint.get("enabled", True) and url and endpoint_url_allowed(url) and endpoint_has_credentials(endpoint):
            result.append((index, endpoint))
    return result


def monitoring_idle_status(config: dict, *, connection_test_running: bool = False) -> str:
    """Return an actionable reason when no transport worker can be started."""
    if connection_test_running:
        return "Connection test in progress; monitoring will resume automatically."
    if not bool(config.get("enabled", True)):
        return "Monitoring is disabled in Settings."

    endpoints = normalize_endpoints(config)
    enabled = [(index, endpoint) for index, endpoint in enumerate(endpoints) if endpoint.get("enabled", True)]
    if not enabled:
        return "Monitoring is waiting: enable at least one PBX profile."

    for index, endpoint in enabled:
        name = safe_string(endpoint.get("name")) or f"PBX {index + 1}"
        url = safe_string(endpoint.get("endpoint"))
        if not url:
            return f"Monitoring is waiting: enter the PBX address for {name}."
        if not endpoint_url_allowed(url):
            return f"Monitoring is waiting: correct the PBX address for {name}."
        if not safe_string(endpoint.get("username")):
            return f"Monitoring is waiting: enter the desktop username for {name}."
        if not unprotect_secret(endpoint.get("password", "")):
            return f"Monitoring is waiting: enter the desktop password for {name}."
    return "Starting PBX monitoring..."


def default_config() -> dict:
    return {
        "schema_version": 2,
        "auto_update_enabled": False,
        "update_channel": "stable",
        "update_policy": {"enabled": False},
        "trusted_signers": [],
        "retention_days": 30,
        "audio_sound": DEFAULT_AUDIO_NAME,
        "endpoint": "",
        "enabled": True,
        "endpoints": [blank_endpoint(index) for index in range(MAX_ENDPOINTS)],
        "last_update_check_ts": 0.0,
        "last_update_commit": "",
        "last_update_error": "",
        "last_update_release_id": "",
        "last_update_release_name": "",
        "last_update_release_tag": "",
        "pending_update_release_id": "",
        "startup_enabled": True,
        "username": "",
        "password": "",
        "last_event_id": "",
        "last_fingerprint": "",
    }


def load_config() -> dict:
    loaded: dict = {}
    try:
        if CONFIG_PATH.exists():
            if CONFIG_PATH.stat().st_size > 1024 * 1024:
                raise ValueError("Saved settings exceed the safety limit")
            with CONFIG_PATH.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("Saved settings must be a JSON object")
    except (OSError, ValueError) as exc:
        # Refuse to silently replace the only copy with empty defaults.
        raise ValueError(f"Cannot load settings. Restore or repair {CONFIG_PATH.name}: {exc}") from exc
    version = loaded.get("schema_version", 1)
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("Settings use an unsupported schema version; refusing to overwrite them.")
    for key in ("enabled", "startup_enabled", "auto_update_enabled"):
        if key in loaded and type(loaded[key]) is not bool:
            raise ValueError(f"Saved {key} must be true or false.")
    if "endpoints" in loaded:
        if not isinstance(loaded["endpoints"], list) or len(loaded["endpoints"]) > MAX_ENDPOINTS:
            raise ValueError("Saved PBX profiles are invalid.")
        for profile in loaded["endpoints"]:
            if not isinstance(profile, dict):
                raise ValueError("Each PBX profile must be an object.")
            for key in ("enabled", "reconnect_automatically"):
                if key in profile and type(profile[key]) is not bool:
                    raise ValueError(f"Profile {key} must be true or false.")
    days = loaded.get("retention_days", 30)
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError("History retention must be between 1 and 3650 days.")
    loaded["schema_version"] = 2
    machine_path = Path(sys.executable).resolve().parent / "installation-defaults.json"
    if getattr(sys, "frozen", False) and machine_path.is_file():
        try:
            defaults = json.loads(machine_path.read_text(encoding="utf-8"))
            for key in ("startup_enabled", "auto_update_enabled"):
                if isinstance(defaults.get(key), bool):
                    loaded.setdefault(key, defaults[key])
        except (OSError, ValueError, AttributeError):
            log("Machine installation defaults could not be read")
    cfg = normalize_config(loaded)
    changed = False
    for profile in cfg["endpoints"]:
        secret = profile.get("password", "")
        if secret and not secret.startswith(("cred:", "dpapi:")):
            # DPAPI migration changes no existing Credential Manager records; the
            # original file is preserved until the atomic replacement succeeds.
            profile["password"] = protect_secret(unprotect_secret(secret))
            changed = True
    if changed:
        cfg = normalize_config(cfg)
        save_config(cfg)
    return cfg


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=CONFIG_DIR,
            prefix="settings_",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as fh:
            temp_path = Path(fh.name)
            json.dump(config, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
            temp_path = Path(fh.name)
        temp_path.replace(CONFIG_PATH)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def notify_existing_instance(show_settings: bool) -> bool:
    return sls_ipc.send(CONFIG_DIR, "SHOW_SETTINGS" if show_settings else "PING", unprotect_secret)


def normalize_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def safe_string(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def parse_release_version(value: object):
    return sls_updates.parse_version(value)


def compare_release_versions(candidate: object, current: object | None = None) -> int | None:
    return sls_updates.compare_versions(candidate, APP_VERSION if current is None else current)


def trusted_github_download_url(value: object) -> bool:
    return sls_updates.trusted_download_url(value, GITHUB_OWNER, GITHUB_REPO)


def lookup(obj: object, keys: set[str]) -> object | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if normalize_key(key) in keys and value not in (None, ""):
                return value
    return None


def lookup_preferred(obj: object, keys: tuple[str, ...]) -> object | None:
    if isinstance(obj, dict):
        normalized = {normalize_key(key): value for key, value in obj.items()}
        for key in keys:
            value = normalized.get(normalize_key(key))
            if value not in (None, ""):
                return value
    return None


def lookup_object(obj: object, keys: set[str]) -> object | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if normalize_key(key) in keys and isinstance(value, (dict, list)):
                return value
    return None


def first_http_url(text: str) -> str:
    match = re.search(r"https?://[^\s\"'<>]+", text or "", re.IGNORECASE)
    return match.group(0) if match else ""


def extract_xml_text(payload: str) -> str:
    if not payload:
        return ""
    xml_match = re.search(r"(<\?xml[\s\S]+)$", payload.strip(), re.IGNORECASE)
    if xml_match:
        return xml_match.group(1).strip()
    yealink_match = re.search(r"(<Yealink[\s\S]+)$", payload.strip(), re.IGNORECASE)
    if yealink_match:
        return yealink_match.group(1).strip()
    if payload.lstrip().startswith("<"):
        return payload.strip()
    return ""


def strip_namespace(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


@dataclass
class PhonePayload:
    screen_type: str = ""
    title: str = ""
    text: str = ""
    prompt: str = ""
    image_url: str = ""
    softkeys: list[str] = field(default_factory=list)


def parse_yealink_payload(xml_text: str) -> PhonePayload:
    phone = PhonePayload()
    xml_body = extract_xml_text(xml_text)
    if not xml_body:
        return phone
    try:
        root = ET.fromstring(xml_body.encode("utf-8"))
    except (ET.ParseError, DefusedXmlException) as exc:
        log(f"yealink xml parse failed: {exc}")
        return phone

    phone.screen_type = strip_namespace(root.tag)
    all_text_parts: list[str] = []
    for elem in root.iter():
        tag = normalize_key(strip_namespace(elem.tag))
        value = (elem.text or "").strip()
        if not value:
            continue
        if tag == "title" and not phone.title:
            phone.title = value
        elif tag in {"text", "message", "body"}:
            all_text_parts.append(value)
        elif tag == "prompt" and not phone.prompt:
            phone.prompt = value
        elif tag in {"image", "imageurl", "url"} and not phone.image_url:
            url = first_http_url(value) or value
            if url.lower().startswith(("http://", "https://")):
                phone.image_url = url
        elif tag in {"label", "softkey", "softkeylabel"}:
            phone.softkeys.append(value)

    if not all_text_parts:
        for elem in root.iter():
            value = (elem.text or "").strip()
            tag = normalize_key(strip_namespace(elem.tag))
            if value and tag not in {"title", "prompt", "label"}:
                all_text_parts.append(value)
    phone.text = "\n".join(dict.fromkeys(all_text_parts))
    if not phone.image_url:
        phone.image_url = first_http_url(xml_body)
    return phone


@dataclass
class AlertData:
    raw: object
    raw_text: str
    source_endpoint: str
    kind: str
    event: str
    title: str
    severity: str
    priority: str
    priority_label: str
    image_url: str
    xml_payload: str
    recipients: str
    timestamp: str
    area: str
    effective: str
    expires: str
    body: str
    description: str
    recent_events: str
    event_id: str
    fingerprint: str
    announcement_style: str = "standard"
    background_color: str = ""
    header_color: str = ""
    accent_color: str = ""
    text_color: str = ""
    test_only: bool = False
    incident_id: str = ""
    revision: int = 0
    action: str = "notify"
    language: str = "en"
    display_mode: str = ""
    stream_id: str = ""
    display_timeout_seconds: int | None = None
    display_expires_at: str = ""
    created_at: str = ""
    delivery_expires_at: str = ""


def safe_hex_color(value: object) -> str:
    color = safe_string(value)
    return color.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else ""


def bounded_text(value: object, limit: int) -> str:
    text = safe_string(value)
    return text[:limit]


def format_recent_events(value: object) -> str:
    if not value:
        return ""
    if isinstance(value, list):
        lines = []
        for item in value[:10]:
            if isinstance(item, dict):
                title = safe_string(lookup(item, TITLE_KEYS)) or safe_string(item.get("title", ""))
                sev = safe_string(lookup(item, SEVERITY_KEYS))
                stamp = safe_string(lookup(item, TIMESTAMP_KEYS))
                parts = [part for part in (stamp, sev, title) if part]
                lines.append(" | ".join(parts) if parts else safe_string(item))
            else:
                lines.append(safe_string(item))
        return "\n".join(lines)
    return safe_string(value)


def extract_alert(data: object, raw_text: str) -> AlertData:
    source = lookup_object(data, LATEST_OBJECT_KEYS) or data
    if isinstance(source, list) and source:
        source = source[0]

    xml_value = lookup(source, XML_KEYS)
    xml_payload = safe_string(xml_value)
    if not xml_payload and raw_text.lstrip().startswith("<"):
        xml_payload = raw_text

    phone = parse_yealink_payload(xml_payload)
    kind = bounded_text(lookup(source, KIND_KEYS), 40)
    event = bounded_text(lookup(source, EVENT_KEYS), 160)
    title = bounded_text(lookup_preferred(source, ("title",)), 160) or event or bounded_text(phone.title, 160)
    severity = bounded_text(lookup(source, SEVERITY_KEYS), 80)
    priority = bounded_text(lookup(source, PRIORITY_KEYS), 40)
    priority_label = bounded_text(lookup(source, PRIORITY_LABEL_KEYS), 80)
    image_url = safe_string(lookup(source, IMAGE_KEYS)) or phone.image_url
    recipients = bounded_text(lookup(source, RECIPIENT_KEYS), 1000)
    timestamp = bounded_text(lookup(source, TIMESTAMP_KEYS), 100)
    area = bounded_text(lookup(source, AREA_KEYS), 500)
    effective = bounded_text(lookup(source, EFFECTIVE_KEYS), 100)
    expires = bounded_text(lookup(source, EXPIRES_KEYS), 100)
    # The PBX contract intentionally prefers message over compatibility aliases.
    body = bounded_text(lookup_preferred(source, ("message", "body", "text", "description", "desc")), 131072)
    description = bounded_text(lookup_preferred(source, ("description", "message", "body", "text", "desc")), 131072)
    event_id = safe_string(lookup(source, EVENT_ID_KEYS))
    recent_events = format_recent_events(lookup(data, RECENT_EVENT_KEYS))

    presentation = source.get("presentation") if isinstance(source, dict) else None
    if not isinstance(presentation, dict):
        presentation = {}
    announcement_style = bounded_text(
        presentation.get("style") or (source.get("announcement_style") if isinstance(source, dict) else ""), 40
    ) or "standard"
    background_color = safe_hex_color(
        presentation.get("background_color") or (source.get("background_color") if isinstance(source, dict) else "")
    )
    header_color = safe_hex_color(
        presentation.get("header_color") or (source.get("header_color") if isinstance(source, dict) else "")
    )
    accent_color = safe_hex_color(
        presentation.get("accent_color") or (source.get("accent_color") if isinstance(source, dict) else "")
    )
    text_color = safe_hex_color(
        presentation.get("text_color") or (source.get("text_color") if isinstance(source, dict) else "")
    )

    if not kind:
        normalized_title_event = normalize_key(f"{title} {event}")
        kind = "announcement" if "announcement" in normalized_title_event else "alert"
    if not body:
        body = description or bounded_text(phone.text, 131072)
    if not description:
        description = body
    if not title and phone.text:
        title = phone.text.splitlines()[0][:80]
    if not title:
        title = "SLS Mass Notification"

    test_only = is_test(source)

    fingerprint_source = "|".join(
        [
            event_id,
            kind,
            timestamp,
            event,
            title,
            severity,
            priority,
            priority_label,
            image_url,
            area,
            effective,
            expires,
            body,
            description,
            xml_payload,
            raw_text[:4000],
        ]
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8", errors="ignore")).hexdigest()

    return AlertData(
        raw=data,
        raw_text=raw_text,
        source_endpoint="",
        kind=kind,
        event=event,
        title=title,
        severity=severity,
        priority=priority,
        priority_label=priority_label,
        image_url=image_url,
        xml_payload=xml_payload,
        recipients=recipients,
        timestamp=timestamp,
        area=area,
        effective=effective,
        expires=expires,
        body=body,
        description=description,
        recent_events=recent_events,
        event_id=event_id,
        fingerprint=fingerprint,
        announcement_style=announcement_style,
        background_color=background_color,
        header_color=header_color,
        accent_color=accent_color,
        text_color=text_color,
        test_only=test_only,
        incident_id=safe_string(source.get("incident_id", "")) if isinstance(source, dict) else "",
        revision=source.get("revision", 0) if isinstance(source, dict) else 0,
        action=source.get("action", "notify") if isinstance(source, dict) else "notify",
        language=safe_string(source.get("language", "en")) if isinstance(source, dict) else "en",
        display_mode=safe_string(presentation.get("mode", "")),
        display_timeout_seconds=source.get("display_timeout_seconds") if isinstance(source, dict) else None,
        display_expires_at=safe_string(source.get("display_expires_at", "")) if isinstance(source, dict) else "",
        created_at=safe_string(source.get("created_at", "")) if isinstance(source, dict) else "",
    )


class ApiError(Exception):
    pass


class UnauthorizedError(ApiError):
    pass


class TlsRequiredError(ApiError):
    pass


class RateLimitedError(ApiError):
    def __init__(self, message: str, retry_after: float = 60):
        super().__init__(message)
        self.retry_after = min(3600.0, max(1.0, retry_after))


class ServiceUnavailableError(ApiError):
    pass


class StreamProtocolError(ApiError):
    pass


def request_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, host, port


class HttpSchemeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_scheme = urlparse(newurl).scheme.lower()
        if new_scheme not in {"http", "https"}:
            raise urllib.error.HTTPError(req.full_url, code, "Redirected to a non-HTTP URL", headers, fp)
        if urlparse(req.full_url).scheme.lower() == "https" and urlparse(newurl).scheme.lower() != "https":
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "Endpoint attempted an HTTPS downgrade; request blocked",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SameOriginRedirectHandler(HttpSchemeRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if request_origin(req.full_url) != request_origin(newurl):
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "Endpoint redirected to a different origin; request blocked to protect credentials",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def response_socket(response):
    """Keep the actual socket alive while a watchdog may need to interrupt its read."""
    for candidate in (
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(response, "fp", None), "_sock", None),
    ):
        if candidate is not None:
            return candidate
    return None


def shutdown_socket(sock) -> None:
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass


class SocketDeadline:
    """Absolute read deadline; incoming partial bytes cannot extend it.

    Renewals update one deadline without creating a thread per SSE line. The one
    active timer either interrupts the socket or reschedules for the renewed time.
    Cancellation and expiry serialize so a canceled timer cannot shut down a socket.
    """
    def __init__(self, sock, seconds: float, *, deadline=None, message="Response deadline exceeded"):
        self.sock = sock
        self.message = message
        self.deadline = time.monotonic() + seconds if deadline is None else deadline
        self.lock = threading.Lock()
        self.cancelled = False
        self.expired = False
        self.timer = None
        if sock is not None:
            self._schedule(seconds)

    def _schedule(self, seconds: float) -> None:
        self.timer = threading.Timer(max(0.001, seconds), self._expire)
        self.timer.daemon = True
        self.timer.name = "PBXResponseDeadline"
        self.timer.start()

    def _expire(self) -> None:
        with self.lock:
            if self.cancelled:
                return
            remaining = self.deadline - time.monotonic()
            if remaining > 0:
                self._schedule(remaining)
                return
            self.expired = True
            shutdown_socket(self.sock)

    def renew(self, deadline: float) -> None:
        with self.lock:
            if not self.cancelled and not self.expired:
                self.deadline = deadline

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True
            if self.timer is not None:
                self.timer.cancel()
            self.sock = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.cancel()
        if self.expired:
            raise TimeoutError(self.message) from exc


class DeadlineHTTPSConnection(http.client.HTTPSConnection):
    def getresponse(self):
        seconds = self.timeout if isinstance(self.timeout, (float, int)) else SSE_CONNECT_TIMEOUT_SECONDS
        with SocketDeadline(self.sock, seconds, message="HTTPS response headers exceeded their deadline"):
            return super().getresponse()


class DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        # The inherited handler creates the standard verified TLS context.
        return self.do_open(DeadlineHTTPSConnection, request, context=self._context)


def build_http_opener(*, same_origin: bool = False):
    handlers: list[object] = [SameOriginRedirectHandler() if same_origin else HttpSchemeRedirectHandler(),
                             DeadlineHTTPSHandler()]
    return urllib.request.build_opener(*handlers)


def open_http_request(
    request: urllib.request.Request,
    timeout: int,
    *,
    same_origin: bool = False,
):
    opener = build_http_opener(same_origin=same_origin)
    return opener.open(request, timeout=timeout)


def raise_http_status(exc: urllib.error.HTTPError) -> None:
    # Error bodies are not used. Close their connection before translating the
    # status, including rate-limit responses that will not be retried immediately.
    exc.close()
    if exc.code == 401:
        raise UnauthorizedError("Authentication failed. Review the desktop username and password.") from exc
    if exc.code == 426:
        raise TlsRequiredError("HTTPS is required by the PBX.") from exc
    if exc.code == 429:
        try:
            delay = float(exc.headers.get("Retry-After", "60"))
        except (ValueError, TypeError):
            delay = 60
        raise RateLimitedError("PBX request limit reached; waiting before retrying.", delay) from exc
    if exc.code == 503:
        raise ServiceUnavailableError("The PBX desktop notification service is unavailable.") from exc
    raise ApiError(f"HTTP {exc.code}: {exc.reason}") from exc


def endpoint_auth_headers(endpoint_cfg: dict) -> dict[str, str]:
    username = safe_string(endpoint_cfg.get("username"))
    password = unprotect_secret(endpoint_cfg.get("password", ""))
    if not username or not password:
        return {}
    if ":" in username or any(ord(char) < 32 or ord(char) == 127 for char in username):
        raise ApiError("Desktop username contains an unsupported character.")
    raw = f"{username}:{password}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def send_receipt_ack(endpoint_cfg: dict, event_id: str) -> None:
    event_id = validate_id(event_id, optional=False)
    url = normalize_pbx_address(endpoint_cfg.get("endpoint", "")) + PBX_ACK_PATH
    request = urllib.request.Request(url, data=json.dumps({"event_id": event_id}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": f"{APP_SHORT_NAME}/{APP_VERSION}", **endpoint_auth_headers(endpoint_cfg)}, method="POST")
    try:
        with open_http_request(request, timeout=10, same_origin=True) as response:
            with SocketDeadline(response_socket(response), 10):
                body = response.read(4097)
    except urllib.error.HTTPError as exc:
        raise_http_status(exc)
    if len(body) > 4096:
        raise StreamProtocolError("Receipt response exceeds the size limit")
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict) or data.get("ok") is not True or data.get("event_id") != event_id:
        raise StreamProtocolError("PBX did not confirm the notification receipt")


def alert_expiry(alert: AlertData) -> float | None:
    if alert.kind.lower() == "announcement" and alert.display_timeout_seconds is not None:
        expires = None if alert.display_timeout_seconds == 0 else timestamp(alert.display_expires_at)
    else:
        expires = timestamp(alert.display_expires_at or alert.expires)
    delivery_expires = timestamp(alert.delivery_expires_at)
    return min(expires, delivery_expires) if expires is not None and delivery_expires is not None else (
        delivery_expires if delivery_expires is not None else expires)


def fetch_endpoint(
    endpoint: str,
    auth_headers: dict[str, str] | None = None,
    *, include_server_time: bool = False,
):
    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "User-Agent": f"{APP_SHORT_NAME}/{APP_VERSION}",
    }
    headers.update(auth_headers or {})
    request = urllib.request.Request(endpoint, headers=headers, method="GET")
    opener = build_http_opener(same_origin=True)
    try:
        with opener.open(request, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "")
            server_date = response.headers.get("Date", "")
            with SocketDeadline(response_socket(response), 10):
                raw_bytes = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise_http_status(exc)
    except urllib.error.URLError as exc:
        raise ApiError(str(exc.reason)) from exc
    except TimeoutError as exc:
        raise ApiError("Request timed out") from exc

    if len(raw_bytes) > 1024 * 1024:
        raise ApiError("PBX response exceeded the safety limit")
    raw_text = raw_bytes.decode("utf-8", errors="replace")
    if "json" not in content_type.lower() and not raw_text.lstrip().startswith(("{", "[")):
        raise ApiError("PBX reconciliation response was not JSON")
    try:
        data = json.loads(raw_text)
        if include_server_time:
            # Use the PBX's clock, not the workstation's potentially skewed clock.
            try:
                server_time = parsedate_to_datetime(server_date).timestamp()
            except (ValueError, TypeError, OverflowError) as exc:
                raise StreamProtocolError("Live polling requires a valid HTTP Date header.") from exc
            return data, raw_text, server_time
        return data, raw_text
    except json.JSONDecodeError as exc:
        raise ApiError("PBX returned invalid JSON") from exc


@dataclass
class SseEvent:
    name: str
    data: str
    event_id: str
    retry_ms: int | None = None
    explicit_id: bool = False


def iter_sse_events(response, *, on_activity=None, stop_event: threading.Event | None = None,
                    emit_heartbeats: bool = False):
    event_name = ""
    event_id = ""
    data_lines: list[str] = []
    retry_ms: int | None = None
    event_bytes = 0
    explicit_id = False

    def dispatch():
        nonlocal event_name, data_lines, retry_ms, event_bytes, explicit_id
        if not data_lines:
            event_name = ""
            event_bytes = 0
            return None
        event = SseEvent(event_name or "message", "\n".join(data_lines), event_id, retry_ms, explicit_id)
        event_name = ""
        data_lines = []
        retry_ms = None
        event_bytes = 0
        explicit_id = False
        return event

    while stop_event is None or not stop_event.is_set():
        raw_line = response.readline(SSE_MAX_LINE_BYTES + 1)
        if not raw_line:
            pending = dispatch()
            if pending is not None:
                yield pending
            return
        if len(raw_line) > SSE_MAX_LINE_BYTES:
            raise StreamProtocolError("SSE line exceeded the client safety limit.")
        if on_activity is not None and raw_line.endswith(b"\n"):
            on_activity()
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            pending = dispatch()
            if pending is not None:
                yield pending
            continue
        if line.startswith(":"):
            if emit_heartbeats and line[1:].strip():
                yield SseEvent("_heartbeat", "", event_id)
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            event_bytes += len(raw_line)
            if event_bytes > SSE_MAX_EVENT_BYTES:
                raise StreamProtocolError("SSE event exceeded the client safety limit.")
            data_lines.append(value)
        elif field == "id" and "\x00" not in value:
            event_id = validate_id(value)
            explicit_id = True
        elif field == "retry" and value.isdigit():
            retry_ms = min(int(value), 60_000)


def set_stream_read_timeout(response, timeout: int) -> None:
    """Apply a read timeout after HTTP headers arrive without lengthening authentication."""
    sock = response_socket(response)
    if sock is not None and hasattr(sock, "settimeout"):
        sock.settimeout(timeout)


def open_sse_response(
    endpoint_cfg: dict,
    *,
    timeout: int = SSE_CONNECT_TIMEOUT_SECONDS,
    read_timeout: int = SSE_READ_TIMEOUT_SECONDS,
):
    if not endpoint_has_credentials(endpoint_cfg):
        raise ApiError("Desktop username and password are required.")
    url = pbx_live_url(endpoint_cfg)
    if urlparse(url).scheme.lower() != "https":
        raise TlsRequiredError("Live authenticated handshake requires HTTPS.")
    headers = {
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
        "User-Agent": f"{APP_SHORT_NAME}/{APP_VERSION}",
        **endpoint_auth_headers(endpoint_cfg),
    }
    # No Last-Event-ID: every connection starts at the current journal tail.
    # Missed broadcasts must never be replayed after startup or reconnect.
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = build_http_opener(same_origin=True)
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise_http_status(exc)
    except urllib.error.URLError as exc:
        raise ApiError(str(exc.reason)) from exc
    except TimeoutError as exc:
        raise ApiError("Live handshake timed out.") from exc
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/event-stream" not in content_type:
        response.close()
        raise StreamProtocolError("PBX did not return an event-stream response.")
    set_stream_read_timeout(response, min(read_timeout, SSE_TEST_TIMEOUT_SECONDS))
    return response


def ensure_api_ok(data: object) -> None:
    if isinstance(data, dict) and data.get("ok") is False:
        message = safe_string(data.get("error") or data.get("message") or "API returned ok=false")
        raise ApiError(message)


def resolve_image_url(image_url: str, endpoint: str) -> str:
    image_url = safe_string(image_url)
    if not image_url:
        return ""

    parsed = urlparse(image_url)
    base = urlparse(endpoint)
    if parsed.scheme and parsed.netloc:
        return image_url
    if not base.scheme or not base.netloc:
        return image_url

    if image_url.startswith("//"):
        return f"{base.scheme}:{image_url}"
    if parsed.scheme and not parsed.netloc and parsed.path.startswith("/"):
        suffix = parsed.path
        if parsed.query:
            suffix += f"?{parsed.query}"
        if parsed.fragment:
            suffix += f"#{parsed.fragment}"
        return f"{base.scheme}://{base.netloc}{suffix}"
    if image_url.startswith("/"):
        return f"{base.scheme}://{base.netloc}{image_url}"
    return urljoin(endpoint, image_url)


def normalize_alert_urls(alert: AlertData, endpoint: str) -> None:
    alert.image_url = resolve_image_url(alert.image_url, endpoint)
    alert.source_endpoint = endpoint
    if alert.image_url:
        image_origin = request_origin(alert.image_url)
        source_origin = request_origin(endpoint)
        if image_origin != source_origin:
            alert.image_url = ""
        elif image_origin[0] != "https":
            alert.image_url = ""


def fetch_image_bytes(
    image_url: str,
    *,
    source_endpoint: str = "",
) -> bytes:
    parsed = urlparse(image_url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ApiError("Alert image URL must be HTTPS without embedded credentials")
    if source_endpoint and request_origin(image_url) != request_origin(source_endpoint):
        raise ApiError("Cross-origin alert media was blocked")
    request = urllib.request.Request(
        image_url,
        headers={
            "Accept": "image/png,image/gif,image/*,*/*",
            "User-Agent": f"{APP_SHORT_NAME}/{APP_VERSION}",
        },
        method="GET",
    )
    with open_http_request(
        request,
        timeout=8,
        same_origin=True,
    ) as response:
        with SocketDeadline(response_socket(response), 12):
            data = response.read(IMAGE_FETCH_LIMIT_BYTES + 1)
    if len(data) > IMAGE_FETCH_LIMIT_BYTES:
        raise ApiError("Image response was too large")
    return data


def fetch_latest_github_release(channel: str = "stable") -> dict | None:
    request = urllib.request.Request(GITHUB_RELEASES_URL, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"{APP_SHORT_NAME}/{APP_VERSION}",
        "X-GitHub-Api-Version": "2022-11-28"}, method="GET")
    with open_http_request(request, timeout=12, same_origin=True) as response:
        with SocketDeadline(response_socket(response), 12):
            raw_bytes = response.read(1024 * 512 + 1)
    if len(raw_bytes) > 1024 * 512:
        raise ApiError("Release metadata exceeds the size limit")
    return sls_updates.select_release(json.loads(raw_bytes.decode("utf-8")), APP_VERSION,
        channel=channel, owner=GITHUB_OWNER, repo=GITHUB_REPO)


def download_update_installer(release: dict) -> Path:
    return sls_updates.download_release(release, UPDATE_DIR, owner=GITHUB_OWNER,
        repo=GITHUB_REPO, user_agent=f"{APP_SHORT_NAME}/{APP_VERSION}")


def launch_update_installer(installer_path: Path, install_dir: Path | None = None) -> None:
    sls_updates.launch_update_installer(installer_path, install_dir)


def play_alert_sound(audio_name: str = DEFAULT_AUDIO_NAME) -> float:
    if winsound is None:
        raise RuntimeError("Windows sound output is unavailable")
    sound_path = find_audio_file(audio_name) or find_audio_file(DEFAULT_AUDIO_NAME)
    if sound_path is None:
        raise FileNotFoundError("No notification sound is available")
    info = inspect_wav(sound_path)
    winsound.PlaySound(str(sound_path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    return info.seconds


class RawTextWindow:
    def __init__(self, root: Tk, title: str, content: str) -> None:
        self.window = Toplevel(root)
        self.window.title(title)
        self.window.geometry("780x520")
        icon = resource_path("favicon.ico")
        if icon.exists():
            try:
                self.window.iconbitmap(str(icon))
            except Exception:
                pass

        frame = ttk.Frame(self.window, padding=10)
        frame.pack(fill="both", expand=True)
        self.window.configure(bg="#111418")
        text = Text(
            frame,
            wrap="word",
            font=("Consolas", 10),
            bg="#0f1317",
            fg="#e6edf3",
            insertbackground="#e6edf3",
            selectbackground="#264f78",
            relief="flat",
            padx=10,
            pady=10,
            highlightthickness=1,
            highlightbackground="#343b44",
        )
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", content)
        text.configure(state="disabled")
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        fit_window(self.window, (780, 520))


class FaultToast:
    WIDTH = 420
    HEIGHT = 150

    def __init__(self, app: "MassNotifyApp", message: str) -> None:
        import tkinter as tk

        self.app = app
        self.window = Toplevel(app.root)
        self.window.overrideredirect(True)
        self.window.configure(bg="#181818")
        try:
            self.window.attributes("-topmost", True)
        except Exception:
            pass

        canvas = tk.Canvas(
            self.window,
            width=self.WIDTH,
            height=self.HEIGHT,
            bg="#181818",
            highlightthickness=0,
            bd=0,
        )
        canvas.pack(fill="both", expand=True)
        canvas.create_rectangle(0, 0, self.WIDTH, self.HEIGHT, fill="#181818", outline="#353535")
        canvas.create_rectangle(0, 0, 8, self.HEIGHT, fill="#d18a00", outline="#d18a00")
        canvas.create_text(
            24,
            22,
            text="SLS Mass Notify Fault",
            fill="#ffffff",
            anchor="w",
            font=("Segoe UI", 12, "bold"),
        )
        canvas.create_text(
            24,
            58,
            text=message[:140] + ("…" if len(message) > 140 else ""),
            fill="#f0f0f0",
            anchor="nw",
            width=360,
            font=("Segoe UI", 10),
        )
        canvas.create_text(
            24,
            130,
            text="Click to open settings. Right-click to dismiss.",
            fill="#bdbdbd",
            anchor="w",
            font=("Segoe UI", 9),
        )

        self.window.bind("<Button-1>", self._open_settings)
        self.window.bind("<Button-3>", lambda _event: self.hide())
        canvas.bind("<Button-1>", self._open_settings)
        canvas.bind("<Button-3>", lambda _event: self.hide())
        self._place()
        if winsound is not None:
            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except RuntimeError:
                pass
        self.window.after(FAULT_TOAST_VISIBLE_MS, self.hide)

    def _place(self) -> None:
        fit_window(self.window, (self.WIDTH, self.HEIGHT), minimum=(320, 120))

    def _open_settings(self, _event=None) -> None:
        self.hide()
        self.app.show_settings()

    def hide(self) -> None:
        if self.window.winfo_exists():
            self.window.destroy()


class SettingsWindow:
    def __init__(self, app: "MassNotifyApp") -> None:
        self.app = app
        self.window = Toplevel(app.root)
        self.window.title("SLS Mass Notify — Settings")
        self._set_initial_geometry()
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        icon = resource_path("favicon.ico")
        if icon.exists():
            try:
                self.window.iconbitmap(str(icon))
            except Exception:
                pass

        self._configure_style()
        cfg = normalize_config(app.get_config())
        self.enabled_var = BooleanVar(value=bool(cfg.get("enabled", True)))
        self.startup_var = BooleanVar(value=bool(cfg.get("startup_enabled", True)))
        self.auto_update_var = BooleanVar(value=bool(cfg.get("auto_update_enabled", False)))
        self.audio_var = StringVar(value=safe_audio_name(safe_string(cfg.get("audio_sound")) or DEFAULT_AUDIO_NAME))
        self.audio_combo: ttk.Combobox | None = None
        self.endpoint_forms: list[dict] = []
        self.endpoint_panels: list[ttk.Frame] = []
        self.endpoint_buttons: list[ttk.Button] = []
        self.selected_endpoint_index = 0
        self.logo_image = None
        self.monitor_badge: ttk.Label | None = None
        self.test_button: ttk.Button | None = None

        self._build(cfg)
        self.window.update_idletasks()
        self._center()
        self.window.lift()
        self.window.focus_force()

    def _set_initial_geometry(self) -> None:
        fit_window(self.window, (1140, 840), minimum=(600, 400))

    def _configure_style(self) -> None:
        style = ttk.Style(self.window)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        page = "#111418"
        surface = "#181c21"
        inset = "#20252b"
        field = "#0f1317"
        ink = "#f3f4f6"
        muted = "#a7afb9"
        accent = "#2f81f7"
        accent_hover = "#388bfd"
        border = "#343b44"

        self.window.configure(bg=page)
        style.configure(".", background=page, foreground=ink, font=("Segoe UI", 10))
        style.configure("Surface.TFrame", background=page)
        style.configure("Card.TFrame", background=surface, borderwidth=1, relief="solid")
        style.configure("Panel.TFrame", background=surface, borderwidth=0, relief="flat")
        style.configure("Inset.TFrame", background=inset)
        style.configure("Header.TFrame", background=surface)
        style.configure("Header.TLabel", background=surface, foreground=ink, font=("Segoe UI Semibold", 16))
        style.configure("HeaderHint.TLabel", background=surface, foreground=muted, font=("Segoe UI", 9))
        style.configure("Version.TLabel", background=surface, foreground=muted, font=("Segoe UI", 9))
        style.configure("BadgeOn.TLabel", background=surface, foreground="#4ade80", font=("Segoe UI Semibold", 9))
        style.configure("BadgeOff.TLabel", background=surface, foreground=muted, font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=surface, foreground=ink, font=("Segoe UI Semibold", 11))
        style.configure("Field.TLabel", background=inset, foreground=ink, font=("Segoe UI", 9))
        style.configure("Hint.TLabel", background=surface, foreground=muted, font=("Segoe UI", 9))
        style.configure("InsetHint.TLabel", background=inset, foreground=muted, font=("Segoe UI", 9))
        style.configure("StatusBar.TFrame", background=surface)
        style.configure("Status.TLabel", background=surface, foreground=muted, font=("Segoe UI", 9))
        style.configure("RedWarning.TLabel", background=inset, foreground="#ff7b72", font=("Segoe UI Semibold", 9))
        style.configure("YellowWarning.TLabel", background=inset, foreground="#e3b341", font=("Segoe UI Semibold", 9))
        style.configure("Card.TCheckbutton", background=surface, foreground=ink, padding=(0, 3))
        style.map("Card.TCheckbutton", background=[("active", surface)])
        style.configure("Inset.TCheckbutton", background=inset, foreground=ink, padding=(0, 3))
        style.map("Inset.TCheckbutton", background=[("active", inset)])
        style.configure("TEntry", fieldbackground=field, foreground=ink, bordercolor=border, lightcolor=border, darkcolor=border, padding=(8, 6))
        style.configure("TCombobox", fieldbackground=field, foreground=ink, arrowcolor=muted, bordercolor=border, lightcolor=border, darkcolor=border, padding=(6, 5))
        style.map("TCombobox", fieldbackground=[("readonly", field)], foreground=[("readonly", ink)], selectbackground=[("readonly", field)], selectforeground=[("readonly", ink)])
        style.configure("TSpinbox", fieldbackground=field, foreground=ink, arrowcolor=muted, bordercolor=border, lightcolor=border, darkcolor=border, padding=(6, 5))
        style.configure("TButton", background="#2a3038", foreground=ink, padding=(12, 7), font=("Segoe UI", 9), bordercolor=border)
        style.map("TButton", background=[("active", "#343c46"), ("pressed", "#3d4652")], foreground=[("disabled", "#727b86")])
        style.configure("Accent.TButton", background=accent, foreground="#ffffff", padding=(14, 7), font=("Segoe UI Semibold", 9))
        style.map("Accent.TButton", background=[("active", accent_hover), ("pressed", "#1f6feb")], foreground=[("disabled", "#89929d")])
        style.configure("Endpoint.TButton", background="#2a3038", foreground=ink, padding=(14, 7))
        style.configure("EndpointSelected.TButton", background="#17345a", foreground="#8ec5ff", padding=(14, 7))
        style.map("EndpointSelected.TButton", background=[("active", "#1d4474")])
        style.configure("Horizontal.TProgressbar", background=accent, troughcolor="#252b32", borderwidth=0)

    def _build(self, cfg: dict) -> None:
        self.window.rowconfigure(1, weight=1)
        self.window.columnconfigure(0, weight=1)

        header = ttk.Frame(self.window, padding=(22, 13), style="Header.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        title_block = ttk.Frame(header, style="Header.TFrame")
        title_block.pack(side="left", fill="x", expand=True)
        ttk.Label(title_block, text="SLS Mass Notify", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            title_block,
            text="PBX connections and notification preferences",
            style="HeaderHint.TLabel",
        ).pack(anchor="w", pady=(3, 0))
        header_meta = ttk.Frame(header, style="Header.TFrame")
        header_meta.pack(side="right", anchor="ne")
        ttk.Label(header_meta, text=f"Version {APP_VERSION}", style="Version.TLabel").pack(anchor="e", pady=(0, 7))
        enabled = bool(self.enabled_var.get())
        self.monitor_badge = ttk.Label(
            header_meta,
            text="Monitoring enabled" if enabled else "Monitoring disabled",
            style="BadgeOn.TLabel" if enabled else "BadgeOff.TLabel",
        )
        self.monitor_badge.pack(anchor="e")

        content = ttk.Frame(self.window, style="Surface.TFrame")
        content.grid(row=1, column=0, sticky="nsew", padx=16, pady=(14, 10))

        def make_scroll_area(parent: ttk.Frame) -> ttk.Frame:
            parent.rowconfigure(0, weight=1)
            parent.columnconfigure(0, weight=1)
            body = ScrollableBody(parent, style="Surface.TFrame")
            body.grid(row=0, column=0, sticky="nsew")
            return body.content

        mass_frame = make_scroll_area(content)

        overview = ttk.Frame(mass_frame, style="Surface.TFrame")
        overview.pack(fill="x", pady=(0, 16))
        overview.columnconfigure(0, weight=1, uniform="overview")
        overview.columnconfigure(1, weight=1, uniform="overview")

        general = ttk.Frame(overview, padding=16, style="Card.TFrame")
        general.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(general, text="App behavior", style="Section.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(
            general,
            text="Monitoring, Windows startup, and update downloads.",
            style="Hint.TLabel",
            wraplength=430,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(3, 13))
        ttk.Checkbutton(general, text="Enable background monitoring", variable=self.enabled_var, style="Card.TCheckbutton").grid(
            row=2, column=0, columnspan=4, sticky="w"
        )
        ttk.Checkbutton(general, text="Run at Windows startup", variable=self.startup_var, style="Card.TCheckbutton").grid(
            row=3, column=0, columnspan=4, sticky="w"
        )
        ttk.Checkbutton(
            general,
            text="Download available updates for administrator review",
            variable=self.auto_update_var,
            style="Card.TCheckbutton",
        ).grid(row=4, column=0, columnspan=4, sticky="w")
        general.columnconfigure(3, weight=1)

        audio = ttk.Frame(overview, padding=16, style="Card.TFrame")
        audio.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ttk.Label(audio, text="Notification sound", style="Section.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(
            audio,
            text="Choose the sound played for new notifications.",
            style="Hint.TLabel",
            wraplength=430,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(3, 15))
        ttk.Label(audio, text="Notification sound", style="Hint.TLabel").grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 4))
        self.audio_combo = ttk.Combobox(audio, textvariable=self.audio_var, values=list_audio_choices(), state="readonly", width=32)
        self.audio_combo.grid(row=3, column=0, columnspan=4, sticky="ew")
        audio_actions = ttk.Frame(audio, style="Panel.TFrame")
        audio_actions.grid(row=4, column=0, columnspan=4, sticky="w", pady=(11, 0))
        ttk.Button(audio_actions, text="Play sound", command=self.play_selected_audio).pack(side="left", padx=(0, 8))
        ttk.Button(audio_actions, text="Import WAV", command=self.import_audio).pack(side="left")
        ttk.Label(
            audio,
            text="Custom WAV files are stored with your app settings.",
            style="Hint.TLabel",
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(12, 0))
        audio.columnconfigure(0, weight=1)
        self.refresh_audio_choices()

        endpoints_frame = ttk.Frame(mass_frame, padding=16, style="Card.TFrame")
        endpoints_frame.pack(fill="both", expand=True, pady=(0, 12))
        endpoint_heading = ttk.Frame(endpoints_frame, style="Panel.TFrame")
        endpoint_heading.pack(fill="x")
        ttk.Label(endpoint_heading, text="PBX connections", style="Section.TLabel").pack(side="left", anchor="w")
        ttk.Label(endpoint_heading, text="Up to three connections", style="Hint.TLabel").pack(side="right", anchor="e")
        ttk.Label(
            endpoints_frame,
            text="Passwords are stored securely for your Windows account.",
            style="Hint.TLabel",
            wraplength=680,
        ).pack(anchor="w", pady=(3, 14))

        selector = ttk.Frame(endpoints_frame, style="Panel.TFrame")
        selector.pack(fill="x", pady=(0, 12))
        panel_host = ttk.Frame(endpoints_frame, style="Inset.TFrame", padding=18)
        panel_host.pack(fill="both", expand=True)
        panel_host.columnconfigure(0, weight=1)
        for index, endpoint in enumerate(normalize_endpoints(cfg)):
            button = ttk.Button(
                selector,
                text=f"PBX {index + 1}",
                style="EndpointSelected.TButton" if index == 0 else "Endpoint.TButton",
                command=lambda idx=index: self.show_endpoint(idx),
            )
            button.pack(side="left", padx=(0, 8))
            self.endpoint_buttons.append(button)
            group = ttk.Frame(panel_host, style="Inset.TFrame")
            group.grid(row=0, column=0, sticky="nsew")
            self.endpoint_panels.append(group)
            self._build_endpoint_tab(group, index, endpoint)
            if index != 0:
                group.grid_remove()

        footer = ttk.Frame(self.window, padding=(16, 10), style="StatusBar.TFrame")
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(
            footer,
            text=self.app.status_text,
            style="Status.TLabel",
            wraplength=500,
        )
        self.status_label.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))

        actions = ttk.Frame(footer, style="StatusBar.TFrame")
        actions.grid(row=1, column=0, columnspan=2, sticky="w")
        self.test_button = ttk.Button(actions, text="Test connection", command=self.test_now)
        action_buttons = [self.test_button,
            ttk.Button(actions, text="Save changes", style="Accent.TButton", command=self.save),
            ttk.Button(actions, text="Close", command=self.close),
            ttk.Button(actions, text="Exit", command=self.quit_app)]
        tools_row = ttk.Frame(footer, style="StatusBar.TFrame")
        tools_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tool_buttons = [ttk.Button(tools_row, text=label, command=command) for label, command in (
            ("History", self.app.show_history), ("Health", self.app.show_health),
            ("Export diagnostics", self.app.export_support_bundle),
            ("Local preview", self.app.preview_notification), ("Reconnect", self.app.reconnect_now))]
        def reflow(event):
            self.status_label.configure(wraplength=max(160, event.width - 32))
            for buttons in (action_buttons, tool_buttons):
                cell = max(button.winfo_reqwidth() for button in buttons) + 8
                columns = max(1, min(len(buttons), (event.width - 32) // cell))
                for index, button in enumerate(buttons):
                    button.grid(row=index // columns, column=index % columns, sticky="ew", padx=(0, 8), pady=2)
        footer.bind("<Configure>", reflow)
        for buttons in (action_buttons, tool_buttons):
            for index, button in enumerate(buttons):
                button.grid(row=0, column=index, padx=(0, 8))


    def _build_endpoint_tab(self, parent: ttk.Frame, index: int, endpoint: dict) -> None:
        form = {
            "name": StringVar(value=safe_string(endpoint.get("name")) or f"PBX {index + 1}"),
            "endpoint": StringVar(value=safe_string(endpoint.get("endpoint"))),
            "username": StringVar(value=safe_string(endpoint.get("username"))),
            "password": StringVar(value=unprotect_secret(safe_string(endpoint.get("password")))),
            "enabled": BooleanVar(value=bool(endpoint.get("enabled", index == 0))),
            "reconnect_automatically": BooleanVar(value=bool(endpoint.get("reconnect_automatically", True))),
            "show_password": BooleanVar(value=False),
            "password_entry": None,
            "warning_label": None,
        }
        self.endpoint_forms.append(form)

        ttk.Label(
            parent,
            text=f"PBX {index + 1}",
            style="Field.TLabel",
            font=("Segoe UI Semibold", 11),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Checkbutton(parent, text="Enabled", variable=form["enabled"], style="Inset.TCheckbutton").grid(
            row=0, column=3, sticky="e"
        )

        ttk.Label(parent, text="Name", style="Field.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Label(parent, text="PBX address", style="Field.TLabel").grid(
            row=1, column=1, columnspan=3, sticky="w"
        )
        ttk.Entry(parent, textvariable=form["name"], width=24).grid(
            row=2, column=0, sticky="ew", pady=(4, 0), padx=(0, 10)
        )
        ttk.Entry(parent, textvariable=form["endpoint"], width=64).grid(
            row=2, column=1, columnspan=3, sticky="ew", pady=(4, 0)
        )

        username_label = ttk.Label(parent, text="Username", style="Field.TLabel")
        username_label.grid(row=3, column=0, sticky="w", pady=(14, 0))
        username_entry = ttk.Entry(parent, textvariable=form["username"], width=28)
        username_entry.grid(row=4, column=0, sticky="ew", pady=(4, 0), padx=(0, 10))
        password_label = ttk.Label(parent, text="Password", style="Field.TLabel")
        password_label.grid(row=3, column=1, sticky="w", pady=(14, 0))
        password_entry = ttk.Entry(parent, textvariable=form["password"], width=36, show="*")
        password_entry.grid(row=4, column=1, columnspan=3, sticky="ew", pady=(4, 0))
        form["password_entry"] = password_entry

        options = ttk.Frame(parent, style="Inset.TFrame")
        options.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        ttk.Checkbutton(
            options,
            text="Reconnect automatically",
            variable=form["reconnect_automatically"],
            style="Inset.TCheckbutton",
        ).pack(side="left")
        ttk.Checkbutton(
            options,
            text="Show password",
            variable=form["show_password"],
            style="Inset.TCheckbutton",
            command=lambda idx=index: self.toggle_password(idx),
        ).pack(side="right")

        warning_label = ttk.Label(parent, text="", style="InsetHint.TLabel", wraplength=820)
        warning_label.grid(row=6, column=0, columnspan=4, sticky="w", pady=(10, 0))
        form["warning_label"] = warning_label
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.columnconfigure(2, weight=0)
        parent.columnconfigure(3, weight=0)
        form["endpoint"].trace_add("write", lambda *_args, idx=index: self.update_endpoint_warning(idx))
        self.update_endpoint_warning(index)

    def show_endpoint(self, index: int) -> None:
        if not 0 <= index < len(self.endpoint_panels):
            return
        self.selected_endpoint_index = index
        for panel_index, panel in enumerate(self.endpoint_panels):
            if panel_index == index:
                panel.grid()
            else:
                panel.grid_remove()
        for button_index, button in enumerate(self.endpoint_buttons):
            button.configure(style="EndpointSelected.TButton" if button_index == index else "Endpoint.TButton")

    def _center(self) -> None:
        fit_window(self.window, (1140, 840), minimum=(600, 400))

    def refresh_audio_choices(self) -> None:
        choices = list_audio_choices()
        selected = safe_audio_name(self.audio_var.get() or DEFAULT_AUDIO_NAME)
        if selected not in choices and choices:
            selected = choices[0]
        if self.audio_combo is not None:
            self.audio_combo.configure(values=choices)
        self.audio_var.set(selected)

    def play_selected_audio(self) -> None:
        name = safe_audio_name(self.audio_var.get() or DEFAULT_AUDIO_NAME)
        if not find_audio_file(name):
            messagebox.showerror("Alert Audio", f"Audio file was not found:\n\n{name}")
            return
        try:
            duration = play_alert_sound(name)
            self.status_label.configure(text=f"Playing {name} ({duration:.1f} seconds). Confirm you can hear it on this device.")
        except Exception as exc:
            messagebox.showerror("Alert Audio", f"Sound could not play: {exc}")

    def import_audio(self) -> None:
        selected = filedialog.askopenfilename(
            title="Import alert audio",
            filetypes=(("WAV audio", "*.wav"), ("All files", "*.*")),
        )
        if not selected:
            return
        try:
            audio_name = import_custom_audio(Path(selected))
            self.audio_var.set(audio_name)
            self.refresh_audio_choices()
            self.status_label.configure(text=f"Imported audio: {audio_name}")
        except Exception as exc:
            messagebox.showerror("Import Audio", f"Could not import audio:\n\n{exc}")

    def update_endpoint_warning(self, index: int) -> None:
        form = self.endpoint_forms[index]
        label = form.get("warning_label")
        if label is None:
            return
        address = form["endpoint"].get().strip()
        if not address:
            label.configure(text="HTTPS · certificate validation on", style="InsetHint.TLabel")
            return
        try:
            normalize_pbx_address(address)
        except ValueError as exc:
            label.configure(text=safe_string(exc), style="RedWarning.TLabel")
            return
        label.configure(text="HTTPS · certificate validation on", style="InsetHint.TLabel")

    def toggle_password(self, index: int) -> None:
        form = self.endpoint_forms[index]
        entry = form.get("password_entry")
        if entry is not None:
            entry.configure(show="" if form["show_password"].get() else "*")

    def collect_settings(self) -> list[dict] | None:
        endpoints: list[dict] = []
        active_count = 0
        for index, form in enumerate(self.endpoint_forms):
            name = form["name"].get().strip() or f"PBX {index + 1}"
            raw_url = form["endpoint"].get().strip()
            try:
                url = normalize_pbx_address(raw_url) if raw_url else ""
            except ValueError as exc:
                messagebox.showerror("PBX address", f"PBX profile {index + 1}: {exc}")
                return None
            username = form["username"].get().strip()
            password = form["password"].get()
            endpoint_enabled = bool(form["enabled"].get())

            if url and not endpoint_url_allowed(url):
                messagebox.showerror(
                    "PBX address",
                    f"PBX {index + 1} must use a valid HTTPS hostname or URL.",
                )
                return None
            if endpoint_enabled and url:
                if not username or not password:
                    messagebox.showerror(
                        "Username/password required",
                        f"PBX {index + 1} needs both a desktop username and password.",
                    )
                    return None
                active_count += 1

            endpoints.append(
                {
                    "name": name,
                    "endpoint": url,
                    "enabled": endpoint_enabled,
                    "auth_mode": AUTH_BASIC,
                    "delivery_mode": DELIVERY_LIVE,
                    "reconnect_automatically": bool(form["reconnect_automatically"].get()),
                    "username": username,
                    "password": password,
                    "credential_revision": int(self.app.get_endpoint_state(index, "credential_revision") or 0),
                    "last_event_id": self.app.get_endpoint_state(index, "last_event_id"),
                    "last_fingerprint": self.app.get_endpoint_state(index, "last_fingerprint"),
                    "recent_event_ids": normalize_endpoint(
                        normalize_endpoints(self.app.get_config())[index], index
                    ).get("recent_event_ids", []),
                }
            )

        if self.enabled_var.get() and active_count == 0:
            messagebox.showerror("No active endpoint", "Enable and configure at least one endpoint, or disable monitoring.")
            return None
        return endpoints

    def save(self) -> bool:
        collected = self.collect_settings()
        if collected is None:
            return False
        endpoints = collected
        try:
            self.app.update_settings(
                endpoints=endpoints,
                enabled=self.enabled_var.get(),
                startup_enabled=self.startup_var.get(),
                auto_update_enabled=self.auto_update_var.get(),
                audio_sound=safe_audio_name(self.audio_var.get() or DEFAULT_AUDIO_NAME),
            )
        except Exception as exc:
            messagebox.showerror("Save settings", f"Settings were not saved. Your previous configuration was retained.\n\n{exc}")
            return False
        if self.monitor_badge is not None:
            enabled = bool(self.enabled_var.get())
            self.monitor_badge.configure(
                text="Monitoring enabled" if enabled else "Monitoring disabled",
                style="BadgeOn.TLabel" if enabled else "BadgeOff.TLabel",
            )
        active_count = sum(bool(form["enabled"].get() and form["endpoint"].get().strip()) for form in self.endpoint_forms)
        self.status_label.configure(text=f"Settings saved. {active_count} endpoint{'s' if active_count != 1 else ''} active.")
        return True

    def test_now(self) -> None:
        if not self.save():
            return
        if self.test_button is not None:
            self.test_button.configure(state="disabled")
        self.status_label.configure(text="Checking authentication and live delivery activity...")
        self.app.test_now(self.finish_connection_test)

    def finish_connection_test(self, message: str) -> None:
        if not self.window.winfo_exists():
            return
        self.status_label.configure(text=message)
        if self.test_button is not None:
            self.test_button.configure(state="normal")

    def close(self) -> None:
        if self.app.tray is not None and not self.app.tray.available:
            self.window.iconify()
            return
        self.window.destroy()
        self.app.settings_window = None

    def quit_app(self) -> None:
        self.app.shutdown()


def endpoint_worker_signature(endpoint_cfg: dict) -> str:
    keys = (
        "endpoint",
        "enabled",
        "username",
        "password",
        "credential_revision",
        "reconnect_automatically",
    )
    selected = {key: endpoint_cfg.get(key) for key in keys}
    return json.dumps(selected, sort_keys=True, separators=(",", ":"))


def notification_routed_to_client(payload: object, endpoint_cfg: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("desktop_all") is True:
        return True
    username = safe_string(endpoint_cfg.get("username"))
    recipients = payload.get("desktop_recipients")
    if isinstance(recipients, str):
        recipients = [recipients]
    if isinstance(recipients, list):
        return username in {safe_string(item) for item in recipients}
    return False


def reconciliation_records(data: object, last_event_id: str) -> list[dict]:
    return ordered_reconciliation(data, last_event_id)


class EndpointTransportWorker:
    def __init__(self, app: "MassNotifyApp", index: int, endpoint_cfg: dict) -> None:
        self.app = app
        self.index = index
        self.endpoint_cfg = normalize_endpoint(endpoint_cfg, index)
        self.signature = endpoint_worker_signature(self.endpoint_cfg)
        self.stop_event = threading.Event()
        self.response_lock = threading.Lock()
        self.response = None
        self.last_activity = 0.0
        self.last_stream_signal = 0.0
        self.stream_ready = threading.Event()
        self.session_id = ""
        self.client_id = ""
        self.server_retry_seconds = 1.0
        self.authenticated_event = threading.Event()
        self.auth_lock = threading.RLock()
        self.auth_generation = 0
        self.revoked_event = threading.Event()
        self.receipt_thread: threading.Thread | None = None
        self.thread = threading.Thread(
            target=self.run,
            name=f"PBXTransport-{index + 1}",
            daemon=True,
        )

    def start(self) -> None:
        self.receipt_thread = threading.Thread(
            target=self._run_receipts,
            name=f"PBXReceipts-{self.index + 1}",
            daemon=True,
        )
        self.receipt_thread.start()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._invalidate_auth()
        self.authenticated_event.set()
        with self.response_lock:
            response = self.response
            self.response = None
        shutdown_socket(response_socket(response))

    def _invalidate_auth(self, *, revoked: bool = False, generation: int | None = None) -> None:
        with self.auth_lock:
            if generation is not None and generation != self.auth_generation:
                return
            if revoked:
                self.revoked_event.set()
            self.auth_generation += 1
            self.authenticated_event.clear()
            self.last_stream_signal = 0.0
            self.stream_ready.clear()
        if revoked:
            with self.response_lock:
                response = self.response
            shutdown_socket(response_socket(response))

    def _authenticated_generation(self) -> int | None:
        with self.auth_lock:
            if (self.revoked_event.is_set() or not self.authenticated_event.is_set()
                    or not self._is_current()):
                return None
            return self.auth_generation

    def _generation_is_current(self, generation: int) -> bool:
        return generation == self._authenticated_generation()

    def _track_response(self, response) -> None:
        with self.response_lock:
            self.response = response

    def _release_response(self, response) -> None:
        with self.response_lock:
            if self.response is response:
                self.response = None
        try:
            response.close()
        except Exception:
            pass

    def _status(self, state: str, detail: str) -> None:
        if not self._is_current():
            return
        self.app.set_transport_status(self.index, state, detail)

    def _is_current(self) -> bool:
        if self.stop_event.is_set():
            return False
        if hasattr(self.app, "workers"):
            with self.app.worker_lock:
                return self.app.workers.get(self.index) is self
        return True

    def _fault(self, message: str) -> None:
        if not self._is_current():
            return
        key = f"endpoint-{self.index}"
        self.app.record_fault(key, message)
        log(message)

    def run(self) -> None:
        if 0 < self.endpoint_cfg.get("live_polling_until", 0) - time.time() <= 86400:
            self._run_live_polling()
            return
        self._run_live()

    def _run_live(self) -> None:
        name = endpoint_display_name(self.index, self.endpoint_cfg)
        backoff_index = 0
        while not self.stop_event.is_set():
            if self.revoked_event.is_set():
                self._status("AUTH_FAILED", f"{name}: credentials require review before reconnecting")
                self.stop_event.wait()
                return
            self.authenticated_event.clear()
            authenticated_this_attempt = False
            self.authenticated_this_attempt = False
            self._status("CONNECTING", f"{name}: opening authenticated live stream")
            try:
                self.app.inbox.expire_active(profile_namespace(self.endpoint_cfg), include_displayed=False)
                response = open_sse_response(self.endpoint_cfg)
                self._track_response(response)
                try:
                    authenticated_this_attempt, reconnect_requested = self._consume_stream(response)
                finally:
                    self.authenticated_event.clear()
                    self._release_response(response)
                if self.stop_event.is_set():
                    return
                if not authenticated_this_attempt:
                    raise StreamProtocolError("The stream ended before the authenticated handshake.")
                backoff_index = 0
                if not bool(self.endpoint_cfg.get("reconnect_automatically", True)):
                    self._status("DISCONNECTED", f"{name}: live stream closed; automatic reconnect is off")
                    self.stop_event.wait()
                    return
                delay = secrets.SystemRandom().uniform(0.1, 0.8) if reconnect_requested else self.server_retry_seconds
                self._status("RECONNECTING", f"{name}: renewing live stream")
                if self.stop_event.wait(delay):
                    return

                continue
            except UnauthorizedError as exc:
                self._invalidate_auth(revoked=True)
                if self.stop_event.is_set():
                    return
                message = f"{name}: {exc}"
                self._status("AUTH_FAILED", message)
                self._fault(message)
                self.stop_event.wait()
                return
            except TlsRequiredError as exc:
                if self.stop_event.is_set():
                    return
                message = f"{name}: {exc}"
                self._status("TLS_REQUIRED", message)
                self._fault(message)
                self.stop_event.wait()
                return
            except RateLimitedError as exc:
                if self.stop_event.is_set():
                    return
                message = f"{name}: {exc}"
                self._status("RATE_LIMITED", message)
                self._fault(message)
                if self.stop_event.wait(exc.retry_after):
                    return
                self._run_live_polling()
                return
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                if self.revoked_event.is_set():
                    self._status("AUTH_FAILED", f"{name}: credentials require review before reconnecting")
                    self.stop_event.wait()
                    return
                if self.authenticated_this_attempt:
                    backoff_index = 0
                message = f"{name}: live transport fault: {safe_string(exc)}"
                self._fault(message)
                if not bool(self.endpoint_cfg.get("reconnect_automatically", True)):
                    self._status("DISCONNECTED", message)
                    self.stop_event.wait()
                    return
                if self.authenticated_this_attempt:
                    self._run_live_polling()
                    return
                delay = RECONNECT_BACKOFF_SECONDS[min(backoff_index, len(RECONNECT_BACKOFF_SECONDS) - 1)]
                backoff_index = min(backoff_index + 1, len(RECONNECT_BACKOFF_SECONDS) - 1)
                delay = min(30.0, delay + secrets.SystemRandom().uniform(0.0, max(0.25, delay * 0.2)))
                self._status("RECONNECTING", f"{message}; retrying in {delay:.1f}s")
                if self.stop_event.wait(delay):
                    return

    def _run_live_polling(self) -> None:
        """Fallback for blocked/buffered SSE; never replay an initial snapshot."""
        baseline = LiveSnapshotBaseline()
        name = endpoint_display_name(self.index, self.endpoint_cfg)
        if hasattr(self.app, "remember_polling_fallback"):
            self.app.remember_polling_fallback(self.index, self.signature)
        self._invalidate_auth()
        self.app.inbox.expire_active(profile_namespace(self.endpoint_cfg), include_displayed=False)
        self._status("CONNECTING", f"{name}: establishing live polling after a stream failure")
        while self._is_current():
            delay = 5.0
            try:
                if self.revoked_event.is_set():
                    raise UnauthorizedError("Desktop credentials require review before reconnecting.")
                data, _raw, server_time = fetch_endpoint(pbx_recent_url(self.endpoint_cfg),
                    endpoint_auth_headers(self.endpoint_cfg), include_server_time=True)
                ensure_api_ok(data)
                records = ordered_reconciliation(data)
                if not self._is_current():
                    return
                fresh = baseline.observe(records, server_time, time.time())
                with self.auth_lock:
                    if self.revoked_event.is_set():
                        raise UnauthorizedError("Desktop credentials were revoked or changed.")
                    if not self._is_current():
                        return
                    self.authenticated_event.set()
                    self.last_activity = self.last_stream_signal = time.monotonic()
                    self.stream_ready.set()
                    generation = self.auth_generation
                self.app.clear_fault(f"endpoint-{self.index}")
                self._status("POLLING", f"{name}: receiving new announcements through live polling (5 seconds)")
                for payload in fresh:
                    self._accept_payload(payload, json.dumps(payload), delivery_source="live polling",
                                         auth_generation=generation)
            except UnauthorizedError as exc:
                self._invalidate_auth(revoked=True)
                self._fault(str(exc))
                self._status("AUTH_FAILED", f"{name}: credentials require review")
                self.stop_event.wait()
                return
            except Exception as exc:
                baseline.reset()
                self._invalidate_auth()
                if not self._is_current():
                    return
                self.app.inbox.expire_active(profile_namespace(self.endpoint_cfg), include_displayed=False)
                self._fault(f"{name}: live polling interrupted: {exc}")
                self._status("RECONNECTING", f"{name}: live polling interrupted; missed messages will be skipped")
                delay = exc.retry_after if isinstance(exc, RateLimitedError) else 15.0
            if self.stop_event.wait(delay):
                return

    def _run_receipts(self) -> None:
        """Send saved device receipts; never fetch historical notifications."""
        while not self.stop_event.is_set():
            if not self.authenticated_event.wait(1):
                continue
            if self.stop_event.is_set():
                return
            generation = self._authenticated_generation()
            if generation is None:
                continue
            delay = 2.0
            try:
                if not self._generation_is_current(generation):
                    continue
                namespace = profile_namespace(self.endpoint_cfg)
                for receipt in self.app.inbox.pending_receipts(namespace, limit=1):
                    try:
                        if not self._generation_is_current(generation):
                            break
                        send_receipt_ack(self.endpoint_cfg, receipt["event_id"])
                        if not self._generation_is_current(generation):
                            break
                        self.app.inbox.receipt_sent(receipt["receipt_id"])
                        self.app.clear_fault(f"receipt-{self.index}")
                    except Exception as exc:
                        self.app.inbox.receipt_failed(receipt["receipt_id"], str(exc))
                        self.app.record_fault(f"receipt-{self.index}", "PBX receipt synchronization failed; saved receipts will retry.")
                        raise
            except UnauthorizedError as exc:
                self._invalidate_auth(revoked=True)
                self._fault(str(exc))
                self._status("AUTH_FAILED", str(exc))
                self.stop_event.wait()
                return
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                if not self._generation_is_current(generation):
                    continue
                self.app.record_fault(f"receipt-{self.index}", f"Receipt synchronization failed: {exc}")
                log(f"PBX {self.index + 1} receipt synchronization failed: {exc}")
                delay = exc.retry_after if isinstance(exc, RateLimitedError) else 15.0
            if self.stop_event.wait(delay):
                return

    def _consume_stream(self, response) -> tuple[bool, bool]:
        self._invalidate_auth()
        with self.auth_lock:
            if self.revoked_event.is_set():
                raise UnauthorizedError("Desktop credentials require review before reconnecting.")
            generation = self.auth_generation
        try:
            return self._consume_authenticated_stream(response, generation)
        finally:
            self._invalidate_auth(generation=generation)

    def _consume_authenticated_stream(self, response, generation: int) -> tuple[bool, bool]:
        deadline = time.monotonic() + SSE_TEST_TIMEOUT_SECONDS
        watchdog = SocketDeadline(response_socket(response), SSE_TEST_TIMEOUT_SECONDS,
                                  deadline=deadline, message="PBX stream activity exceeded its deadline")

        def activity() -> None:
            self.last_activity = time.monotonic()
            if self.last_activity > deadline:
                raise StreamProtocolError("PBX authenticated handshake exceeded its deadline")

        try:
            with watchdog:
                return self._read_authenticated_stream(response, generation, activity, watchdog)
        except TimeoutError as exc:
            raise StreamProtocolError("PBX stream authentication or heartbeat exceeded its deadline") from exc

    def _read_authenticated_stream(self, response, generation: int, activity, watchdog) -> tuple[bool, bool]:
        authenticated = False
        reconnect_requested = False
        last_wall_activity = time.time()

        def complete_activity() -> None:
            nonlocal last_wall_activity
            now = time.time()
            if authenticated and now - last_wall_activity >= SSE_READ_TIMEOUT_SECONDS:
                raise StreamProtocolError("Live stream was interrupted; buffered notifications discarded")
            last_wall_activity = now
            # Before authentication the fixed deadline cannot be renewed. After
            # authentication, only a complete SSE line counts as stream activity.
            if authenticated:
                self.last_activity = time.monotonic()
                watchdog.renew(self.last_activity + SSE_READ_TIMEOUT_SECONDS)
            else:
                activity()

        for event in iter_sse_events(response, on_activity=complete_activity, stop_event=self.stop_event,
                                     emit_heartbeats=True):
            if watchdog.expired:
                raise StreamProtocolError("PBX stream activity exceeded its deadline")
            if self.revoked_event.is_set():
                raise UnauthorizedError("Desktop credentials were revoked or changed.")
            if event.retry_ms is not None:
                self.server_retry_seconds = max(0.1, event.retry_ms / 1000.0)
            if event.name == "authenticated":
                if authenticated:
                    raise StreamProtocolError("PBX sent a duplicate authenticated event.")
                try:
                    payload = json.loads(event.data)
                except json.JSONDecodeError as exc:
                    raise StreamProtocolError("PBX sent an invalid authenticated event.") from exc
                if (
                    not isinstance(payload, dict)
                    or payload.get("ok") is not True
                    or safe_string(payload.get("transport")) != "live_sse"
                    or payload.get("protocol_version", 2) != 2
                ):
                    raise StreamProtocolError("PBX rejected the live authentication handshake.")
                with self.auth_lock:
                    if (self.revoked_event.is_set() or generation != self.auth_generation
                            or not self._is_current()):
                        raise UnauthorizedError("Desktop authentication is no longer active.")
                    if event.explicit_id and event.event_id and hasattr(self.app, "inbox"):
                        self.app.inbox.advance_cursor(profile_namespace(self.endpoint_cfg), event.event_id, force=True)
                        self.endpoint_cfg["last_stream_id"] = self.app.inbox.cursor(profile_namespace(self.endpoint_cfg))
                    authenticated = True
                    self.authenticated_this_attempt = True
                    self.authenticated_event.set()
                set_stream_read_timeout(response, SSE_READ_TIMEOUT_SECONDS)
                watchdog.renew(self.last_activity + SSE_READ_TIMEOUT_SECONDS)
                self.session_id = bounded_text(payload.get("session_id"), 160)
                self.client_id = bounded_text(payload.get("client_id"), 160)
                self.app.clear_fault(f"endpoint-{self.index}")
                log(f"{endpoint_display_name(self.index, self.endpoint_cfg)} live handshake authenticated")
                self._status("CONNECTING", f"{endpoint_display_name(self.index, self.endpoint_cfg)}: authenticated; waiting for stream activity")
                continue
            if event.name == "_heartbeat" and not authenticated:
                continue
            if not authenticated:
                raise StreamProtocolError(f"PBX sent '{event.name}' before authentication completed.")
            if event.name in {"_heartbeat", "notification"}:
                first_signal = not self.stream_ready.is_set()
                self.last_stream_signal = time.monotonic()
                self.stream_ready.set()
                if first_signal:
                    self._status("AUTHENTICATED", f"{endpoint_display_name(self.index, self.endpoint_cfg)}: connected with live stream activity")
            if event.name == "notification":
                try:
                    payload = json.loads(event.data)
                except json.JSONDecodeError:
                    self._quarantine_payload(event.data, event.event_id if event.explicit_id else "", "PBX sent invalid notification JSON.", auth_generation=generation)
                    continue
                self._accept_payload(payload, event.data, event.event_id if event.explicit_id else "", delivery_source="live stream", auth_generation=generation)
            elif event.name == "reconnect":
                reconnect_requested = True
                break
            elif event.name == "cursor_reset":
                # A live-only connection never asks for replay. Do not consume
                # retained events if a server unexpectedly enters recovery mode.
                reconnect_requested = True
                break
            elif event.name == "revoked":
                self._invalidate_auth(revoked=True)
                raise UnauthorizedError("Desktop credentials were revoked or changed. Save updated credentials to reconnect.")
        return authenticated, reconnect_requested

    def _accept_payload(
        self,
        payload: object,
        raw_text: str,
        stream_event_id: str = "",
        *,
        delivery_source: str = "live stream",
        auth_generation: int | None = None,
    ) -> bool:
        # Serialize resume queue retirement with the current-worker check and
        # receipt transaction, so an old socket cannot refill the cleared queue.
        with self.auth_lock, getattr(self.app, "config_lock", nullcontext()):
            if (self.revoked_event.is_set() or not self._is_current()
                    or (auth_generation is not None and not self._generation_is_current(auth_generation))):
                return False
            return self._accept_current_payload(payload, raw_text, stream_event_id, delivery_source=delivery_source)

    def _accept_current_payload(self, payload: object, raw_text: str, stream_event_id: str,
                                *, delivery_source: str) -> bool:
        if self.revoked_event.is_set() or not self._is_current():
            return False
        try:
            validate_payload(payload)
        except (ValueError, TypeError) as exc:
            self._quarantine_payload(payload, stream_event_id, str(exc))
            return False
        if not notification_routed_to_client(payload, self.endpoint_cfg):
            event_id = safe_string(lookup(payload, EVENT_ID_KEYS)) if isinstance(payload, dict) else ""
            log(
                f"{endpoint_display_name(self.index, self.endpoint_cfg)} ignored {delivery_source} notification"
                f"{f' {event_id}' if event_id else ''} outside its desktop route"
            )
            return False
        alert = extract_alert(payload, raw_text)
        if not alert.event_id:
            alert.event_id = stream_event_id or alert.fingerprint
        if stream_event_id and stream_event_id != alert.event_id:
            self._quarantine_payload(payload, stream_event_id, "SSE ID does not match its notification ID.")
            return False
        alert.stream_id = validate_id(stream_event_id)
        normalize_alert_urls(alert, safe_string(self.endpoint_cfg.get("endpoint")))
        accepted = self.app.accept_alert(self.index, alert, self.signature)
        if accepted:
            log(
                f"{endpoint_display_name(self.index, self.endpoint_cfg)} accepted {delivery_source} "
                f"{alert.kind or 'notification'}{f' {alert.event_id}' if alert.event_id else ''}"
            )
        if alert.stream_id and hasattr(self.app, "inbox"):
            self.endpoint_cfg["last_stream_id"] = self.app.inbox.cursor(profile_namespace(self.endpoint_cfg))
        if accepted and alert.event_id:
            # The same worker reconnects after the PBX's bounded stream closes, so keep its
            # request snapshot current as well as the persisted profile state.
            self.endpoint_cfg["last_event_id"] = alert.event_id
        return accepted

    def _quarantine_payload(self, payload: object, stream_id: str, error: str,
                            *, auth_generation: int | None = None) -> None:
        with self.auth_lock:
            if (self.revoked_event.is_set() or not self._is_current()
                    or (auth_generation is not None and not self._generation_is_current(auth_generation))):
                return
            if not hasattr(self.app, "reject_payload"):
                raise StreamProtocolError(error)
            self.app.reject_payload(self.index, payload, stream_id, error, self.signature)
            self.endpoint_cfg["last_stream_id"] = self.app.inbox.cursor(profile_namespace(self.endpoint_cfg))


class ControlQueue(queue.Queue):
    """Control notifications are bounded; actual alert contents live in SQLite."""
    def __init__(self) -> None:
        super().__init__(maxsize=128)

    def put(self, item, block=True, timeout=None):
        with self.not_full:
            if self._qsize() >= self.maxsize:
                # Remove an obsolete status update, never an alert receipt.
                for index, queued in enumerate(self.queue):
                    if queued[0] == "status":
                        del self.queue[index]
                        break
                else:
                    if item[0] == "status":
                        return
                    self.queue.popleft()
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()


class MassNotifyApp:
    def __init__(self, root: Tk, show_settings_on_start: bool) -> None:
        self.root = root
        self.root.withdraw()
        self.root.title(APP_DISPLAY_NAME)
        apply_window_icon(self.root)
        icon = resource_path("favicon.ico")

        self.config_lock = threading.RLock()
        self.config = load_config()
        self.inbox = Inbox(CONFIG_DIR / "notifications.sqlite3")
        self.inbox.expire_active()
        self.inbox.prune(retention_days=int(self.config.get("retention_days", 30)))
        self._last_delivery_tick = time.time()
        self._scheduled_records: set[str] = set()
        self.stop_event = threading.Event()
        self.wakeup_event = threading.Event()
        self.connection_test_event = threading.Event()
        self.worker_lock = threading.RLock()
        self.workers: dict[int, EndpointTransportWorker] = {}
        self.faults_lock = threading.RLock()
        self.ui_queue: queue.Queue[tuple[str, object]] = ControlQueue()
        self.settings_window: SettingsWindow | None = None
        self.status_text = "Waiting for PBX connection."
        self.transport_statuses: dict[int, tuple[str, str]] = {}
        self.faults: dict[str, dict] = {}
        self.presenter = AlertPresenter(root,
            on_displayed=self.alert_displayed, on_acknowledged=self.alert_acknowledged,
            on_failed=self.alert_failed,
            play_sound=self.play_notification_sound,
            stop_sound=lambda: winsound.PlaySound(None, 0) if winsound else None,
            image_loader=lambda alert: fetch_image_bytes(alert.image_url, source_endpoint=alert.source_endpoint),
            can_display=self.can_display_record,
            on_discarded=self._scheduled_records.discard)
        self.tray = TrayIcon(
            on_open=lambda: self.ui_queue.put(("settings", None)),
            on_history=lambda: self.ui_queue.put(("history", None)),
            on_diagnostics=lambda: self.ui_queue.put(("health", None)),
            on_exit=lambda: self.ui_queue.put(("shutdown", None)), icon_path=icon if icon.exists() else None)
        if not self.tray.start():
            self.record_fault("tray", f"Notification tray icon is unavailable: {self.tray.error}")
            show_settings_on_start = True

        self.command_thread = threading.Thread(target=self.command_server, name="CommandServer", daemon=True)
        self.command_thread.start()

        self.monitor_thread = threading.Thread(target=self.monitor_loop, name="PBXMonitor", daemon=True)
        self.monitor_thread.start()

        self.update_thread = threading.Thread(target=self.update_loop, name="AutoUpdater", daemon=True)
        self.update_thread.start()

        self.root.after(200, self.process_ui_queue)

        if show_settings_on_start or not self.is_configured():
            self.root.after(250, self.show_settings)

    def can_display_record(self, record_id: str) -> bool:
        self.check_delivery_resume()
        if record_id.startswith("preview-"):
            return True
        record = self.inbox.record(record_id)
        cfg = self.get_config()
        return bool(record and cfg.get("enabled", True)
                    and record["state"] in {"pending", "displayed"}
                    and (record["expires"] is None or record["expires"] > time.time())
                    and record["namespace"] in {profile_namespace(p) for _, p in active_endpoints(cfg)})

    def play_notification_sound(self, _alert) -> float:
        try:
            duration = play_alert_sound(self.get_config().get("audio_sound", DEFAULT_AUDIO_NAME))
            self.clear_fault("audio")
            return duration
        except Exception as exc:
            self.record_fault("audio", f"Notification sound could not play: {exc}")
            log(f"Notification sound failed: {exc}")
            return 0.0

    def alert_displayed(self, record_id: str) -> None:
        if not record_id.startswith("preview-"):
            self.inbox.mark(record_id, "displayed")

    def alert_acknowledged(self, record_id: str, response: str) -> None:
        if not record_id.startswith("preview-"):
            if response in {"acknowledged", "safe", "need_help", "evacuated"}:
                # The published API supports device receipts only. This is a
                # local read acknowledgment, subject to normal history retention.
                self.inbox.mark(record_id, "acknowledged")
            else:
                self.inbox.mark(record_id, response if response in Inbox.TERMINAL else "dismissed")
        self._scheduled_records.discard(record_id)

    def alert_failed(self, record_id: str, error: str) -> None:
        if not record_id.startswith("preview-"):
            self.inbox.failed(record_id, safe_string(error))
        self._scheduled_records.discard(record_id)
        self.record_fault("presentation", "A notification could not be displayed; it will be retried.")
        log(f"Notification rendering failed: {error}")

    def pump_inbox(self) -> None:
        try:
            self.check_delivery_resume()
            cfg = self.get_config()
            if not cfg.get("enabled", True):
                return
            namespaces = {profile_namespace(profile) for _, profile in active_endpoints(cfg)}
            for record in self.inbox.pending(limit=16, excluding=self._scheduled_records, namespaces=namespaces):
                record_id = record["record_id"]
                if record_id in self._scheduled_records or record["namespace"] not in namespaces:
                    continue
                alert = AlertData(**record["payload"])
                expires, effective = alert_expiry(alert), timestamp(alert.effective)
                if expires is not None and expires <= time.time():
                    self.inbox.mark(record_id, "expired")
                    continue
                if effective is not None and effective > time.time():
                    self.inbox.defer(record_id, effective)
                    continue
                alert.incident_id = f"{record['namespace']}:{alert.incident_id}" if alert.incident_id else ""
                if alert.action in {"cancel", "all_clear"}:
                    self.presenter.cancel(alert.incident_id)
                    if alert.action == "cancel":
                        self.inbox.mark(record_id, "cancelled")
                        continue
                    alert.kind = "announcement"
                    alert.priority = "notice"
                self._scheduled_records.add(record_id)
                try:
                    submitted = self.presenter.submit(record_id, alert)
                except Exception as exc:
                    self.alert_failed(record_id, str(exc))
                    continue
                if not submitted:
                    self._scheduled_records.discard(record_id)
                    break
            self.clear_fault("storage")
        except Exception as exc:
            self.record_fault("storage", f"Local notification storage needs attention: {exc}")

    def check_delivery_resume(self) -> None:
        """Tk-thread guard: retire the queue and sockets after sleep/UI suspension.

        This also runs before presentation callbacks, which may precede the first
        inbox pump after Windows resumes. Workers refuse delivery during the gap.
        """
        now = time.time()
        with self.config_lock:
            previous = getattr(self, "_last_delivery_tick", now)
            if 0 <= now - previous < DELIVERY_PAUSE_SECONDS:
                self._last_delivery_tick = now
                return
            self.inbox.expire_active()
            with self.worker_lock:
                workers = list(self.workers.values())
                self.workers.clear()
            self._last_delivery_tick = now
        for worker in workers:
            worker.stop()
        self.wakeup_event.set()

    def evaluate_faults(self) -> None:
        now = time.monotonic()
        with self.faults_lock:
            for fault in self.faults.values():
                if not fault.get("notified") and now - fault["started_at"] >= FAULT_NOTIFY_SECONDS:
                    fault["notified"] = True
                    self.ui_queue.put(("fault", fault["message"]))

    def show_history(self) -> None:
        self.presenter.show_history(self.inbox.history())

    def show_health(self) -> None:
        with self.worker_lock:
            lines = [f"PBX {index + 1}: {state}\n{detail}" for index, (state, detail) in self.transport_statuses.items()]
        with self.faults_lock:
            lines.extend(str(fault["message"]) for fault in self.faults.values())
        lines.append("Inbox: " + json.dumps(self.inbox.statistics()))
        for rejected in self.inbox.quarantined():
            lines.append(f"Rejected notification {rejected['event_id']}: {rejected['error']}")
        lines.append("Receipt ACKs retry automatically. 'I have read this alert' is stored locally; human responses require a separate PBX API.")
        cfg = self.get_config()
        if cfg.get("downloaded_update_tag"):
            lines.append(f"Update {cfg['downloaded_update_tag']} downloaded for administrator review: {cfg.get('downloaded_update_path', '')}")
        if cfg.get("last_update_error"):
            lines.append(f"Update check: {cfg['last_update_error']}")
        RawTextWindow(self.root, "Notification health", "\n\n".join(lines) or "No profiles configured.")

    def export_support_bundle(self) -> None:
        filename = filedialog.asksaveasfilename(title="Save diagnostics", defaultextension=".json", filetypes=[("Diagnostics", "*.json")])
        if filename:
            try:
                export_diagnostics(Path(filename), version=APP_VERSION, statuses=dict(self.transport_statuses),
                    counts=self.inbox.statistics(), update_error=self.get_config().get("last_update_error", ""))
            except OSError as exc:
                messagebox.showerror("Diagnostics", str(exc))

    def reconnect_now(self) -> None:
        with self.worker_lock:
            workers = list(self.workers.values())
            self.workers.clear()
        for worker in workers:
            worker.stop()
        self.wakeup_event.set()

    def preview_notification(self) -> None:
        self.present_alert(extract_alert({"kind": "announcement", "title": "Local notification preview",
            "body": "This is a local display and sound test. No notification was sent to the PBX or other devices.",
            "priority": "notice", "is_test": True, "test_only": True}, ""))

    def is_configured(self) -> bool:
        with self.config_lock:
            return bool(active_endpoints(self.config))

    def get_config(self) -> dict:
        with self.config_lock:
            return normalize_config(dict(self.config))

    def get_endpoint_state(self, index: int, key: str) -> str:
        with self.config_lock:
            endpoints = normalize_endpoints(self.config)
            if not 0 <= index < len(endpoints):
                return ""
            endpoint = endpoints[index]
            if key == "last_stream_id" and hasattr(self, "inbox"):
                return self.inbox.cursor(profile_namespace(endpoint))
            return safe_string(endpoint.get(key, ""))

    def update_settings(self, *, endpoints: list[dict], enabled: bool,
                        startup_enabled: bool, auto_update_enabled: bool, audio_sound: str) -> None:
        staged_targets = []
        retired_targets = []
        with self.config_lock:
            previous = normalize_endpoints(self.config)
            new_profiles = normalize_endpoints({"endpoints": endpoints})
            try:
                for index, candidate in enumerate(new_profiles):
                    old = previous[index]
                    secret = candidate.get("password", "")
                    # Settings collects exact plaintext; stored credential references are never reused as passwords.
                    if secret == unprotect_secret(old.get("password", "")):
                        candidate["password"] = old.get("password", "")
                        candidate["credential_revision"] = old.get("credential_revision", 0)
                    else:
                        candidate["credential_revision"] = int(old.get("credential_revision", 0)) + 1
                        if secret and is_windows():
                            target = profile_credential_target(index, "password") + "/" + secrets.token_hex(12)
                            try:
                                _write_windows_credential(target, secret)
                                staged_targets.append(target)
                                candidate["password"] = "cred:" + target
                            except OSError:
                                candidate["password"] = protect_secret(secret)
                        else:
                            candidate["password"] = protect_secret(secret)
                        old_secret = old.get("password", "")
                        if old_secret.startswith("cred:"):
                            retired_targets.append(old_secret[5:])
                    if profile_namespace(old) != profile_namespace(candidate):
                        for key in ("last_event_id", "last_stream_id", "last_fingerprint"):
                            candidate[key] = ""
                        candidate["recent_event_ids"] = []
                    else:
                        # Preserve state accepted while Settings was open.
                        for key in ("last_event_id", "last_stream_id", "last_fingerprint", "recent_event_ids", "live_polling_until"):
                            candidate[key] = old.get(key, [] if key == "recent_event_ids" else "")
                updated = normalize_config(dict(self.config, endpoints=new_profiles, enabled=bool(enabled),
                    startup_enabled=bool(startup_enabled), auto_update_enabled=bool(auto_update_enabled),
                    audio_sound=safe_audio_name(audio_sound or DEFAULT_AUDIO_NAME)))
                save_config(updated)
                self.config = updated
            except Exception:
                for target in staged_targets:
                    try:
                        _delete_windows_credential(target)
                    except OSError:
                        log("Could not remove an unused staged credential")
                raise
        for target in retired_targets:
            try:
                _delete_windows_credential(target)
            except OSError:
                log("Could not remove a retired credential")
        set_startup_enabled(startup_enabled)
        self.wakeup_event.set()

    def command_server(self) -> None:
        def command(value):
            self.ui_queue.put(("shutdown" if value == "SHUTDOWN" else "settings", None))
        try:
            sls_ipc.serve(CONFIG_DIR, self.stop_event, command, protect_secret)
        except Exception as exc:
            self.record_fault("ipc", f"Local control unavailable: {exc}")

    def record_fault(self, key: str, message: str) -> None:
        with self.faults_lock:
            is_new = key not in self.faults
            fault = self.faults.setdefault(key, {"started_at": time.monotonic(), "notified": False})
            fault["message"] = message
        if is_new:
            self.ui_queue.put(("status", self.status_text))

    def clear_fault(self, key: str | None = None) -> None:
        with self.faults_lock:
            changed = bool(self.faults) if key is None else key in self.faults
            if key is None:
                self.faults.clear()
            else:
                self.faults.pop(key, None)
        if changed:
            self.ui_queue.put(("status", self.status_text))

    def set_transport_status(self, index: int, state: str, detail: str) -> None:
        with self.worker_lock:
            self.transport_statuses[index] = (state, detail)
            states = [value[0] for value in self.transport_statuses.values()]
            live_count = sum(state in {"AUTHENTICATED", "POLLING"} for state in states)
            if live_count == len(states) and live_count:
                self.status_text = f"Connected: {live_count}/{len(states)} PBX profiles."
                if "POLLING" in states:
                    self.status_text += " Live polling active (5-second checks)."
            elif live_count:
                self.status_text = f"Degraded: {live_count}/{len(states)} PBX profiles connected. {detail}"
            else:
                self.status_text = detail
        self.ui_queue.put(("status", self.status_text))

    def accept_alert(self, index: int, alert: AlertData, worker_signature: str = "") -> bool:
        with self.config_lock:
            now = time.time()
            gap = now - getattr(self, "_last_delivery_tick", now)
            if gap < 0 or gap >= DELIVERY_PAUSE_SECONDS:
                return False  # The UI must discard the suspended session first.
            if not self.config.get("enabled", True) or self.stop_event.is_set():
                return False
            endpoints = self.config["endpoints"]
            if not 0 <= index < len(endpoints):
                return False
            endpoint = endpoints[index]
            if not endpoint.get("enabled", True):
                return False
            if worker_signature and endpoint_worker_signature(endpoint) != worker_signature:
                return False
            namespace = profile_namespace(endpoint)
            event_id = validate_id(alert.event_id or alert.fingerprint, optional=False)
            if alert.kind.lower() == "announcement":
                published = timestamp(alert.created_at)
                # Missing/future sender times cannot extend the local queue.
                start = min(published, now) if published is not None else now
                alert.delivery_expires_at = datetime.fromtimestamp(
                    start + ANNOUNCEMENT_MAX_AGE_SECONDS, timezone.utc).isoformat()
            expires = alert_expiry(alert)
            effective = timestamp(alert.effective)
            state = "expired" if expires is not None and (expires <= now or (
                effective is not None and expires <= effective)) else "pending"
            # Receipt and transport cursor advance together in one durable transaction.
            saved_alert = asdict(alert)
            saved_alert.update(raw={}, raw_text="", xml_payload="", recent_events="")
            if alert.test_only:
                saved_alert["raw"] = {"is_test": True}
            elif isinstance(alert.raw, dict):
                saved_alert["raw"] = {key: alert.raw[key] for key in ("is_test", "test", "test_only", "status") if key in alert.raw}
            record_id, created = self.inbox.receive(namespace, event_id, saved_alert,
                revision=alert.revision, incident_id=alert.incident_id,
                stream_id=alert.stream_id, state=state, receipt_event_id=event_id)
            if alert.action in {"cancel", "all_clear"} and alert.incident_id:
                self.inbox.cancel_incident(namespace, alert.incident_id, except_record=record_id)
            endpoint["last_event_id"] = event_id
            endpoint["last_stream_id"] = self.inbox.cursor(namespace)
        self.clear_fault("storage")
        return created

    def reject_payload(self, index: int, payload: object, stream_id: str, error: str, worker_signature: str) -> None:
        with self.config_lock:
            endpoint = self.config["endpoints"][index]
            if (not self.config.get("enabled", True) or not endpoint.get("enabled", True)
                    or self.stop_event.is_set() or endpoint_worker_signature(endpoint) != worker_signature):
                return
            event_id = stream_id or (payload.get("id", "") if isinstance(payload, dict) else "")
            try:
                event_id = validate_id(event_id, optional=False)
            except ValueError:
                event_id = "invalid-" + hashlib.sha256(json.dumps(payload, ensure_ascii=True).encode()).hexdigest()
            self.inbox.quarantine(profile_namespace(endpoint), event_id, payload, error, stream_id=stream_id)
        self.record_fault(f"protocol-{index}", "A PBX notification could not be interpreted. It was retained for review; open Health for details.")

    def remember_polling_fallback(self, index: int, signature: str) -> None:
        with self.config_lock:
            endpoint = self.config["endpoints"][index]
            if endpoint_worker_signature(endpoint) != signature:
                return
            staged = normalize_config(self.config)
            staged["endpoints"][index]["live_polling_until"] = time.time() + 86400
            try:
                save_config(staged)
            except OSError as exc:
                log(f"Could not remember live polling preference: {exc}")
                return
            self.config = staged
        log(f"PBX {index + 1}: using live polling; retained records form a discard baseline.")

    def monitor_loop(self) -> None:
        next_maintenance = time.monotonic() + 3600
        while not self.stop_event.is_set():
            try:
                with self.config_lock:
                    cfg = normalize_config(dict(self.config))
                desired = dict(active_endpoints(cfg)) if cfg.get("enabled", True) else {}
                retiring = []
                with self.worker_lock:
                    for index, worker in list(self.workers.items()):
                        candidate = desired.get(index)
                        changed = candidate is None or worker.signature != endpoint_worker_signature(candidate)
                        if changed or not worker.thread.is_alive():
                            retiring.append(worker)
                            self.workers.pop(index, None)
                            self.transport_statuses.pop(index, None)
                    for index, endpoint_cfg in desired.items():
                        if index not in self.workers:
                            endpoint_cfg["last_stream_id"] = self.inbox.cursor(profile_namespace(endpoint_cfg))
                            worker = EndpointTransportWorker(self, index, endpoint_cfg)
                            self.workers[index] = worker
                            worker.start()
                for worker in retiring:
                    worker.stop()
                if not desired:
                    self.status_text = monitoring_idle_status(cfg)
                    self.ui_queue.put(("status", self.status_text))
                self.evaluate_faults()
                if time.monotonic() >= next_maintenance:
                    self.inbox.prune(retention_days=cfg.get("retention_days", 30))
                    next_maintenance = time.monotonic() + 3600
                self.clear_fault("monitor")
            except Exception as exc:
                self.record_fault("monitor", f"Monitoring needs attention: {exc}")
                log(f"Monitor recovered from error: {exc}")
            self.wakeup_event.wait(1)
            self.wakeup_event.clear()

    def update_loop(self) -> None:
        if self.stop_event.wait(10):
            return
        while not self.stop_event.is_set():
            try:
                self.check_for_updates_if_due()
            except Exception as exc:
                log(f"auto update loop failed: {exc}")
            if self.stop_event.wait(UPDATE_RETRY_WAKE_SECONDS):
                break

    def record_current_release(self, release: dict) -> None:
        with self.config_lock:
            self.config["last_update_release_id"] = safe_string(release.get("id"))
            self.config["last_update_release_name"] = (
                safe_string(release.get("name")) or safe_string(release.get("tag_name"))
            )
            self.config["last_update_release_tag"] = safe_string(release.get("tag_name"))
            self.config["pending_update_release_id"] = ""
            self.config["last_update_error"] = ""
            self.config = normalize_config(self.config)
            save_config(self.config)

    def check_for_updates_if_due(self) -> None:
        cfg = self.get_config()
        if not cfg.get("auto_update_enabled", False) or self.presenter.has_active_critical:
            return
        now = time.time()
        if now - float(cfg.get("last_update_check_ts", 0) or 0) < UPDATE_CHECK_SECONDS:
            return
        result = {"last_update_check_ts": now, "last_update_error": ""}
        try:
            release = fetch_latest_github_release(cfg.get("update_channel", "stable"))
            if release and release["id"] != cfg.get("downloaded_update_release_id"):
                archive = download_update_installer(release)
                sls_updates.cleanup_downloads(UPDATE_DIR, keep=(archive,))
                result.update(downloaded_update_release_id=release["id"],
                    downloaded_update_path=str(archive), downloaded_update_tag=release["tag_name"])
                self.ui_queue.put(("update_ready", release["tag_name"]))
            else:
                sls_updates.cleanup_downloads(UPDATE_DIR)
        except Exception as exc:
            result["last_update_error"] = safe_string(exc)
            log(f"Update download check failed: {exc}")
        with self.config_lock:
            staged = {**self.config, **result}
            save_config(staged)
            self.config = staged

    def process_ui_queue(self) -> None:
        deadline = time.monotonic() + 0.015
        for _ in range(32):
            if time.monotonic() > deadline:
                break
            try:
                kind, value = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "fault":
                    FaultToast(self, safe_string(value))
                elif kind == "settings":
                    self.show_settings()
                elif kind == "history":
                    self.show_history()
                elif kind == "health":
                    self.show_health()
                elif kind == "update_ready":
                    self.status_text = f"Update {value} downloaded. Open Health for administrator deployment details."
                    if self.settings_window is not None and self.settings_window.window.winfo_exists():
                        self.settings_window.status_label.configure(text=self.status_text)
                elif kind == "shutdown":
                    self.shutdown()
                    return
                elif kind == "status":
                    with self.worker_lock:
                        states = [item[0] for item in self.transport_statuses.values()]
                    live = sum(state in {"AUTHENTICATED", "POLLING"} for state in states)
                    with self.faults_lock:
                        has_faults = bool(self.faults)
                    self.tray.update("healthy" if live and live == len(states) and not has_faults else "degraded" if live else "offline", safe_string(value))
                    if self.settings_window is not None and self.settings_window.window.winfo_exists():
                        self.settings_window.status_label.configure(text=safe_string(value))
                elif kind == "test_result" and isinstance(value, tuple) and len(value) == 2:
                    callback, message = value
                    callback(message)
            except Exception as exc:
                log(f"UI dispatch failed: {exc}")
        if not self.stop_event.is_set():
            self.pump_inbox()
            self.root.after(100, self.process_ui_queue)

    def present_alert(self, alert: AlertData) -> None:
        # Compatibility for explicit previews only. Network delivery uses pump_inbox.
        self.presenter.submit("preview-" + secrets.token_hex(8), alert)

    def show_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.window.winfo_exists():
            self.settings_window.window.deiconify()
            self.settings_window.window.lift()
            self.settings_window.window.focus_force()
            return
        self.settings_window = SettingsWindow(self)

    def transport_is_authenticated(self, index: int, endpoint_cfg: dict) -> bool:
        """Use the live worker as the connection test when it is already healthy."""
        with self.worker_lock:
            worker = self.workers.get(index)
            status = self.transport_statuses.get(index)
            return bool(
                worker is not None
                and worker.thread.is_alive()
                and worker.signature == endpoint_worker_signature(endpoint_cfg)
                and status is not None
                and status[0] in {"AUTHENTICATED", "POLLING"}
                and time.monotonic() - worker.last_activity < SSE_READ_TIMEOUT_SECONDS
                and worker.last_stream_signal > 0
                and time.monotonic() - worker.last_stream_signal < 45
            )

    def test_now(self, callback) -> None:
        if self.connection_test_event.is_set():
            self.ui_queue.put(("test_result", (callback, "A connection test is already running.")))
            return
        self.connection_test_event.set()

        def worker() -> None:
            failures: list[str] = []
            successes = 0
            message = "Connection test ended unexpectedly."
            try:
                with self.config_lock:
                    cfg = normalize_config(dict(self.config))
                endpoints = active_endpoints(cfg)
                if not endpoints:
                    message = "No active endpoints are configured."
                    return

                for index, endpoint_cfg in endpoints:
                    try:
                        if self.transport_is_authenticated(index, endpoint_cfg):
                            successes += 1
                            continue
                        with self.worker_lock:
                            existing = self.workers.get(index)
                        if existing is not None and existing.signature == endpoint_worker_signature(endpoint_cfg):
                            # Never consume another server stream slot just to
                            # test the same running monitor.
                            existing.stream_ready.wait(20)
                            if self.transport_is_authenticated(index, endpoint_cfg):
                                successes += 1
                                continue
                            raise StreamProtocolError("The monitor has no recent heartbeat or notification. Check Health and the PBX streaming/proxy configuration.")
                        response = open_sse_response(
                            endpoint_cfg,
                            timeout=SSE_TEST_TIMEOUT_SECONDS,
                            read_timeout=SSE_TEST_TIMEOUT_SECONDS,
                        )
                        set_stream_read_timeout(response, 25)
                        watchdog = SocketDeadline(response_socket(response), 25)
                        try:
                            handshake_ok = False
                            stream_ok = False
                            deadline = watchdog.deadline

                            def enforce_test_deadline() -> None:
                                if time.monotonic() > deadline:
                                    raise ApiError("Authenticated handshake test timed out.")

                            for event in iter_sse_events(response, on_activity=enforce_test_deadline, emit_heartbeats=True):
                                if handshake_ok and event.name in {"_heartbeat", "notification"}:
                                    stream_ok = True
                                    break
                                if event.name == "_heartbeat" and not handshake_ok:
                                    continue
                                if event.name != "authenticated" or handshake_ok:
                                    raise StreamProtocolError(
                                        f"PBX sent '{event.name}' before the authenticated handshake."
                                    )
                                payload = json.loads(event.data)
                                handshake_ok = (
                                    isinstance(payload, dict)
                                    and payload.get("ok") is True
                                    and safe_string(payload.get("transport")) == DELIVERY_LIVE
                                )
                                if not handshake_ok:
                                    break
                            if watchdog.expired:
                                raise ApiError("Authenticated handshake test exceeded its deadline.")
                            if not handshake_ok:
                                raise StreamProtocolError("Authenticated handshake was not received.")
                            if not stream_ok:
                                raise StreamProtocolError("Authentication succeeded, but no heartbeat or notification arrived. Check the PBX streaming/proxy configuration.")
                        finally:
                            watchdog.cancel()
                            shutdown_socket(response_socket(response))
                            response.close()
                        successes += 1
                    except Exception as exc:
                        failures.append(f"{endpoint_display_name(index, endpoint_cfg)}: {exc}")

                if failures:
                    message = f"Test completed: {successes}/{len(endpoints)} OK. " + " | ".join(failures[:2])
                else:
                    message = (
                        f"Connection verified: {successes}/{len(endpoints)} authenticated with live delivery checks. "
                        "Use Local preview to test display and sound; this test does not send an announcement."
                    )
            except Exception as exc:
                message = f"Connection test failed: {safe_string(exc)}"
                failures.append(message)
            finally:
                self.connection_test_event.clear()
                self.status_text = message
                if failures:
                    log(message)
                self.ui_queue.put(("test_result", (callback, message)))

        threading.Thread(target=worker, name="EndpointTest", daemon=True).start()

    def shutdown(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self.wakeup_event.set()
        self.presenter.shutdown()
        with self.worker_lock:
            workers = list(self.workers.values())
            self.workers.clear()
        # Socket shutdown and auth locks can themselves wait on a transport
        # thread. Keep both stop and join off Tk, with a UI-enforced deadline.
        shutdown_deadline = time.monotonic() + 3
        def finish():
            try:
                for worker in workers:
                    worker.stop()
                for worker in workers:
                    worker.thread.join(max(0, shutdown_deadline - time.monotonic()))
            finally:
                self._shutdown_ready.set()
        self._shutdown_ready = threading.Event()
        threading.Thread(target=finish, name="Shutdown", daemon=True).start()
        def poll():
            if self._shutdown_ready.is_set() or time.monotonic() >= shutdown_deadline:
                if hasattr(self, "tray") and self.tray is not None:
                    self.tray.stop()
                self.root.destroy()
            else:
                self.root.after(50, poll)
        self.root.after(50, poll)


def main() -> None:
    if "--check-presentation" in sys.argv:
        from sls_presentation_probe import check_presentation
        try:
            check_presentation(SettingsWindow._configure_style)
        except Exception:
            raise SystemExit(1) from None
        return
    if "--uninstall" in sys.argv:
        messagebox.showinfo(APP_DISPLAY_NAME, "Use Windows Installed Apps to uninstall this managed installation.")
        return
    background = "--background" in sys.argv
    try:
        config = load_config()
    except Exception as exc:
        messagebox.showerror("Settings need attention", str(exc))
        return
    if background and not config.get("startup_enabled", True):
        return
    if not acquire_single_instance():
        for _ in range(10):
            if notify_existing_instance(show_settings=not background):
                break
            time.sleep(0.15)
        return

    root = Tk()
    try:
        app = MassNotifyApp(root, show_settings_on_start=not background)
    except Exception as exc:
        root.withdraw()
        messagebox.showerror("Unable to start notification monitoring", str(exc), parent=root)
        root.destroy()
        return
    root.protocol("WM_DELETE_WINDOW", app.shutdown)
    root.mainloop()


if __name__ == "__main__":
    main()
