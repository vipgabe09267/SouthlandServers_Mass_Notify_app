from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
import queue
import random
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, Canvas, IntVar, StringVar, Text, Tk, Toplevel, filedialog, messagebox
from tkinter import ttk

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

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
APP_VERSION = "1.0.7-Beta"
IPC_HOST = "127.0.0.1"
IPC_PORT = 48572
DEFAULT_POLL_SECONDS = 10
MAX_ENDPOINTS = 3
DELIVERY_LIVE = "live_sse"
DELIVERY_POLL = "legacy_polling"
DELIVERY_LABELS = {
    DELIVERY_LIVE: "Live handshake (v0.0.7-beta or newer only)",
    DELIVERY_POLL: "Legacy polling fallback (v0.0.6-beta or older only)",
}
PBX_LIVE_PATH = "/api/sipnotify/desktop/stream"
PBX_POLL_PATH = "/api/sipnotify/desktop"
SSE_CONNECT_TIMEOUT_SECONDS = 25
SSE_TEST_TIMEOUT_SECONDS = 12
SSE_READ_TIMEOUT_SECONDS = 330
LIVE_CATCHUP_SECONDS = 5
SSE_MAX_LINE_BYTES = 64 * 1024
SSE_MAX_EVENT_BYTES = 512 * 1024
RECENT_EVENT_ID_LIMIT = 100
RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 20, 30)
FAULT_NOTIFY_SECONDS = 5 * 60
FAULT_TOAST_VISIBLE_MS = 18000
ALERT_AUTO_HIDE_MS = 45000
ALERT_IMAGE_WAIT_MS = 9000
IMAGE_FETCH_LIMIT_BYTES = 5 * 1024 * 1024
IMAGE_PIXEL_LIMIT = 20_000_000
ALERT_FOOTER_TEXT = "Copyright \u00a9 Southland Servers Group"
UPDATE_CHECK_SECONDS = 15 * 60
UPDATE_RETRY_WAKE_SECONDS = 5 * 60
UPDATE_DOWNLOAD_LIMIT_BYTES = 150 * 1024 * 1024
AUDIO_DIR_NAME = "audio"
DEFAULT_AUDIO_NAME = "Announcement.wav"
MAX_CUSTOM_AUDIO_BYTES = 25 * 1024 * 1024
AUTH_TOKEN = "token"
AUTH_NONE = "none"
AUTH_BASIC = "basic"
AUTH_LABELS = {
    AUTH_TOKEN: "Bearer token",
    AUTH_NONE: "No authentication",
    AUTH_BASIC: "Username/password",
}
GITHUB_OWNER = "vipgabe09267"
GITHUB_REPO = "SouthlandServers_Mass_Notify_app"
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases?per_page=10"
GITHUB_RELEASE_DOWNLOAD_PREFIX = f"/{GITHUB_OWNER}/{GITHUB_REPO}/releases/download/".lower()
UPDATE_INSTALLER_ASSET_NAMES = ("SLS_Mass_Notify_Installer.exe",)

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / COMPANY_NAME / APP_SHORT_NAME
CONFIG_PATH = CONFIG_DIR / "settings.json"
LOG_PATH = CONFIG_DIR / "app.log"
UPDATE_DIR = CONFIG_DIR / "updates"
USER_AUDIO_DIR = CONFIG_DIR / AUDIO_DIR_NAME
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / COMPANY_NAME / APP_SHORT_NAME
INSTALL_EXE_PATH = INSTALL_DIR / EXE_NAME
POWERSHELL_EXE = (
    Path(os.environ.get("SystemRoot", r"C:\Windows"))
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
)
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_REG_PATH = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_SHORT_NAME}"
START_MENU_DIR = (
    Path(os.environ.get("APPDATA", Path.home()))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs"
    / COMPANY_NAME
)
START_MENU_APP_SHORTCUT = START_MENU_DIR / f"{APP_DISPLAY_NAME}.lnk"
START_MENU_UNINSTALL_SHORTCUT = START_MENU_DIR / f"Uninstall {APP_DISPLAY_NAME}.lnk"
UNINSTALL_HELPER_NAME = f"Uninstall_{APP_SHORT_NAME}.cmd"
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
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError:
        pass


def is_windows() -> bool:
    return os.name == "nt"


def acquire_single_instance() -> bool:
    if not is_windows():
        return True
    global _MUTEX_HANDLE
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
        if not handle:
            return True
        if ctypes.windll.kernel32.GetLastError() == 183:
            ctypes.windll.kernel32.CloseHandle(handle)
            return False
        _MUTEX_HANDLE = handle
        return True
    except Exception as exc:
        log(f"single-instance mutex failed: {exc}")
        return True


def set_startup_enabled(enabled: bool) -> None:
    if winreg is None:
        return
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, APP_SHORT_NAME, 0, winreg.REG_SZ, app_command(True))
            else:
                try:
                    winreg.DeleteValue(key, APP_SHORT_NAME)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        log(f"startup registry update failed: {exc}")


def is_startup_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_SHORT_NAME)
            return APP_SHORT_NAME in value or EXE_NAME in value or "python" in value.lower()
    except OSError:
        return False


def run_hidden(command: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=False,
        timeout=timeout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def create_shortcut(
    shortcut_path: Path,
    target_path: Path,
    *,
    arguments: str = "",
    description: str = "",
    icon_path: Path | None = None,
    working_dir: Path | None = None,
) -> None:
    if not is_windows():
        return
    try:
        shortcut_path.parent.mkdir(parents=True, exist_ok=True)
        icon = icon_path or target_path
        workdir = working_dir or target_path.parent
        script = r"""
param(
    [string]$ShortcutPath,
    [string]$TargetPath,
    [string]$Arguments,
    [string]$Description,
    [string]$IconPath,
    [string]$WorkingDirectory
)
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $TargetPath
$shortcut.Arguments = $Arguments
$shortcut.Description = $Description
$shortcut.IconLocation = $IconPath
$shortcut.WorkingDirectory = $WorkingDirectory
$shortcut.Save()
"""
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as fh:
            fh.write(script)
            script_path = Path(fh.name)
        try:
            result = run_hidden(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                    "-ShortcutPath",
                    str(shortcut_path),
                    "-TargetPath",
                    str(target_path),
                    "-Arguments",
                    arguments,
                    "-Description",
                    description,
                    "-IconPath",
                    str(icon),
                    "-WorkingDirectory",
                    str(workdir),
                ]
            )
            if result.returncode != 0:
                log(f"shortcut creation failed for {shortcut_path}: PowerShell exit {result.returncode}")
        finally:
            try:
                script_path.unlink()
            except OSError:
                pass
    except Exception as exc:
        log(f"shortcut creation failed for {shortcut_path}: {exc}")


def write_uninstall_helper() -> None:
    try:
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        helper_path = INSTALL_DIR / UNINSTALL_HELPER_NAME
        helper_path.write_text(f'@echo off\r\n"{INSTALL_EXE_PATH}" --uninstall\r\n', encoding="utf-8")
    except OSError as exc:
        log(f"uninstall helper write failed: {exc}")


def create_start_menu_entries() -> None:
    if not is_windows():
        return
    target = INSTALL_EXE_PATH if INSTALL_EXE_PATH.exists() else Path(sys.executable)
    write_uninstall_helper()
    create_shortcut(
        START_MENU_APP_SHORTCUT,
        target,
        description=APP_DISPLAY_NAME,
        icon_path=target,
        working_dir=target.parent,
    )
    create_shortcut(
        START_MENU_UNINSTALL_SHORTCUT,
        target,
        arguments="--uninstall",
        description=f"Uninstall {APP_DISPLAY_NAME}",
        icon_path=target,
        working_dir=Path(tempfile.gettempdir()),
    )


def remove_start_menu_entries() -> None:
    for shortcut in (START_MENU_APP_SHORTCUT, START_MENU_UNINSTALL_SHORTCUT):
        try:
            if shortcut.exists():
                shortcut.unlink()
        except OSError as exc:
            log(f"shortcut removal failed for {shortcut}: {exc}")
    try:
        START_MENU_DIR.rmdir()
    except OSError:
        pass


def register_uninstall_entry() -> None:
    if winreg is None:
        return
    try:
        install_size_kb = 0
        if INSTALL_EXE_PATH.exists():
            install_size_kb = max(1, INSTALL_EXE_PATH.stat().st_size // 1024)
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, UNINSTALL_REG_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_DISPLAY_NAME)
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, COMPANY_DISPLAY_NAME)
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(INSTALL_DIR))
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(INSTALL_EXE_PATH))
            winreg.SetValueEx(
                key,
                "UninstallString",
                0,
                winreg.REG_SZ,
                f'"{INSTALL_EXE_PATH}" --uninstall',
            )
            winreg.SetValueEx(
                key,
                "QuietUninstallString",
                0,
                winreg.REG_SZ,
                f'"{INSTALL_EXE_PATH}" --uninstall --quiet',
            )
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, install_size_kb)
    except OSError as exc:
        log(f"uninstall registry update failed: {exc}")


def remove_uninstall_entry() -> None:
    if winreg is None:
        return
    try:
        # Try to remove from HKEY_CURRENT_USER
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG_PATH)
        except FileNotFoundError:
            pass
        # Try to remove from HKEY_LOCAL_MACHINE (if admin)
        try:
            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_REG_PATH)
        except (FileNotFoundError, PermissionError):
            pass
    except OSError as exc:
        log(f"uninstall registry removal failed: {exc}")


def ensure_install_artifacts() -> None:
    # Installer-managed now. Kept as a no-op for compatibility with older builds.
    return


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


def protect_profile_secret(index: int, kind: str, secret: str) -> str:
    target = profile_credential_target(index, kind)
    if not secret:
        delete_profile_secret(index, kind)
        return ""
    if is_windows():
        try:
            _write_windows_credential(target, secret)
            return "cred:" + target
        except Exception as exc:
            log(f"Windows Credential Manager write failed; using DPAPI fallback: {exc}")
    return protect_secret(secret)


def delete_profile_secret(index: int, kind: str) -> None:
    if not is_windows():
        return
    try:
        _delete_windows_credential(profile_credential_target(index, kind))
    except Exception as exc:
        log(f"Windows Credential Manager cleanup failed: {exc}")


def delete_all_profile_secrets() -> None:
    for index in range(MAX_ENDPOINTS):
        delete_profile_secret(index, "token")
        delete_profile_secret(index, "password")


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
        # Legacy builds stored endpoint tokens as raw text. Return them so the
        # settings window can save once and migrate them to the current format.
        return value
    try:
        if value.startswith("dpapi:") and is_windows():
            return _unprotect_with_dpapi(value)
        if value.startswith("plain:"):
            return base64.b64decode(value.removeprefix("plain:")).decode("utf-8")
    except Exception as exc:
        log(f"token decrypt failed: {exc}")
    return ""


def blank_endpoint(index: int) -> dict:
    return {
        "name": f"PBX {index + 1}",
        "endpoint": "",
        "enabled": index == 0,
        "auth_mode": AUTH_BASIC,
        "delivery_mode": DELIVERY_LIVE,
        "poll_seconds": DEFAULT_POLL_SECONDS,
        "reconnect_automatically": True,
        "allow_invalid_certificate": False,
        "allow_legacy_media": False,
        "no_token": False,
        "token": "",
        "username": "",
        "password": "",
        "last_event_id": "",
        "last_fingerprint": "",
        "recent_event_ids": [],
    }


def normalize_auth_mode(value: object, no_token: bool = False) -> str:
    mode = normalize_key(value or "")
    if no_token:
        return AUTH_NONE
    if mode in {"none", "noauth", "noauthentication", "notoken"}:
        return AUTH_NONE
    if mode in {"basic", "usernamepassword", "userpass", "password"}:
        return AUTH_BASIC
    return AUTH_TOKEN


def normalize_delivery_mode(value: object, *, existing_url: str = "") -> str:
    mode = normalize_key(value or "")
    if mode in {"legacypolling", "polling", "poll", "json", "legacyfallback"}:
        return DELIVERY_POLL
    if mode in {"livesse", "live", "sse", "authenticatedhandshake", "liveauthenticatedhandshake"}:
        return DELIVERY_LIVE
    # Preserve upgraded 1.0.6 profiles as polling. Newly created profiles are live.
    return DELIVERY_POLL if safe_string(existing_url) else DELIVERY_LIVE


def normalize_poll_seconds(value: object, default: int = DEFAULT_POLL_SECONDS) -> int:
    try:
        return max(5, min(3600, int(value)))
    except (TypeError, ValueError):
        return max(5, min(3600, int(default)))


def normalize_endpoint(value: object, index: int) -> dict:
    endpoint = blank_endpoint(index)
    if isinstance(value, dict):
        no_token = bool(value.get("no_token", value.get("noToken", False)))
        auth_mode = normalize_auth_mode(value.get("auth_mode", value.get("authMode", "")), no_token)
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
                "auth_mode": auth_mode,
                "delivery_mode": normalize_delivery_mode(
                    value.get("delivery_mode", value.get("deliveryMode", "")), existing_url=configured_url
                ),
                "poll_seconds": normalize_poll_seconds(
                    value.get("poll_seconds", value.get("pollSeconds", DEFAULT_POLL_SECONDS))
                ),
                "reconnect_automatically": bool(value.get("reconnect_automatically", True)),
                "allow_invalid_certificate": bool(value.get("allow_invalid_certificate", False)),
                "allow_legacy_media": bool(value.get("allow_legacy_media", False)),
                "no_token": auth_mode == AUTH_NONE,
                "token": safe_string(value.get("token")),
                "username": safe_string(value.get("username", value.get("user", ""))),
                "password": safe_string(value.get("password")),
                "last_event_id": safe_string(value.get("last_event_id", value.get("lastEventId", ""))),
                "last_fingerprint": safe_string(
                    value.get("last_fingerprint", value.get("lastFingerprint", ""))
                ),
                "recent_event_ids": recent_ids,
            }
        )
    return endpoint


def normalize_endpoints(config: dict) -> list[dict]:
    endpoints: list[dict] = []
    legacy_interval = normalize_poll_seconds(config.get("poll_seconds", DEFAULT_POLL_SECONDS))
    configured = config.get("endpoints")
    if isinstance(configured, list):
        for index, endpoint in enumerate(configured[:MAX_ENDPOINTS]):
            normalized = normalize_endpoint(endpoint, index)
            if isinstance(endpoint, dict) and "poll_seconds" not in endpoint and "pollSeconds" not in endpoint:
                normalized["poll_seconds"] = legacy_interval
            endpoints.append(normalized)

    if not endpoints and (config.get("endpoint") or config.get("token")):
        endpoints.append(
            normalize_endpoint(
                {
                    "name": "PBX 1",
                    "endpoint": config.get("endpoint", ""),
                    "enabled": True,
                    "auth_mode": AUTH_NONE if bool(config.get("no_token", False)) else AUTH_TOKEN,
                    "no_token": bool(config.get("no_token", False)),
                    "token": config.get("token", ""),
                    "username": config.get("username", ""),
                    "password": config.get("password", ""),
                    "poll_seconds": legacy_interval,
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
    normalized["token"] = first.get("token", "")
    normalized["username"] = first.get("username", "")
    normalized["password"] = first.get("password", "")
    normalized["auth_mode"] = normalize_auth_mode(first.get("auth_mode"), bool(first.get("no_token", False)))
    normalized["no_token"] = bool(first.get("no_token", False))
    normalized["last_event_id"] = first.get("last_event_id", "")
    normalized["last_fingerprint"] = first.get("last_fingerprint", "")
    normalized["audio_sound"] = safe_audio_name(safe_string(normalized.get("audio_sound")) or DEFAULT_AUDIO_NAME)
    return normalized


def endpoint_has_credentials(endpoint: dict) -> bool:
    auth_mode = normalize_auth_mode(endpoint.get("auth_mode"), bool(endpoint.get("no_token")))
    if auth_mode == AUTH_NONE:
        return True
    if auth_mode == AUTH_BASIC:
        return bool(safe_string(endpoint.get("username")) and unprotect_secret(endpoint.get("password", "")))
    return bool(unprotect_secret(endpoint.get("token", "")))


def endpoint_display_name(index: int, endpoint: dict) -> str:
    name = safe_string(endpoint.get("name")) or f"PBX {index + 1}"
    url = safe_string(endpoint.get("endpoint"))
    if url:
        parsed = urlparse(url)
        if parsed.netloc:
            return f"{name} ({parsed.netloc})"
    return name


def endpoint_url_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None and parsed.password is None:
        return True
    return False


def endpoint_security_warnings(url: str, auth_mode: str) -> list[tuple[str, str]]:
    warnings: list[tuple[str, str]] = []
    parsed = urlparse(safe_string(url))
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        warnings.append(
            (
                "red",
                "Warning: this HTTP endpoint may be vulnerable to man-in-the-middle attacks. Use HTTPS when possible.",
            )
        )
    if normalize_auth_mode(auth_mode) == AUTH_NONE:
        warnings.append(
            (
                "yellow",
                "Caution: this endpoint is not using a token, so requests may not be fully authenticated.",
            )
        )
    return warnings


def normalize_pbx_address(value: object) -> str:
    address = safe_string(value)
    if not address:
        return ""
    if "://" not in address:
        address = "https://" + address
    parsed = urlparse(address)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PBX address must be an HTTP(S) hostname or URL without credentials, query, or fragment.")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    port = f":{parsed.port}" if parsed.port and parsed.port != default_port else ""
    return f"{parsed.scheme.lower()}://{host}{port}"


def pbx_transport_url(endpoint_cfg: dict, delivery_mode: str | None = None) -> str:
    origin = normalize_pbx_address(endpoint_cfg.get("endpoint", ""))
    mode = delivery_mode or normalize_delivery_mode(
        endpoint_cfg.get("delivery_mode"), existing_url=safe_string(endpoint_cfg.get("endpoint"))
    )
    path = PBX_LIVE_PATH if mode == DELIVERY_LIVE else PBX_POLL_PATH
    return origin + path + ("?limit=25" if mode == DELIVERY_POLL else "")


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
    source = source.resolve()
    if source.suffix.lower() != ".wav":
        raise ValueError("Only .wav audio files are supported.")
    if not source.is_file():
        raise FileNotFoundError(f"Audio file does not exist: {source}")
    if source.stat().st_size > MAX_CUSTOM_AUDIO_BYTES:
        raise ValueError("Audio file is too large. Use a WAV file under 25 MB.")
    USER_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    destination = USER_AUDIO_DIR / source.name
    if source != destination:
        shutil.copy2(source, destination)
    return destination.name


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
        auth_mode = normalize_auth_mode(endpoint.get("auth_mode"), bool(endpoint.get("no_token")))
        if auth_mode == AUTH_BASIC:
            if not safe_string(endpoint.get("username")):
                return f"Monitoring is waiting: enter the desktop username for {name}."
            if not unprotect_secret(endpoint.get("password", "")):
                return f"Monitoring is waiting: enter the desktop password for {name}."
        elif auth_mode == AUTH_TOKEN and not unprotect_secret(endpoint.get("token", "")):
            return f"Monitoring is waiting: enter the API token for {name}."
    return "Starting PBX monitoring..."


def default_config() -> dict:
    return {
        "auto_update_enabled": True,
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
        "no_token": False,
        "poll_seconds": DEFAULT_POLL_SECONDS,
        "startup_enabled": True,
        "token": "",
        "username": "",
        "password": "",
        "last_event_id": "",
        "last_fingerprint": "",
    }


def load_config() -> dict:
    cfg = default_config()
    try:
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                cfg.update(loaded)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"config load failed: {exc}")
    return normalize_config(cfg)


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = CONFIG_PATH.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    temp_path.replace(CONFIG_PATH)


def install_self_if_needed() -> None:
    # Installer-managed now. Direct runs are allowed without copying to LocalAppData.
    return


def notify_existing_instance(show_settings: bool) -> bool:
    try:
        with socket.create_connection((IPC_HOST, IPC_PORT), timeout=0.4) as sock:
            command = b"SHOW_SETTINGS\n" if show_settings else b"PING\n"
            sock.sendall(command)
        return True
    except OSError:
        return False


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


def parse_release_version(value: object) -> tuple[int, int, int, int, int] | None:
    text = safe_string(value).strip()
    match = re.search(
        r"(?i)(?:^|[^0-9])v?(\d+)\.(\d+)\.(\d+)(?:[-_]?([a-z]+)(\d*))?",
        text,
    )
    if not match:
        return None
    major, minor, patch = (int(match.group(index)) for index in (1, 2, 3))
    prerelease = (match.group(4) or "").lower()
    prerelease_number = int(match.group(5) or 0)
    stage_rank = {
        "dev": 0,
        "alpha": 1,
        "a": 1,
        "beta": 2,
        "b": 2,
        "rc": 3,
    }.get(prerelease, 1 if prerelease else 4)
    return major, minor, patch, stage_rank, prerelease_number


def compare_release_versions(candidate: object, current: object | None = None) -> int | None:
    if current is None:
        current = APP_VERSION
    candidate_version = parse_release_version(candidate)
    current_version = parse_release_version(current)
    if candidate_version is None or current_version is None:
        return None
    return (candidate_version > current_version) - (candidate_version < current_version)


def trusted_github_download_url(value: object) -> bool:
    parsed = urlparse(safe_string(value))
    return bool(
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "github.com"
        and parsed.port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path.lower().startswith(GITHUB_RELEASE_DOWNLOAD_PREFIX)
    )


def lookup(obj: object, keys: set[str]) -> object | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if normalize_key(key) in keys and value not in (None, ""):
                return value
        for value in obj.values():
            found = lookup(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = lookup(item, keys)
            if found not in (None, ""):
                return found
    return None


def lookup_preferred(obj: object, keys: tuple[str, ...]) -> object | None:
    if isinstance(obj, dict):
        normalized = {normalize_key(key): value for key, value in obj.items()}
        for key in keys:
            value = normalized.get(normalize_key(key))
            if value not in (None, ""):
                return value
        for value in obj.values():
            found = lookup_preferred(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = lookup_preferred(item, keys)
            if found not in (None, ""):
                return found
    return None


def lookup_object(obj: object, keys: set[str]) -> object | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if normalize_key(key) in keys and isinstance(value, (dict, list)):
                return value
        for value in obj.values():
            found = lookup_object(value, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = lookup_object(item, keys)
            if found is not None:
                return found
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
    allow_invalid_certificate: bool = False
    allow_legacy_media: bool = False


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
    image_url = safe_string(lookup(source, IMAGE_KEYS))
    recipients = bounded_text(lookup(source, RECIPIENT_KEYS), 1000)
    timestamp = bounded_text(lookup(source, TIMESTAMP_KEYS), 100)
    area = bounded_text(lookup(source, AREA_KEYS), 500)
    effective = bounded_text(lookup(source, EFFECTIVE_KEYS), 100)
    expires = bounded_text(lookup(source, EXPIRES_KEYS), 100)
    # The PBX contract intentionally prefers message over compatibility aliases.
    body = bounded_text(lookup_preferred(source, ("message", "body", "text", "description", "desc")), 8000)
    description = bounded_text(lookup_preferred(source, ("description", "message", "body", "text", "desc")), 8000)
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
        body = description
    if not description:
        description = body
    if not title and phone.text:
        title = phone.text.splitlines()[0][:80]
    if not title:
        title = "SLS Mass Notification"

    test_marker = f"{title}\n{body}\n{event}".upper()
    lightning_marker = normalize_key(f"{title} {event}")
    test_only = "TEST ONLY" in test_marker or ("lightning" in lightning_marker and "test" in lightning_marker)

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
    )


class ApiError(Exception):
    pass


class UnauthorizedError(ApiError):
    pass


class TlsRequiredError(ApiError):
    pass


class RateLimitedError(ApiError):
    pass


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


def build_http_opener(*, same_origin: bool = False, allow_invalid_certificate: bool = False):
    handlers: list[object] = [SameOriginRedirectHandler() if same_origin else HttpSchemeRedirectHandler()]
    if allow_invalid_certificate:
        context = ssl._create_unverified_context()  # Explicit, per-profile unsafe testing option.  # nosec B323
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def open_http_request(
    request: urllib.request.Request,
    timeout: int,
    *,
    same_origin: bool = False,
    allow_invalid_certificate: bool = False,
):
    opener = build_http_opener(
        same_origin=same_origin, allow_invalid_certificate=allow_invalid_certificate
    )
    return opener.open(request, timeout=timeout)


def raise_http_status(exc: urllib.error.HTTPError) -> None:
    if exc.code == 401:
        raise UnauthorizedError("Authentication failed. Review the desktop username and password.") from exc
    if exc.code == 426:
        raise TlsRequiredError("HTTPS is required by the PBX.") from exc
    if exc.code == 429:
        raise RateLimitedError("Authentication attempts were rate limited; waiting at least one minute.") from exc
    if exc.code == 503:
        raise ServiceUnavailableError("The PBX desktop notification service is unavailable.") from exc
    raise ApiError(f"HTTP {exc.code}: {exc.reason}") from exc


def endpoint_auth_headers(endpoint_cfg: dict) -> dict[str, str]:
    auth_mode = normalize_auth_mode(endpoint_cfg.get("auth_mode"), bool(endpoint_cfg.get("no_token")))
    if auth_mode == AUTH_NONE:
        return {}
    if auth_mode == AUTH_BASIC:
        username = safe_string(endpoint_cfg.get("username"))
        password = unprotect_secret(endpoint_cfg.get("password", ""))
        if not username or not password:
            return {}
        raw = f"{username}:{password}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
    token = unprotect_secret(endpoint_cfg.get("token", ""))
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def fetch_endpoint(
    endpoint: str,
    auth_headers: dict[str, str] | None = None,
    *,
    allow_invalid_certificate: bool = False,
) -> tuple[object, str]:
    headers = {
        "Accept": "application/json, application/xml, text/xml, text/plain, */*",
        "User-Agent": f"{APP_SHORT_NAME}/{APP_VERSION}",
    }
    headers.update(auth_headers or {})
    request = urllib.request.Request(endpoint, headers=headers, method="GET")
    opener = build_http_opener(
        same_origin=True, allow_invalid_certificate=allow_invalid_certificate
    )
    try:
        with opener.open(request, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "")
            raw_bytes = response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise_http_status(exc)
    except urllib.error.URLError as exc:
        raise ApiError(str(exc.reason)) from exc
    except TimeoutError as exc:
        raise ApiError("Request timed out") from exc

    raw_text = raw_bytes.decode("utf-8", errors="replace")
    if "json" in content_type.lower() or raw_text.lstrip().startswith(("{", "[")):
        try:
            return json.loads(raw_text), raw_text
        except json.JSONDecodeError:
            pass
    if raw_text.lstrip().startswith("<"):
        return {"xml_payload": raw_text}, raw_text
    return {"raw": raw_text}, raw_text


@dataclass
class SseEvent:
    name: str
    data: str
    event_id: str
    retry_ms: int | None = None


def iter_sse_events(response, *, on_activity=None, stop_event: threading.Event | None = None):
    event_name = ""
    event_id = ""
    data_lines: list[str] = []
    retry_ms: int | None = None
    event_bytes = 0

    def dispatch():
        nonlocal event_name, data_lines, retry_ms, event_bytes
        if not data_lines:
            event_name = ""
            event_bytes = 0
            return None
        event = SseEvent(event_name or "message", "\n".join(data_lines), event_id, retry_ms)
        event_name = ""
        data_lines = []
        retry_ms = None
        event_bytes = 0
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
        if on_activity is not None:
            on_activity()
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            pending = dispatch()
            if pending is not None:
                yield pending
            continue
        if line.startswith(":"):
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
            event_id = value
        elif field == "retry" and value.isdigit():
            retry_ms = min(int(value), 60_000)


def set_stream_read_timeout(response, timeout: int) -> None:
    """Apply a read timeout after HTTP headers arrive without lengthening authentication."""
    candidates = (
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(response, "fp", None), "_sock", None),
    )
    for sock in candidates:
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(timeout)
            return


def open_sse_response(
    endpoint_cfg: dict,
    *,
    timeout: int = SSE_CONNECT_TIMEOUT_SECONDS,
    read_timeout: int = SSE_READ_TIMEOUT_SECONDS,
):
    if normalize_auth_mode(endpoint_cfg.get("auth_mode"), bool(endpoint_cfg.get("no_token"))) != AUTH_BASIC:
        raise ApiError("Live authenticated handshake requires desktop username/password authentication.")
    url = pbx_transport_url(endpoint_cfg, DELIVERY_LIVE)
    if urlparse(url).scheme.lower() != "https":
        raise TlsRequiredError("Live authenticated handshake requires HTTPS.")
    headers = {
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
        "User-Agent": f"{APP_SHORT_NAME}/{APP_VERSION}",
        **endpoint_auth_headers(endpoint_cfg),
    }
    last_event_id = safe_string(endpoint_cfg.get("last_event_id"))
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = build_http_opener(
        same_origin=True,
        allow_invalid_certificate=bool(endpoint_cfg.get("allow_invalid_certificate", False)),
    )
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
    set_stream_read_timeout(response, read_timeout)
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


def normalize_alert_urls(alert: AlertData, endpoint: str, endpoint_cfg: dict | None = None) -> None:
    alert.image_url = resolve_image_url(alert.image_url, endpoint)
    alert.source_endpoint = endpoint
    endpoint_cfg = endpoint_cfg or {}
    alert.allow_invalid_certificate = bool(endpoint_cfg.get("allow_invalid_certificate", False))
    alert.allow_legacy_media = bool(endpoint_cfg.get("allow_legacy_media", False))
    if alert.image_url:
        image_origin = request_origin(alert.image_url)
        source_origin = request_origin(endpoint)
        if image_origin != source_origin:
            alert.image_url = ""
        elif image_origin[0] != "https" and not alert.allow_legacy_media:
            alert.image_url = ""


def fetch_image_bytes(
    image_url: str,
    *,
    source_endpoint: str = "",
    allow_invalid_certificate: bool = False,
    allow_legacy_media: bool = False,
) -> bytes:
    parsed = urlparse(image_url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ApiError("Alert image URL must be an HTTP(S) URL without embedded credentials")
    if parsed.scheme.lower() != "https" and not allow_legacy_media:
        raise ApiError("Insecure alert media was blocked")
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
        allow_invalid_certificate=allow_invalid_certificate,
    ) as response:
        data = response.read(IMAGE_FETCH_LIMIT_BYTES + 1)
    if len(data) > IMAGE_FETCH_LIMIT_BYTES:
        raise ApiError("Image response was too large")
    return data


def fetch_latest_github_release() -> dict:
    request = urllib.request.Request(
        GITHUB_RELEASES_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{APP_SHORT_NAME}/{APP_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    with open_http_request(request, timeout=12) as response:
        raw_bytes = response.read(1024 * 512)
    data = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(data, list):
        raise ApiError("GitHub did not return a release list")

    for release in data:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        selected_asset = None
        expected_asset_names = {name.lower() for name in UPDATE_INSTALLER_ASSET_NAMES}
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = safe_string(asset.get("name"))
            if name.lower() in expected_asset_names:
                selected_asset = asset
                break
        if selected_asset is None:
            continue

        download_url = safe_string(selected_asset.get("browser_download_url"))
        if not trusted_github_download_url(download_url):
            raise ApiError("Release installer asset did not provide a trusted GitHub download URL")

        release_id = safe_string(release.get("id")) or safe_string(release.get("tag_name"))
        if not release_id:
            raise ApiError("GitHub release did not include an id or tag")
        return {
            "id": release_id,
            "tag_name": safe_string(release.get("tag_name")),
            "name": safe_string(release.get("name")) or safe_string(release.get("tag_name")),
            "published_at": safe_string(release.get("published_at")),
            "asset_name": safe_string(selected_asset.get("name")),
            "asset_size": int(selected_asset.get("size") or 0),
            "asset_digest": safe_string(selected_asset.get("digest")),
            "download_url": download_url,
        }

    raise ApiError("No GitHub release with SLS_Mass_Notify_Installer.exe was found")


def download_update_installer(release: dict) -> Path:
    release_id = safe_string(release.get("id"))
    download_url = safe_string(release.get("download_url"))
    asset_name = safe_string(release.get("asset_name")) or "SLS_Mass_Notify_Installer.exe"
    if not release_id or not trusted_github_download_url(download_url):
        raise ApiError("Invalid update release metadata")
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    safe_release_id = re.sub(r"[^A-Za-z0-9_.-]", "_", release_id)[:80]
    installer_path = UPDATE_DIR / f"SLS_Mass_Notify_Installer_{safe_release_id}.exe"
    temp_path = installer_path.with_suffix(".tmp")
    request = urllib.request.Request(
        download_url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": f"{APP_SHORT_NAME}/{APP_VERSION}",
        },
        method="GET",
    )
    with open_http_request(request, timeout=45) as response:
        with temp_path.open("wb") as fh:
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > UPDATE_DOWNLOAD_LIMIT_BYTES:
                    raise ApiError("Downloaded update installer was too large")
                fh.write(chunk)
    expected_size = int(release.get("asset_size") or 0)
    expected_digest = safe_string(release.get("asset_digest")).lower()
    actual_size = temp_path.stat().st_size
    if actual_size < 5 * 1024 * 1024:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise ApiError("Downloaded update installer was unexpectedly small")
    if expected_size and abs(actual_size - expected_size) > 4096:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise ApiError("Downloaded update installer size did not match the GitHub release asset")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest):
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise ApiError("GitHub release asset did not include a valid SHA-256 digest")
    digest = hashlib.sha256()
    with temp_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_digest = digest.hexdigest()
    if actual_digest != expected_digest.removeprefix("sha256:"):
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise ApiError("Downloaded update installer failed SHA-256 verification")
    temp_path.replace(installer_path)
    return installer_path


def launch_update_installer(installer_path: Path, install_dir: Path | None = None) -> None:
    if not installer_path.exists():
        raise ApiError(f"Update installer is missing: {installer_path}")
    if install_dir is None and getattr(sys, "frozen", False):
        install_dir = Path(sys.executable).resolve().parent
    arguments = ["--silent", "--update"]
    if install_dir is not None:
        arguments.extend(["--install-dir", str(install_dir.resolve())])
    if is_windows():
        shell_execute = ctypes.windll.shell32.ShellExecuteW
        shell_execute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_int,
        ]
        shell_execute.restype = ctypes.c_void_p
        result = shell_execute(
            None,
            "runas",
            str(installer_path),
            subprocess.list2cmdline(arguments),
            str(installer_path.parent),
            1,
        )
        result_code = int(result or 0)
        if result_code <= 32:
            raise ApiError(f"Windows did not start the update installer (ShellExecute error {result_code})")
        return
    subprocess.Popen([str(installer_path), *arguments], cwd=str(installer_path.parent))


def play_alert_sound(audio_name: str = DEFAULT_AUDIO_NAME) -> None:
    if winsound is None:
        return
    sound_path = find_audio_file(audio_name) or find_audio_file(DEFAULT_AUDIO_NAME)
    if not sound_path:
        log(f"sound file not found: {audio_name}")
        return
    try:
        winsound.PlaySound(str(sound_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
    except RuntimeError as exc:
        log(f"sound failed: {exc}")


def wrap_for_canvas(text: str, width: int, max_lines: int) -> str:
    lines: list[str] = []
    for paragraph in (text or "").splitlines() or [""]:
        wrapped = textwrap.wrap(paragraph, width=width, replace_whitespace=False) or [""]
        lines.extend(wrapped)
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + ["..."]
    return "\n".join(lines)


def display_alert_time(value: str) -> str:
    value = safe_string(value)
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        formatted = parsed.strftime("%b %d, %Y %I:%M %p")
        formatted = re.sub(r"\b0(\d)\b", r"\1", formatted)
        zone = parsed.strftime("%Z")
        if zone:
            formatted = f"{formatted} {zone}"
        return formatted
    except ValueError:
        return value


def summarize_json(value: object) -> str:
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except TypeError:
        return str(value)


class AlertWindow:
    CANVAS_W = 800
    CANVAS_H = 480
    SCREEN_X = 0
    SCREEN_Y = 0
    SCREEN_W = 800
    SCREEN_H = 480

    def __init__(self, app: "MassNotifyApp", alert: AlertData) -> None:
        self.app = app
        self.alert = alert
        self.image_refs = []
        self._revealed = False
        self._image_pending = False
        self._image_results: queue.Queue[bytes | None] = queue.Queue(maxsize=1)
        self._after_ids: set[str] = set()
        self.window = Toplevel(app.root)
        self.window.withdraw()
        self.window.title("SLS Mass Notify Alert")
        self.window.resizable(False, False)
        self.window.configure(bg="#111111")
        self._set_icon(self.window)
        try:
            self.window.attributes("-topmost", True)
        except Exception:
            pass

        import tkinter as tk

        container = tk.Frame(self.window, bg="#111111", bd=0, highlightthickness=0)
        container.pack(fill="both", expand=True)
        self.canvas = self._build_canvas(container)
        self.canvas.pack(fill="both", expand=False)
        footer_bar = tk.Frame(container, bg="#111111", bd=0, highlightthickness=0)
        footer_bar.pack(fill="x")
        footer = tk.Label(
            footer_bar,
            text=ALERT_FOOTER_TEXT,
            bg="#111111",
            fg="#d8d8d8",
            font=("Segoe UI", 9),
            anchor="w",
            padx=10,
            pady=7,
        )
        footer.pack(side="left", fill="x", expand=True)
        dismiss = tk.Button(
            footer_bar,
            text="Dismiss",
            command=self.hide,
            font=("Segoe UI", 9),
            padx=12,
            pady=2,
        )
        dismiss.pack(side="right", padx=8, pady=5)
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self.window.bind("<Escape>", lambda _event: self.hide())
        self.window.bind("<FocusOut>", self._handle_focus_out)
        self.window.bind("<Deactivate>", self._handle_focus_out)
        self.window.bind("<Control-r>", lambda _event: self.show_raw_xml())
        self.window.bind("<Control-e>", lambda _event: self.show_recent_events())
        self.canvas.bind("<Button-1>", lambda _event: self.window.focus_set())
        self.canvas.bind("<Button-3>", lambda _event: self.show_raw_xml())

        self._place_notification()
        self._image_pending = self._load_screen_image()
        if self._image_pending:
            self._schedule(ALERT_IMAGE_WAIT_MS, self._finish_image_load)
        else:
            self._reveal()

    def _schedule(self, delay_ms: int, callback) -> str:
        after_id = ""

        def run_callback() -> None:
            self._after_ids.discard(after_id)
            callback()

        after_id = self.window.after(delay_ms, run_callback)
        self._after_ids.add(after_id)
        return after_id

    def _set_icon(self, window: Toplevel) -> None:
        icon = resource_path("favicon.ico")
        if icon.exists():
            try:
                window.iconbitmap(str(icon))
            except Exception:
                pass

    def _build_canvas(self, parent=None):
        import tkinter as tk

        canvas = tk.Canvas(
            parent or self.window,
            width=self.CANVAS_W,
            height=self.CANVAS_H,
            bg="#050505",
            highlightthickness=0,
            bd=0,
        )
        if self._is_announcement():
            self._draw_announcement_screen(canvas)
        else:
            self._draw_red_alert_screen(canvas)
        return canvas

    def _rounded_rect(self, canvas, x1, y1, x2, y2, radius, **kwargs) -> None:
        points = [
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            x2,
            y1,
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            x2,
            y2,
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            x1,
            y2,
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            x1,
            y1,
        ]
        canvas.create_polygon(points, smooth=True, **kwargs)

    def _is_announcement(self) -> bool:
        marker = normalize_key(" ".join(part for part in (self.alert.kind, self.alert.title, self.alert.event) if part))
        return "announcement" in marker

    def _alert_palette(self) -> dict[str, str]:
        if self.alert.background_color:
            background = self.alert.background_color
            header = self.alert.header_color or background
            accent = self.alert.accent_color or header
            text_color = self.alert.text_color or "#ffffff"
            return {
                "outer": header,
                "header": header,
                "body": background,
                "stripe_a": background,
                "stripe_b": background,
                "outline": accent,
                "text": text_color,
                "muted": text_color,
            }
        priority_text = " ".join(
            part for part in (self.alert.priority_label, self.alert.priority, self.alert.severity) if part
        ).lower()
        if any(word in priority_text for word in ("advisory", "notice")):
            return {
                "outer": "#211d04",
                "header": "#85743a",
                "body": "#d8b72f",
                "stripe_a": "#caa928",
                "stripe_b": "#e0c54c",
                "outline": "#fff0a3",
                "text": "#201800",
                "muted": "#3f3300",
            }
        if "urgent" in priority_text:
            return {
                "outer": "#221106",
                "header": "#8a5d3b",
                "body": "#d97221",
                "stripe_a": "#c7641d",
                "stripe_b": "#df8133",
                "outline": "#ffd1a6",
                "text": "#ffffff",
                "muted": "#fff1df",
            }
        return {
            "outer": "#1c0708",
            "header": "#806d73",
            "body": "#c94242",
            "stripe_a": "#b83a3d",
            "stripe_b": "#c64a4a",
            "outline": "#f0a7a7",
            "text": "#ffffff",
            "muted": "#f8dddd",
        }

    def _screen_text_parts(self) -> tuple[str, str, str, str]:
        title = self.alert.title or self.alert.event or "NWS ALERT"
        priority = self.alert.priority_label or self.alert.priority or self.alert.severity
        severity_parts = [part for part in (priority, self.alert.severity) if part]
        severity_line = " - ".join(dict.fromkeys(severity_parts))

        area = self.alert.area or self.alert.event
        time_parts = []
        effective = display_alert_time(self.alert.effective)
        expires = display_alert_time(self.alert.expires)
        if effective:
            time_parts.append(f"Effective: {effective}")
        if expires:
            time_parts.append(f"Until: {expires}")
        timing = "\n".join(time_parts)

        return title.upper(), severity_line.upper(), area, timing

    def _draw_red_alert_screen(self, canvas) -> None:
        sx, sy, sw, sh = self.SCREEN_X, self.SCREEN_Y, self.SCREEN_W, self.SCREEN_H
        palette = self._alert_palette()
        canvas.create_rectangle(sx, sy, sx + sw, sy + sh, fill=palette["outer"], outline="#050505")
        canvas.create_rectangle(sx + 2, sy + 2, sx + sw - 2, sy + sh - 2, fill=palette["body"], outline="")
        canvas.create_rectangle(sx + 4, sy + 4, sx + sw - 4, sy + 112, fill=palette["header"], outline="")
        canvas.create_rectangle(sx + 4, sy + 112, sx + sw - 4, sy + sh - 4, fill=palette["body"], outline="")
        for offset in range(120, sh - 8, 6):
            color = palette["stripe_b"] if offset % 12 else palette["stripe_a"]
            canvas.create_line(sx + 4, sy + offset, sx + sw - 4, sy + offset, fill=color)
        canvas.create_rectangle(sx + 2, sy + 2, sx + sw - 2, sy + sh - 2, outline=palette["outline"], width=2)

        title, priority, area, timing = self._screen_text_parts()
        canvas.create_text(
            sx + sw / 2,
            sy + 58,
            text=wrap_for_canvas(title, 25, 2),
            fill=palette["text"],
            justify="center",
            font=("Segoe UI", -34),
        )
        canvas.create_text(
            sx + sw / 2,
            sy + 178,
            text=wrap_for_canvas(priority, 30, 2),
            fill=palette["text"],
            justify="center",
            font=("Segoe UI", -30, "bold"),
        )
        canvas.create_text(
            sx + sw / 2,
            sy + 286,
            text=wrap_for_canvas(area, 44, 2),
            fill=palette["text"],
            justify="center",
            width=690,
            font=("Segoe UI", -24, "bold"),
        )
        if timing:
            canvas.create_text(
                sx + sw / 2,
                sy + 392,
                text=wrap_for_canvas(timing, 68, 2),
                fill=palette["muted"],
                justify="center",
                width=700,
                font=("Segoe UI", -17, "bold"),
            )

    def _announcement_text_parts(self) -> tuple[str, str, str]:
        title = self.alert.title or self.alert.event or "Announcement"
        body = self.alert.body or self.alert.description or self.alert.event or "Announcement"
        created = display_alert_time(self.alert.timestamp)
        return title, body, created

    def _draw_announcement_screen(self, canvas) -> None:
        sx, sy, sw, sh = self.SCREEN_X, self.SCREEN_Y, self.SCREEN_W, self.SCREEN_H
        title, body, _created = self._announcement_text_parts()
        background = self.alert.background_color or "#ffffff"
        header = self.alert.header_color or background
        accent = self.alert.accent_color or "#f5b82e"
        text_color = self.alert.text_color or "#172033"
        outline = self.alert.accent_color or "#d7e0eb"
        canvas.create_rectangle(sx, sy, sx + sw, sy + sh, fill=header, outline=outline)
        canvas.create_rectangle(sx + 2, sy + 2, sx + sw - 2, sy + sh - 2, fill=background, outline=outline)

        content_top = sy + 70 if self.alert.test_only else sy + 42
        if self.alert.test_only:
            canvas.create_rectangle(sx + 2, sy + 2, sx + sw - 2, sy + 40, fill="#7f1d1d", outline="")
            canvas.create_text(
                sx + sw / 2,
                sy + 21,
                text="TEST ONLY — NOT A LIVE EMERGENCY",
                fill="#ffffff",
                font=("Segoe UI", -15, "bold"),
            )
        canvas.create_rectangle(sx + 30, content_top, sx + 38, sy + sh - 42, fill=accent, outline="")
        canvas.create_text(
            sx + 62,
            content_top,
            text=wrap_for_canvas(title or "Announcement", 30, 2),
            fill=text_color,
            justify="left",
            anchor="nw",
            width=690,
            font=("Segoe UI Semibold", -29),
        )
        canvas.create_text(
            sx + 62,
            content_top + 105,
            text=wrap_for_canvas(body, 68, 7),
            fill=text_color,
            justify="left",
            anchor="nw",
            width=690,
            font=("Segoe UI", -19),
        )

    def _load_screen_image(self) -> bool:
        if self._is_announcement():
            return False
        image_url = resolve_image_url(self.alert.image_url, self.alert.source_endpoint)
        if not image_url:
            return False

        def worker() -> None:
            try:
                image_bytes = fetch_image_bytes(
                    image_url,
                    source_endpoint=self.alert.source_endpoint,
                    allow_invalid_certificate=self.alert.allow_invalid_certificate,
                    allow_legacy_media=self.alert.allow_legacy_media,
                )
            except Exception as exc:
                log(f"alert image fetch failed: {exc}")
                image_bytes = None
            try:
                self._image_results.put_nowait(image_bytes)
            except queue.Full:
                pass

        threading.Thread(target=worker, name="AlertImageLoader", daemon=True).start()
        self._schedule(40, self._poll_image_result)
        return True

    def _poll_image_result(self) -> None:
        if not self.window.winfo_exists() or not self._image_pending:
            return
        try:
            image_bytes = self._image_results.get_nowait()
        except queue.Empty:
            self._schedule(40, self._poll_image_result)
            return
        self._finish_image_load(image_bytes)

    def _finish_image_load(self, image_bytes: bytes | None = None) -> None:
        if not self.window.winfo_exists() or not self._image_pending:
            return
        self._image_pending = False
        if image_bytes:
            self._show_screen_image(image_bytes)
        self._reveal()

    def _show_screen_image(self, image_bytes: bytes) -> None:
        import tkinter as tk

        if not self.window.winfo_exists():
            return
        try:
            if Image is not None and ImageTk is not None:
                source = Image.open(io.BytesIO(image_bytes))
                if source.width * source.height > IMAGE_PIXEL_LIMIT:
                    raise ValueError("alert image dimensions are too large")
                image = source.convert("RGB")
                source.close()
                image = image.resize((self.SCREEN_W, self.SCREEN_H), Image.Resampling.LANCZOS)
                display = ImageTk.PhotoImage(image)
                self.image_refs = [display]
            else:
                encoded = base64.b64encode(image_bytes).decode("ascii")
                original = tk.PhotoImage(data=encoded)
                display = self._fit_photo_to_screen(original)
                self.image_refs = [original, display]
        except Exception as exc:
            log(f"alert image render failed: {exc}")
            return

        sx, sy, sw, sh = self.SCREEN_X, self.SCREEN_Y, self.SCREEN_W, self.SCREEN_H
        self.canvas.create_rectangle(sx, sy, sx + sw, sy + sh, fill="#111111", outline="#f0a7a7", width=2)
        self.canvas.create_image(sx, sy, image=display, anchor="nw")
        self.canvas.create_rectangle(sx, sy, sx + sw, sy + sh, outline="#f0a7a7", width=2)

    def _reveal(self) -> None:
        if self._revealed or not self.window.winfo_exists():
            return
        self._revealed = True
        self._place_notification()
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        self._schedule(750, self._drop_topmost)
        self._schedule(ALERT_AUTO_HIDE_MS, self.hide)

    def _fit_photo_to_screen(self, photo):
        width = photo.width()
        height = photo.height()
        if width <= 0 or height <= 0:
            return photo
        subsample = max(
            1,
            (width + self.SCREEN_W - 1) // self.SCREEN_W,
            (height + self.SCREEN_H - 1) // self.SCREEN_H,
        )
        if subsample > 1:
            return photo.subsample(subsample, subsample)
        zoom = min(self.SCREEN_W // width, self.SCREEN_H // height)
        if zoom > 1:
            return photo.zoom(zoom, zoom)
        return photo

    def _place_notification(self) -> None:
        self.window.update_idletasks()
        # A withdrawn Toplevel reports Tk's temporary 200x200 size. Center the
        # requested final dimensions so mapping the window cannot shift it.
        width = max(self.CANVAS_W, self.window.winfo_reqwidth())
        height = max(self.CANVAS_H, self.window.winfo_reqheight())
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        x = max(12, int((screen_w - width) / 2))
        y = max(12, int((screen_h - height) / 2))
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    def _drop_topmost(self) -> None:
        if self.window.winfo_exists():
            try:
                self.window.attributes("-topmost", False)
            except Exception:
                pass

    def _handle_focus_out(self, _event) -> None:
        self._schedule(140, self._lower_if_unfocused)

    def _lower_if_unfocused(self) -> None:
        if not self.window.winfo_exists():
            return
        focused = self.window.focus_displayof()
        if focused is None or not str(focused).startswith(str(self.window)):
            self._drop_topmost()
            try:
                self.window.lower()
            except Exception:
                pass

    def hide(self) -> None:
        if self.window.winfo_exists():
            self._image_pending = False
            for after_id in tuple(self._after_ids):
                try:
                    self.window.after_cancel(after_id)
                except Exception:
                    pass
            self._after_ids.clear()
            self.window.destroy()

    def show_raw_xml(self) -> None:
        content = self.alert.xml_payload or self.alert.raw_text or summarize_json(self.alert.raw)
        RawTextWindow(self.app.root, "Exact Yealink XML Payload", content)

    def show_recent_events(self) -> None:
        content = self.alert.recent_events or "No recent events were returned by the endpoint."
        RawTextWindow(self.app.root, "Recent Events", content)


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
            text=wrap_for_canvas(message, 48, 3),
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
        self.window.update_idletasks()
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        x = max(12, screen_w - self.WIDTH - 24)
        y = max(12, screen_h - self.HEIGHT - 64)
        self.window.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

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
        self.startup_var = BooleanVar(value=is_startup_enabled() or bool(cfg.get("startup_enabled", True)))
        self.auto_update_var = BooleanVar(value=bool(cfg.get("auto_update_enabled", True)))
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
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        width = max(940, min(1140, screen_w - 80))
        height = max(680, min(840, screen_h - 100))
        self.window.geometry(f"{width}x{height}")
        self.window.minsize(900, 640)

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
        ttk.Label(title_block, text="SLS Mass Notify settings", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            title_block,
            text="Configure PBX connections, notifications, and Windows startup behavior.",
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
            scroll_frame = ttk.Frame(parent, style="Surface.TFrame")
            scroll_frame.grid(row=0, column=0, sticky="nsew")
            canvas = Canvas(scroll_frame, bg="#111418", highlightthickness=0, bd=0)
            scrollbar = ttk.Scrollbar(scroll_frame, orient="vertical", command=canvas.yview)
            inner = ttk.Frame(canvas, padding=(4, 4, 14, 14), style="Surface.TFrame")
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")
            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            def on_mousewheel(event):
                units = int(-1 * (event.delta / 120)) if event.delta else 0
                if units == 0:
                    units = -1 if event.delta > 0 else 1
                canvas.yview_scroll(units, "units")
                return "break"

            def on_scroll_up(_event):
                canvas.yview_scroll(-1, "units")
                return "break"

            def on_scroll_down(_event):
                canvas.yview_scroll(1, "units")
                return "break"

            def bind_mousewheel(widget):
                widget.bind("<MouseWheel>", on_mousewheel)
                widget.bind("<Button-4>", on_scroll_up)
                widget.bind("<Button-5>", on_scroll_down)
                for child in widget.winfo_children():
                    bind_mousewheel(child)

            def update_scroll_region(_event=None):
                canvas.configure(scrollregion=canvas.bbox("all"))
                if canvas.winfo_width() > 1:
                    canvas.itemconfig(canvas_window, width=canvas.winfo_width())
                bind_mousewheel(inner)

            inner.bind("<Configure>", update_scroll_region)
            canvas.bind("<Configure>", update_scroll_region)
            return inner

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
            text="Monitoring, Windows startup, and automatic updates.",
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
            text="Install verified updates automatically",
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
        ttk.Label(endpoint_heading, text="Three profiles available", style="Hint.TLabel").pack(side="right", anchor="e")
        ttk.Label(
            endpoints_frame,
            text="Live profiles use an authenticated stream. Secrets are stored in Windows Credential Manager.",
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
        self.status_label.grid(row=0, column=0, sticky="ew", padx=(0, 12))

        actions = ttk.Frame(footer, style="StatusBar.TFrame")
        actions.grid(row=0, column=1, sticky="e")
        self.test_button = ttk.Button(actions, text="Test connections", command=self.test_now)
        self.test_button.pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Save changes", style="Accent.TButton", command=self.save).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Close", command=self.close).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Exit", command=self.quit_app).pack(side="left")


    def _build_endpoint_tab(self, parent: ttk.Frame, index: int, endpoint: dict) -> None:
        auth_mode = normalize_auth_mode(endpoint.get("auth_mode"), bool(endpoint.get("no_token")))
        delivery_mode = normalize_delivery_mode(
            endpoint.get("delivery_mode"), existing_url=safe_string(endpoint.get("endpoint"))
        )
        form = {
            "name": StringVar(value=safe_string(endpoint.get("name")) or f"PBX {index + 1}"),
            "endpoint": StringVar(value=safe_string(endpoint.get("endpoint"))),
            "token": StringVar(value=unprotect_secret(safe_string(endpoint.get("token")))),
            "username": StringVar(value=safe_string(endpoint.get("username"))),
            "password": StringVar(value=unprotect_secret(safe_string(endpoint.get("password")))),
            "auth_mode": StringVar(value=AUTH_LABELS.get(auth_mode, AUTH_LABELS[AUTH_TOKEN])),
            "delivery_mode": StringVar(value=DELIVERY_LABELS[delivery_mode]),
            "poll_seconds": IntVar(value=normalize_poll_seconds(endpoint.get("poll_seconds"))),
            "enabled": BooleanVar(value=bool(endpoint.get("enabled", index == 0))),
            "reconnect_automatically": BooleanVar(value=bool(endpoint.get("reconnect_automatically", True))),
            "allow_invalid_certificate": BooleanVar(value=bool(endpoint.get("allow_invalid_certificate", False))),
            "allow_legacy_media": BooleanVar(value=bool(endpoint.get("allow_legacy_media", False))),
            "show_token": BooleanVar(value=False),
            "token_entry": None,
            "username_entry": None,
            "password_entry": None,
            "token_widgets": [],
            "basic_widgets": [],
            "show_secrets_widget": None,
            "auth_hint_label": None,
            "warning_label": None,
            "auth_combo": None,
            "legacy_settings_frame": None,
        }
        self.endpoint_forms.append(form)

        ttk.Label(parent, text=f"PBX profile {index + 1}", style="Field.TLabel", font=("Segoe UI Semibold", 11)).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        ttk.Checkbutton(parent, text="Enabled", variable=form["enabled"], style="Inset.TCheckbutton").grid(
            row=1, column=0, sticky="w"
        )
        delivery_combo = ttk.Combobox(
            parent,
            textvariable=form["delivery_mode"],
            values=list(DELIVERY_LABELS.values()),
            state="readonly",
            width=59,
        )
        delivery_combo.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(12, 0))
        delivery_combo.bind("<<ComboboxSelected>>", lambda _event, idx=index: self.update_delivery_mode(idx))

        ttk.Label(parent, text="Display name", style="Field.TLabel").grid(row=2, column=0, sticky="w", pady=(14, 0))
        ttk.Label(parent, text="PBX address", style="Field.TLabel").grid(
            row=2, column=1, columnspan=3, sticky="w", pady=(14, 0)
        )
        ttk.Entry(parent, textvariable=form["name"], width=24).grid(
            row=3, column=0, sticky="ew", pady=(4, 0), padx=(0, 10)
        )
        ttk.Entry(parent, textvariable=form["endpoint"], width=64).grid(
            row=3, column=1, columnspan=3, sticky="ew", pady=(4, 0)
        )

        legacy_settings = ttk.Frame(parent, style="Inset.TFrame")
        legacy_settings.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        form["legacy_settings_frame"] = legacy_settings
        ttk.Label(legacy_settings, text="Legacy authentication", style="Field.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        auth_combo = ttk.Combobox(
            legacy_settings,
            textvariable=form["auth_mode"],
            values=[AUTH_LABELS[AUTH_BASIC], AUTH_LABELS[AUTH_TOKEN]],
            state="readonly",
            width=22,
        )
        auth_combo.grid(row=1, column=0, sticky="w", pady=(4, 0), padx=(0, 24))
        auth_combo.bind("<<ComboboxSelected>>", lambda _event, idx=index: self.update_auth_mode(idx))
        form["auth_combo"] = auth_combo
        ttk.Label(legacy_settings, text="Polling interval", style="Field.TLabel").grid(
            row=0, column=1, columnspan=2, sticky="w"
        )
        ttk.Spinbox(
            legacy_settings,
            from_=5,
            to=3600,
            textvariable=form["poll_seconds"],
            width=8,
        ).grid(row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Label(legacy_settings, text="seconds", style="Field.TLabel").grid(
            row=1, column=2, sticky="w", padx=(8, 0), pady=(4, 0)
        )
        show_secrets = ttk.Checkbutton(
            parent,
            text="Show secrets",
            variable=form["show_token"],
            command=lambda idx=index: self.toggle_token(idx),
            style="Inset.TCheckbutton",
        )
        show_secrets.grid(row=5, column=3, sticky="e", padx=(18, 0), pady=(14, 0))
        form["show_secrets_widget"] = show_secrets

        token_label = ttk.Label(parent, text="Bearer token", style="Field.TLabel")
        token_label.grid(row=6, column=0, columnspan=4, sticky="w", pady=(5, 0))
        token_entry = ttk.Entry(parent, textvariable=form["token"], width=80, show="*")
        token_entry.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        form["token_entry"] = token_entry
        form["token_widgets"] = [token_label, token_entry]

        username_label = ttk.Label(parent, text="Username", style="Field.TLabel")
        username_label.grid(row=6, column=0, sticky="w", pady=(5, 0))
        username_entry = ttk.Entry(parent, textvariable=form["username"], width=28)
        username_entry.grid(row=7, column=0, sticky="ew", pady=(4, 0), padx=(0, 10))
        form["username_entry"] = username_entry
        password_label = ttk.Label(parent, text="Password", style="Field.TLabel")
        password_label.grid(row=6, column=1, sticky="w", pady=(5, 0))
        password_entry = ttk.Entry(parent, textvariable=form["password"], width=36, show="*")
        password_entry.grid(row=7, column=1, columnspan=3, sticky="ew", pady=(4, 0))
        form["password_entry"] = password_entry
        form["basic_widgets"] = [username_label, username_entry, password_label, password_entry]
        options = ttk.Frame(parent, style="Inset.TFrame")
        options.grid(row=8, column=0, columnspan=4, sticky="w", pady=(12, 0))
        ttk.Checkbutton(
            options,
            text="Reconnect automatically",
            variable=form["reconnect_automatically"],
            style="Inset.TCheckbutton",
        ).grid(row=0, column=0, sticky="w", padx=(0, 16))
        ttk.Checkbutton(
            options,
            text="Allow invalid certificate (unsafe)",
            variable=form["allow_invalid_certificate"],
            style="Inset.TCheckbutton",
            command=lambda idx=index: self.update_endpoint_warning(idx),
        ).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(
            options,
            text="Allow legacy HTTP media (unsafe)",
            variable=form["allow_legacy_media"],
            style="Inset.TCheckbutton",
            command=lambda idx=index: self.update_endpoint_warning(idx),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        auth_hint = ttk.Label(
            parent,
            text="Bearer token uses Authorization: Bearer. Username/password uses HTTP Basic auth. No authentication sends no Authorization header.",
            style="InsetHint.TLabel",
            wraplength=820,
        )
        auth_hint.grid(row=9, column=0, columnspan=4, sticky="w", pady=(10, 0))
        form["auth_hint_label"] = auth_hint
        warning_label = ttk.Label(parent, text="", style="InsetHint.TLabel", wraplength=820)
        warning_label.grid(row=10, column=0, columnspan=4, sticky="w", pady=(7, 0))
        form["warning_label"] = warning_label
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.columnconfigure(2, weight=0)
        parent.columnconfigure(3, weight=0)
        form["endpoint"].trace_add("write", lambda *_args, idx=index: self.update_endpoint_warning(idx))
        form["auth_mode"].trace_add("write", lambda *_args, idx=index: self.update_auth_mode(idx))
        form["delivery_mode"].trace_add("write", lambda *_args, idx=index: self.update_delivery_mode(idx))
        self.update_delivery_mode(index)

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
        width = self.window.winfo_width()
        height = self.window.winfo_height()
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        x = max(0, int((screen_w - width) / 2))
        y = max(0, int((screen_h - height) / 3))
        self.window.geometry(f"+{x}+{y}")

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
        play_alert_sound(name)

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
        auth_mode = self.form_auth_mode(form)
        address = form["endpoint"].get().strip()
        try:
            normalized_address = normalize_pbx_address(address) if address else ""
        except ValueError:
            normalized_address = address
        warnings = endpoint_security_warnings(normalized_address, auth_mode)
        if bool(form["allow_invalid_certificate"].get()):
            warnings.append(("red", "Unsafe: TLS certificate and hostname validation is disabled for this PBX."))
        if bool(form["allow_legacy_media"].get()):
            warnings.append(("red", "Unsafe: unencrypted HTTP alert media is allowed for this PBX."))
        if not warnings:
            label.configure(text="Security status: PBX and authentication settings look normal.", style="InsetHint.TLabel")
            return
        severity, _message = warnings[0]
        text = "\n".join(message for _severity, message in warnings)
        label.configure(text=text, style="RedWarning.TLabel" if severity == "red" else "YellowWarning.TLabel")

    def form_auth_mode(self, form: dict) -> str:
        selected = form["auth_mode"].get()
        for mode, label in AUTH_LABELS.items():
            if selected == label:
                return mode
        return AUTH_TOKEN

    def form_delivery_mode(self, form: dict) -> str:
        selected = form["delivery_mode"].get()
        for mode, label in DELIVERY_LABELS.items():
            if selected == label:
                return mode
        return DELIVERY_LIVE

    def update_delivery_mode(self, index: int) -> None:
        form = self.endpoint_forms[index]
        live = self.form_delivery_mode(form) == DELIVERY_LIVE
        legacy_settings = form.get("legacy_settings_frame")
        auth_combo = form.get("auth_combo")
        if live:
            form["auth_mode"].set(AUTH_LABELS[AUTH_BASIC])
            if legacy_settings is not None:
                legacy_settings.grid_remove()
        else:
            if self.form_auth_mode(form) not in {AUTH_BASIC, AUTH_TOKEN}:
                form["auth_mode"].set(AUTH_LABELS[AUTH_BASIC])
            if legacy_settings is not None:
                legacy_settings.grid()
            if auth_combo is not None:
                auth_combo.configure(state="readonly")
        self.update_auth_mode(index)

    def toggle_token(self, index: int) -> None:
        form = self.endpoint_forms[index]
        show = "" if form["show_token"].get() else "*"
        for key in ("token_entry", "password_entry"):
            entry = form.get(key)
            if entry is not None:
                entry.configure(show=show)

    def update_auth_mode(self, index: int) -> None:
        form = self.endpoint_forms[index]
        mode = self.form_auth_mode(form)
        for widget in form.get("token_widgets", []):
            if mode == AUTH_TOKEN:
                widget.grid()
            else:
                widget.grid_remove()
        for widget in form.get("basic_widgets", []):
            if mode == AUTH_BASIC:
                widget.grid()
            else:
                widget.grid_remove()
        show_secrets = form.get("show_secrets_widget")
        if show_secrets is not None:
            if mode == AUTH_NONE:
                show_secrets.grid_remove()
            else:
                show_secrets.grid()
        auth_hint = form.get("auth_hint_label")
        if auth_hint is not None:
            hints = {
                AUTH_TOKEN: "The token is sent as an Authorization: Bearer header and stored in Windows Credential Manager.",
                AUTH_BASIC: "Desktop-specific credentials use HTTP Basic over HTTPS and are stored in Windows Credential Manager.",
                AUTH_NONE: "No Authorization header will be sent. Use this only for a trusted endpoint designed for anonymous access.",
            }
            auth_hint.configure(text=hints[mode])
            if self.form_delivery_mode(form) == DELIVERY_LIVE:
                auth_hint.configure(
                    text="Live mode uses desktop-specific Basic credentials, waits for the authenticated SSE event, and resumes with Last-Event-ID."
                )
        for key in ("token_entry", "username_entry", "password_entry"):
            entry = form.get(key)
            if entry is not None:
                entry.configure(state="normal")
        self.update_endpoint_warning(index)

    def collect_settings(self) -> list[dict] | None:
        endpoints: list[dict] = []
        security_messages: list[str] = []
        active_count = 0
        for index, form in enumerate(self.endpoint_forms):
            name = form["name"].get().strip() or f"PBX {index + 1}"
            raw_url = form["endpoint"].get().strip()
            try:
                url = normalize_pbx_address(raw_url) if raw_url else ""
            except ValueError as exc:
                messagebox.showerror("PBX address", f"PBX profile {index + 1}: {exc}")
                return None
            token = form["token"].get().strip()
            username = form["username"].get().strip()
            password = form["password"].get().strip()
            auth_mode = self.form_auth_mode(form)
            delivery_mode = self.form_delivery_mode(form)
            if delivery_mode == DELIVERY_LIVE:
                auth_mode = AUTH_BASIC
            try:
                endpoint_interval = normalize_poll_seconds(form["poll_seconds"].get())
            except Exception:
                endpoint_interval = DEFAULT_POLL_SECONDS
            no_token = auth_mode == AUTH_NONE
            endpoint_enabled = bool(form["enabled"].get())

            if url and not endpoint_url_allowed(url):
                messagebox.showerror(
                    "PBX address",
                    f"PBX profile {index + 1} must be a valid http:// or https:// hostname or URL.",
                )
                return None
            if endpoint_enabled and url:
                if delivery_mode == DELIVERY_LIVE and urlparse(url).scheme.lower() != "https":
                    messagebox.showerror(
                        "HTTPS required",
                        f"PBX profile {index + 1} must use HTTPS for the live authenticated handshake.",
                    )
                    return None
                if auth_mode == AUTH_TOKEN and not token:
                    messagebox.showerror(
                        "Token required",
                        f"Endpoint {index + 1} needs a bearer token, or choose a different authentication mode.",
                    )
                    return None
                if auth_mode == AUTH_BASIC and (not username or not password):
                    messagebox.showerror(
                        "Username/password required",
                        f"Endpoint {index + 1} needs both a username and password for username/password auth.",
                    )
                    return None
                for _severity, warning in endpoint_security_warnings(url, auth_mode):
                    security_messages.append(f"Endpoint {index + 1}: {warning}")
                if bool(form["allow_invalid_certificate"].get()):
                    security_messages.append(
                        f"PBX profile {index + 1}: TLS certificate and hostname validation will be disabled."
                    )
                if bool(form["allow_legacy_media"].get()):
                    security_messages.append(
                        f"PBX profile {index + 1}: unencrypted HTTP alert media will be allowed."
                    )
                active_count += 1

            stored_token = ""
            stored_password = ""
            if auth_mode == AUTH_TOKEN:
                stored_token = token
            elif auth_mode == AUTH_BASIC:
                stored_password = password

            endpoints.append(
                {
                    "name": name,
                    "endpoint": url,
                    "enabled": endpoint_enabled,
                    "auth_mode": auth_mode,
                    "delivery_mode": delivery_mode,
                    "poll_seconds": endpoint_interval,
                    "reconnect_automatically": bool(form["reconnect_automatically"].get()),
                    "allow_invalid_certificate": bool(form["allow_invalid_certificate"].get()),
                    "allow_legacy_media": bool(form["allow_legacy_media"].get()),
                    "no_token": no_token,
                    "token": stored_token,
                    "username": username if auth_mode == AUTH_BASIC else "",
                    "password": stored_password,
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
        if security_messages and not messagebox.askyesno(
            "Endpoint Security Warnings",
            "Review these endpoint security warnings before saving:\n\n"
            + "\n\n".join(security_messages)
            + "\n\nSave these settings anyway?",
        ):
            return None
        for index, endpoint in enumerate(endpoints):
            auth_mode = normalize_auth_mode(endpoint.get("auth_mode"), bool(endpoint.get("no_token")))
            if auth_mode == AUTH_TOKEN:
                endpoint["token"] = protect_profile_secret(index, "token", safe_string(endpoint.get("token")))
                endpoint["password"] = ""
                delete_profile_secret(index, "password")
            elif auth_mode == AUTH_BASIC:
                endpoint["password"] = protect_profile_secret(
                    index, "password", safe_string(endpoint.get("password"))
                )
                endpoint["token"] = ""
                delete_profile_secret(index, "token")
            else:
                endpoint["token"] = ""
                endpoint["password"] = ""
                delete_profile_secret(index, "token")
                delete_profile_secret(index, "password")
        return endpoints

    def save(self) -> bool:
        collected = self.collect_settings()
        if collected is None:
            return False
        endpoints = collected
        self.app.update_settings(
            endpoints=endpoints,
            enabled=self.enabled_var.get(),
            startup_enabled=self.startup_var.get(),
            auto_update_enabled=self.auto_update_var.get(),
            audio_sound=safe_audio_name(self.audio_var.get() or DEFAULT_AUDIO_NAME),
        )
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
        self.status_label.configure(text="Testing PBX connections and waiting for authenticated handshakes...")
        self.app.test_now(self.finish_connection_test)

    def finish_connection_test(self, message: str) -> None:
        if not self.window.winfo_exists():
            return
        self.status_label.configure(text=message)
        if self.test_button is not None:
            self.test_button.configure(state="normal")

    def close(self) -> None:
        self.window.destroy()
        self.app.settings_window = None

    def quit_app(self) -> None:
        self.app.shutdown()


def endpoint_poll_seconds(endpoint_cfg: dict, fallback: int = DEFAULT_POLL_SECONDS) -> int:
    return normalize_poll_seconds(endpoint_cfg.get("poll_seconds", fallback), fallback)


def endpoint_worker_signature(endpoint_cfg: dict, poll_seconds: int | None = None) -> str:
    keys = (
        "endpoint",
        "enabled",
        "auth_mode",
        "token",
        "username",
        "password",
        "delivery_mode",
        "reconnect_automatically",
        "allow_invalid_certificate",
        "allow_legacy_media",
    )
    selected = {key: endpoint_cfg.get(key) for key in keys}
    selected["poll_seconds"] = endpoint_poll_seconds(
        endpoint_cfg, DEFAULT_POLL_SECONDS if poll_seconds is None else poll_seconds
    )
    return json.dumps(selected, sort_keys=True, separators=(",", ":"))


def notification_routed_to_client(payload: object, endpoint_cfg: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    auth_mode = normalize_auth_mode(endpoint_cfg.get("auth_mode"), bool(endpoint_cfg.get("no_token")))
    # Preserve compatibility for pre-handshake bearer/anonymous sources. Desktop Basic profiles
    # use the PBX routing contract as a defense-in-depth boundary.
    if auth_mode != AUTH_BASIC:
        return True
    if payload.get("desktop_all") is True:
        return True
    username = safe_string(endpoint_cfg.get("username"))
    recipients = payload.get("desktop_recipients")
    if isinstance(recipients, str):
        recipients = [recipients]
    if isinstance(recipients, list):
        return username in {safe_string(item) for item in recipients}
    return False


def polling_records(data: object, last_event_id: str) -> list[dict]:
    if not isinstance(data, dict):
        return []
    latest = data.get("latest")
    events = [item for item in data.get("events", []) if isinstance(item, dict)] if isinstance(data.get("events"), list) else []
    if not last_event_id:
        return [latest] if isinstance(latest, dict) else []
    if isinstance(latest, dict) and safe_string(lookup(latest, EVENT_ID_KEYS)) == last_event_id:
        return []
    start = 0
    for position, record in enumerate(events):
        if safe_string(lookup(record, EVENT_ID_KEYS)) == last_event_id:
            if events and isinstance(latest, dict) and safe_string(lookup(events[0], EVENT_ID_KEYS)) == safe_string(
                lookup(latest, EVENT_ID_KEYS)
            ):
                events = events[:position]
                start = 0
            else:
                start = position + 1
            break
    records = events[start:]
    if isinstance(latest, dict):
        latest_id = safe_string(lookup(latest, EVENT_ID_KEYS))
        if not any(safe_string(lookup(item, EVENT_ID_KEYS)) == latest_id for item in records):
            records.append(latest)
    return records


class EndpointTransportWorker:
    def __init__(self, app: "MassNotifyApp", index: int, endpoint_cfg: dict, poll_seconds: int) -> None:
        self.app = app
        self.index = index
        self.endpoint_cfg = normalize_endpoint(endpoint_cfg, index)
        self.poll_seconds = max(5, int(poll_seconds))
        self.signature = endpoint_worker_signature(self.endpoint_cfg, self.poll_seconds)
        self.stop_event = threading.Event()
        self.response_lock = threading.Lock()
        self.response = None
        self.last_activity = 0.0
        self.session_id = ""
        self.client_id = ""
        self.server_retry_seconds = 1.0
        self.authenticated_event = threading.Event()
        self.catchup_thread: threading.Thread | None = None
        self.thread = threading.Thread(
            target=self.run,
            name=f"PBXTransport-{index + 1}",
            daemon=True,
        )

    def start(self) -> None:
        mode = normalize_delivery_mode(
            self.endpoint_cfg.get("delivery_mode"), existing_url=safe_string(self.endpoint_cfg.get("endpoint"))
        )
        if mode == DELIVERY_LIVE:
            self.catchup_thread = threading.Thread(
                target=self._run_live_catchup,
                name=f"PBXLiveCatchup-{self.index + 1}",
                daemon=True,
            )
            self.catchup_thread.start()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.authenticated_event.set()
        with self.response_lock:
            response = self.response
            self.response = None
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

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
        self.app.set_transport_status(self.index, state, detail)

    def _fault(self, message: str) -> None:
        key = f"endpoint-{self.index}"
        self.app.record_fault(key, message)
        log(message)

    def run(self) -> None:
        mode = normalize_delivery_mode(
            self.endpoint_cfg.get("delivery_mode"), existing_url=safe_string(self.endpoint_cfg.get("endpoint"))
        )
        if mode == DELIVERY_LIVE:
            self._run_live()
        else:
            self._run_polling()

    def _run_polling(self) -> None:
        name = endpoint_display_name(self.index, self.endpoint_cfg)
        while not self.stop_event.is_set():
            self._status("CONNECTING", f"{name}: checking legacy JSON fallback")
            try:
                url = pbx_transport_url(self.endpoint_cfg, DELIVERY_POLL)
                data, raw_text = fetch_endpoint(
                    url,
                    endpoint_auth_headers(self.endpoint_cfg),
                    allow_invalid_certificate=bool(self.endpoint_cfg.get("allow_invalid_certificate", False)),
                )
                ensure_api_ok(data)
                last_id = self.app.get_endpoint_state(self.index, "last_event_id")
                for record in polling_records(data, last_id):
                    self._accept_payload(record, json.dumps(record, ensure_ascii=False))
                self.app.clear_fault(f"endpoint-{self.index}")
                self._status(
                    "POLLING",
                    f"{name}: legacy polling OK at {datetime.now().strftime('%H:%M:%S')}",
                )
            except UnauthorizedError as exc:
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
                if self.stop_event.wait(60):
                    return
                continue
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                message = f"{name}: polling failed: {safe_string(exc)}"
                self._status("RECONNECTING", message)
                self._fault(message)
            if self.stop_event.wait(self.poll_seconds):
                return

    def _run_live(self) -> None:
        name = endpoint_display_name(self.index, self.endpoint_cfg)
        backoff_index = 0
        while not self.stop_event.is_set():
            self.authenticated_event.clear()
            authenticated_this_attempt = False
            self.authenticated_this_attempt = False
            self._status("CONNECTING", f"{name}: opening authenticated live stream")
            try:
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
                delay = random.uniform(0.1, 0.8) if reconnect_requested else self.server_retry_seconds
                self._status("RECONNECTING", f"{name}: renewing live stream")
                if self.stop_event.wait(delay):
                    return

                continue
            except UnauthorizedError as exc:
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
                if self.stop_event.wait(60):
                    return
                continue
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                if self.authenticated_this_attempt:
                    backoff_index = 0
                message = f"{name}: live transport fault: {safe_string(exc)}"
                self._fault(message)
                if not bool(self.endpoint_cfg.get("reconnect_automatically", True)):
                    self._status("DISCONNECTED", message)
                    self.stop_event.wait()
                    return
                delay = RECONNECT_BACKOFF_SECONDS[min(backoff_index, len(RECONNECT_BACKOFF_SECONDS) - 1)]
                backoff_index = min(backoff_index + 1, len(RECONNECT_BACKOFF_SECONDS) - 1)
                delay = min(30.0, delay + random.uniform(0.0, max(0.25, delay * 0.2)))
                self._status("RECONNECTING", f"{message}; retrying in {delay:.1f}s")
                if self.stop_event.wait(delay):
                    return

    def _run_live_catchup(self) -> None:
        """Recover notifications the PBX did not emit on an authenticated live stream."""
        while not self.stop_event.is_set():
            if not self.authenticated_event.wait(1):
                continue
            if self.stop_event.is_set():
                return
            try:
                accepted = self._live_catchup_once()
                if accepted:
                    log(
                        f"{endpoint_display_name(self.index, self.endpoint_cfg)} recovered "
                        f"{accepted} missed live notification{'s' if accepted != 1 else ''}"
                    )
                delay = LIVE_CATCHUP_SECONDS
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                log(
                    f"{endpoint_display_name(self.index, self.endpoint_cfg)} live notification catch-up failed: "
                    f"{safe_string(exc)}"
                )
                delay = max(15, LIVE_CATCHUP_SECONDS)
            if self.stop_event.wait(delay):
                return

    def _live_catchup_once(self) -> int:
        data, raw_text = fetch_endpoint(
            pbx_transport_url(self.endpoint_cfg, DELIVERY_POLL),
            endpoint_auth_headers(self.endpoint_cfg),
            allow_invalid_certificate=bool(self.endpoint_cfg.get("allow_invalid_certificate", False)),
        )
        ensure_api_ok(data)
        last_id = self.app.get_endpoint_state(self.index, "last_event_id")
        accepted = 0
        for record in polling_records(data, last_id):
            if self._accept_payload(
                record,
                json.dumps(record, ensure_ascii=False) if isinstance(record, dict) else raw_text,
                delivery_source="live catch-up",
            ):
                accepted += 1
        return accepted

    def _consume_stream(self, response) -> tuple[bool, bool]:
        authenticated = False
        reconnect_requested = False

        def activity() -> None:
            self.last_activity = time.monotonic()

        for event in iter_sse_events(response, on_activity=activity, stop_event=self.stop_event):
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
                ):
                    raise StreamProtocolError("PBX rejected the live authentication handshake.")
                authenticated = True
                self.authenticated_this_attempt = True
                self.authenticated_event.set()
                self.session_id = bounded_text(payload.get("session_id"), 160)
                self.client_id = bounded_text(payload.get("client_id"), 160)
                self.app.clear_fault(f"endpoint-{self.index}")
                unsafe = " (certificate validation disabled)" if self.endpoint_cfg.get("allow_invalid_certificate") else ""
                log(f"{endpoint_display_name(self.index, self.endpoint_cfg)} live handshake authenticated")
                self._status(
                    "AUTHENTICATED",
                    f"{endpoint_display_name(self.index, self.endpoint_cfg)}: live authenticated{unsafe}",
                )
                continue
            if not authenticated:
                raise StreamProtocolError(f"PBX sent '{event.name}' before authentication completed.")
            if event.name == "notification":
                try:
                    payload = json.loads(event.data)
                except json.JSONDecodeError as exc:
                    raise StreamProtocolError("PBX sent invalid notification JSON.") from exc
                self._accept_payload(payload, event.data, event.event_id, delivery_source="live stream")
            elif event.name == "reconnect":
                reconnect_requested = True
                break
        return authenticated, reconnect_requested

    def _accept_payload(
        self,
        payload: object,
        raw_text: str,
        stream_event_id: str = "",
        *,
        delivery_source: str = "legacy polling",
    ) -> bool:
        if not notification_routed_to_client(payload, self.endpoint_cfg):
            event_id = safe_string(lookup(payload, EVENT_ID_KEYS)) if isinstance(payload, dict) else ""
            log(
                f"{endpoint_display_name(self.index, self.endpoint_cfg)} ignored {delivery_source} notification"
                f"{f' {event_id}' if event_id else ''} outside its desktop route"
            )
            return False
        alert = extract_alert(payload, raw_text)
        if stream_event_id:
            alert.event_id = stream_event_id
        normalize_alert_urls(alert, safe_string(self.endpoint_cfg.get("endpoint")), self.endpoint_cfg)
        accepted = self.app.accept_alert(self.index, alert, self.signature)
        if accepted:
            log(
                f"{endpoint_display_name(self.index, self.endpoint_cfg)} accepted {delivery_source} "
                f"{alert.kind or 'notification'}{f' {alert.event_id}' if alert.event_id else ''}"
            )
        if accepted and alert.event_id:
            # The same worker reconnects after the PBX's bounded stream closes, so keep its
            # request snapshot current as well as the persisted profile state.
            self.endpoint_cfg["last_event_id"] = alert.event_id
        return accepted


class MassNotifyApp:
    def __init__(self, root: Tk, show_settings_on_start: bool) -> None:
        self.root = root
        self.root.withdraw()
        self.root.title(APP_DISPLAY_NAME)
        icon = resource_path("favicon.ico")
        if icon.exists():
            try:
                self.root.iconbitmap(str(icon))
            except Exception:
                pass

        self.config_lock = threading.RLock()
        self.config = load_config()
        self.stop_event = threading.Event()
        self.wakeup_event = threading.Event()
        self.connection_test_event = threading.Event()
        self.worker_lock = threading.RLock()
        self.workers: dict[int, EndpointTransportWorker] = {}
        self.faults_lock = threading.RLock()
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.settings_window: SettingsWindow | None = None
        self.status_text = "Waiting for PBX connection."
        self.transport_statuses: dict[int, tuple[str, str]] = {}
        self.faults: dict[str, dict] = {}

        self.command_thread = threading.Thread(target=self.command_server, name="CommandServer", daemon=True)
        self.command_thread.start()

        self.monitor_thread = threading.Thread(target=self.monitor_loop, name="PBXMonitor", daemon=True)
        self.monitor_thread.start()

        self.update_thread = threading.Thread(target=self.update_loop, name="AutoUpdater", daemon=True)
        self.update_thread.start()

        self.root.after(200, self.process_ui_queue)

        if show_settings_on_start or not self.is_configured():
            self.root.after(250, self.show_settings)

    def is_configured(self) -> bool:
        with self.config_lock:
            return bool(active_endpoints(self.config))

    def get_config(self) -> dict:
        with self.config_lock:
            return normalize_config(dict(self.config))

    def get_endpoint_state(self, index: int, key: str) -> str:
        with self.config_lock:
            endpoints = normalize_endpoints(self.config)
            if 0 <= index < len(endpoints):
                return safe_string(endpoints[index].get(key, ""))
        return ""

    def update_settings(
        self,
        *,
        endpoints: list[dict],
        enabled: bool,
        startup_enabled: bool,
        auto_update_enabled: bool,
        audio_sound: str,
    ) -> None:
        with self.config_lock:
            normalized_endpoints = normalize_endpoints({"endpoints": endpoints})
            first = normalized_endpoints[0]
            self.config["endpoints"] = normalized_endpoints
            self.config["endpoint"] = first.get("endpoint", "")
            self.config["token"] = first.get("token", "")
            self.config["no_token"] = bool(first.get("no_token", False))
            self.config["last_event_id"] = first.get("last_event_id", "")
            self.config["last_fingerprint"] = first.get("last_fingerprint", "")
            self.config["enabled"] = bool(enabled)
            self.config["startup_enabled"] = bool(startup_enabled)
            self.config["auto_update_enabled"] = bool(auto_update_enabled)
            self.config["audio_sound"] = safe_audio_name(audio_sound or DEFAULT_AUDIO_NAME)
            self.config = normalize_config(self.config)
            save_config(self.config)
        set_startup_enabled(startup_enabled)
        self.wakeup_event.set()

    def command_server(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((IPC_HOST, IPC_PORT))
                server.listen(5)
                server.settimeout(1)
                while not self.stop_event.is_set():
                    try:
                        client, _ = server.accept()
                    except socket.timeout:
                        continue
                    with client:
                        data = client.recv(256).decode("utf-8", errors="ignore").strip()
                    if data == "SHOW_SETTINGS":
                        self.ui_queue.put(("settings", None))
        except OSError as exc:
            log(f"command server failed: {exc}")

    def record_fault(self, key: str, message: str) -> None:
        now = time.monotonic()
        with self.faults_lock:
            fault = self.faults.get(key)
            if fault is None or fault.get("message") != message:
                self.faults[key] = {"message": message, "started_at": now, "notified": False}
                return
            if (
                fault.get("started_at") is not None
                and not fault.get("notified")
                and now - float(fault.get("started_at", now)) >= FAULT_NOTIFY_SECONDS
            ):
                fault["notified"] = True
                self.ui_queue.put(
                    (
                        "fault",
                        f"{message} This has been unresolved for at least 5 minutes.",
                    )
                )

    def clear_fault(self, key: str | None = None) -> None:
        with self.faults_lock:
            if key is None:
                self.faults.clear()
            else:
                self.faults.pop(key, None)

    def set_transport_status(self, index: int, state: str, detail: str) -> None:
        with self.worker_lock:
            self.transport_statuses[index] = (state, detail)
            states = [value[0] for value in self.transport_statuses.values()]
            if "AUTHENTICATED" in states:
                live_count = states.count("AUTHENTICATED")
                self.status_text = f"Live authenticated: {live_count} PBX profile{'s' if live_count != 1 else ''}. {detail}"
            else:
                self.status_text = detail
        self.ui_queue.put(("status", self.status_text))

    def accept_alert(self, index: int, alert: AlertData, worker_signature: str = "") -> bool:
        dedupe_id = safe_string(alert.event_id)
        with self.config_lock:
            self.config = normalize_config(self.config)
            if not 0 <= index < len(self.config["endpoints"]):
                return False
            endpoint = self.config["endpoints"][index]
            current_interval = endpoint_poll_seconds(
                endpoint, normalize_poll_seconds(self.config.get("poll_seconds", DEFAULT_POLL_SECONDS))
            )
            if worker_signature and endpoint_worker_signature(endpoint, current_interval) != worker_signature:
                return False
            recent_ids = [safe_string(item) for item in endpoint.get("recent_event_ids", []) if safe_string(item)]
            if dedupe_id and (dedupe_id in recent_ids or dedupe_id == safe_string(endpoint.get("last_event_id"))):
                return False
            if not dedupe_id and alert.fingerprint == safe_string(endpoint.get("last_fingerprint")):
                return False
            if dedupe_id:
                recent_ids.append(dedupe_id)
                endpoint["recent_event_ids"] = recent_ids[-RECENT_EVENT_ID_LIMIT:]
                endpoint["last_event_id"] = dedupe_id
            endpoint["last_fingerprint"] = alert.fingerprint
            first = self.config["endpoints"][0]
            self.config["endpoint"] = first.get("endpoint", "")
            self.config["token"] = first.get("token", "")
            self.config["last_event_id"] = first.get("last_event_id", "")
            self.config["last_fingerprint"] = first.get("last_fingerprint", "")
            save_config(self.config)
        self.ui_queue.put(("alert", alert))
        return True

    def monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            with self.config_lock:
                cfg = normalize_config(dict(self.config))
            enabled = bool(cfg.get("enabled", True))
            legacy_default_interval = normalize_poll_seconds(cfg.get("poll_seconds", DEFAULT_POLL_SECONDS))
            desired = {
                index: endpoint_cfg
                for index, endpoint_cfg in (
                    active_endpoints(cfg) if enabled else []
                )
            }
            with self.worker_lock:
                for index, worker in list(self.workers.items()):
                    candidate = desired.get(index)
                    candidate_interval = (
                        endpoint_poll_seconds(candidate, legacy_default_interval) if candidate is not None else None
                    )
                    if candidate is None or worker.signature != endpoint_worker_signature(candidate, candidate_interval):
                        worker.stop()
                        if not worker.thread.is_alive():
                            self.workers.pop(index, None)
                            self.transport_statuses.pop(index, None)
                for index, endpoint_cfg in desired.items():
                    if index not in self.workers:
                        worker = EndpointTransportWorker(
                            self,
                            index,
                            endpoint_cfg,
                            endpoint_poll_seconds(endpoint_cfg, legacy_default_interval),
                        )
                        self.workers[index] = worker
                        worker.start()
            if not desired:
                self.clear_fault()
                self.status_text = monitoring_idle_status(cfg)
                self.ui_queue.put(("status", self.status_text))
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
        with self.config_lock:
            cfg = normalize_config(dict(self.config))
        if not bool(cfg.get("auto_update_enabled", True)):
            return

        now = time.time()
        last_check = float(cfg.get("last_update_check_ts", 0) or 0)
        if now - last_check < UPDATE_CHECK_SECONDS:
            return

        with self.config_lock:
            self.config["last_update_check_ts"] = now
            self.config["last_update_error"] = ""
            self.config = normalize_config(self.config)
            save_config(self.config)

        try:
            latest_release = fetch_latest_github_release()
            release_id = safe_string(latest_release.get("id"))
            release_name = safe_string(latest_release.get("name")) or safe_string(latest_release.get("tag_name"))
            release_tag = safe_string(latest_release.get("tag_name"))
            version_comparison = compare_release_versions(release_tag, APP_VERSION)
            with self.config_lock:
                current_release_id = safe_string(self.config.get("last_update_release_id"))
                if not current_release_id:
                    current_release_id = safe_string(self.config.get("last_update_commit"))

            if version_comparison is not None and version_comparison <= 0:
                self.record_current_release(latest_release)
                log(f"auto update check OK: app {APP_VERSION} is current for {release_tag or release_id}")
                return
            if version_comparison != 1 and release_id == current_release_id:
                log(f"auto update check OK: already at tracked release {release_name or release_id}")
                return
            if version_comparison != 1 and not current_release_id:
                self.record_current_release(latest_release)
                log(f"auto update baseline set to GitHub release {release_name or release_id}")  # nosec B608
                return

            log(f"auto update found release {release_name or release_id}; downloading installer")
            installer_path = download_update_installer(latest_release)
            log(f"requesting elevation for update installer: {installer_path}")
            launch_update_installer(installer_path)
            with self.config_lock:
                self.config["pending_update_release_id"] = release_id
                self.config["last_update_error"] = ""
                self.config = normalize_config(self.config)
                save_config(self.config)
            log(f"update installer launched for {release_name or release_id}")
        except Exception as exc:
            message = safe_string(exc)
            with self.config_lock:
                self.config["last_update_error"] = message
                self.config = normalize_config(self.config)
                save_config(self.config)
            log(f"auto update check failed: {message}")

    def process_ui_queue(self) -> None:
        while True:
            try:
                kind, value = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "alert" and isinstance(value, AlertData):
                    self.present_alert(value)
                elif kind == "fault":
                    FaultToast(self, safe_string(value))
                elif kind == "settings":
                    self.show_settings()
                elif kind == "status":
                    if self.settings_window is not None and self.settings_window.window.winfo_exists():
                        self.settings_window.status_label.configure(text=safe_string(value))
                elif kind == "test_result" and isinstance(value, tuple) and len(value) == 2:
                    callback, message = value
                    callback(message)
            except Exception as exc:
                log(f"UI dispatch failed for {safe_string(kind)}: {safe_string(exc)}")
        if not self.stop_event.is_set():
            self.root.after(200, self.process_ui_queue)

    def present_alert(self, alert: AlertData) -> None:
        with self.config_lock:
            audio_name = safe_audio_name(safe_string(self.config.get("audio_sound")) or DEFAULT_AUDIO_NAME)
        log(f"presenting {alert.kind or 'notification'}{f' {alert.event_id}' if alert.event_id else ''}")
        play_alert_sound(audio_name)
        AlertWindow(self, alert)

    def show_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.window.winfo_exists():
            self.settings_window.window.lift()
            self.settings_window.window.focus_force()
            return
        self.settings_window = SettingsWindow(self)

    def transport_is_authenticated(self, index: int, endpoint_cfg: dict, poll_seconds: int) -> bool:
        """Use the live worker as the connection test when it is already healthy."""
        with self.worker_lock:
            worker = self.workers.get(index)
            status = self.transport_statuses.get(index)
            return bool(
                worker is not None
                and worker.thread.is_alive()
                and worker.signature == endpoint_worker_signature(endpoint_cfg, poll_seconds)
                and status is not None
                and status[0] == "AUTHENTICATED"
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
                legacy_default_interval = normalize_poll_seconds(cfg.get("poll_seconds", DEFAULT_POLL_SECONDS))
                if not endpoints:
                    message = "No active endpoints are configured."
                    return

                for index, endpoint_cfg in endpoints:
                    try:
                        mode = normalize_delivery_mode(
                            endpoint_cfg.get("delivery_mode"), existing_url=safe_string(endpoint_cfg.get("endpoint"))
                        )
                        if mode == DELIVERY_LIVE:
                            if self.transport_is_authenticated(
                                index,
                                endpoint_cfg,
                                endpoint_poll_seconds(endpoint_cfg, legacy_default_interval),
                            ):
                                successes += 1
                                continue
                            response = open_sse_response(
                                endpoint_cfg,
                                timeout=SSE_TEST_TIMEOUT_SECONDS,
                                read_timeout=SSE_TEST_TIMEOUT_SECONDS,
                            )
                            try:
                                handshake_ok = False
                                deadline = time.monotonic() + SSE_TEST_TIMEOUT_SECONDS

                                def enforce_test_deadline() -> None:
                                    if time.monotonic() > deadline:
                                        raise ApiError("Authenticated handshake test timed out.")

                                for event in iter_sse_events(response, on_activity=enforce_test_deadline):
                                    if event.name != "authenticated":
                                        raise StreamProtocolError(
                                            f"PBX sent '{event.name}' before the authenticated handshake."
                                        )
                                    payload = json.loads(event.data)
                                    handshake_ok = isinstance(payload, dict) and payload.get("ok") is True
                                    break
                                if not handshake_ok:
                                    raise StreamProtocolError("Authenticated handshake was not received.")
                            finally:
                                response.close()
                        else:
                            data, _raw_text = fetch_endpoint(
                                pbx_transport_url(endpoint_cfg, DELIVERY_POLL),
                                endpoint_auth_headers(endpoint_cfg),
                                allow_invalid_certificate=bool(endpoint_cfg.get("allow_invalid_certificate", False)),
                            )
                            ensure_api_ok(data)
                        successes += 1
                    except Exception as exc:
                        failures.append(f"{endpoint_display_name(index, endpoint_cfg)}: {exc}")

                if failures:
                    message = f"Test completed: {successes}/{len(endpoints)} OK. " + " | ".join(failures[:2])
                else:
                    message = (
                        f"Test succeeded: {successes}/{len(endpoints)} PBX profile"
                        f"{'s' if len(endpoints) != 1 else ''} authenticated."
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
        self.stop_event.set()
        self.wakeup_event.set()
        with self.worker_lock:
            for worker in self.workers.values():
                worker.stop()
            self.workers.clear()
        self.root.after(50, self.root.destroy)


def remove_config_dir() -> None:
    try:
        delete_all_profile_secrets()
        if CONFIG_DIR.exists():
            shutil.rmtree(CONFIG_DIR)
    except OSError as exc:
        log(f"uninstall config cleanup failed: {exc}")


def launch_uninstall_cleanup(remove_settings: bool) -> None:
    try:
        cleanup_dir = Path(tempfile.gettempdir()) / f"{APP_SHORT_NAME}_uninstall_{os.getpid()}"
        cleanup_dir.mkdir(parents=True, exist_ok=True)
        cleanup_script = cleanup_dir / "finish_uninstall.ps1"
        parameters_path = cleanup_dir / "parameters.json"
        parameters_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "install_dir": str(INSTALL_DIR.resolve()),
                    "install_parent": str(INSTALL_DIR.resolve().parent),
                    "remove_settings": bool(remove_settings),
                    "config_dir": str(CONFIG_DIR.resolve()),
                    "config_parent": str(CONFIG_DIR.resolve().parent),
                }
            ),
            encoding="utf-8",
        )
        cleanup_script.write_text(
            r'''param([Parameter(Mandatory=$true)][string]$ParametersPath)
$ErrorActionPreference = "SilentlyContinue"
$parameters = Get-Content -LiteralPath $ParametersPath -Raw | ConvertFrom-Json
for ($attempt = 0; $attempt -lt 240; $attempt++) {
    if (-not (Get-Process -Id ([int]$parameters.pid) -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 500
}
Remove-Item -LiteralPath ([string]$parameters.install_dir) -Recurse -Force
if ([bool]$parameters.remove_settings) {
    Remove-Item -LiteralPath ([string]$parameters.config_dir) -Recurse -Force
}
foreach ($parent in @([string]$parameters.install_parent, [string]$parameters.config_parent)) {
    if ((Test-Path -LiteralPath $parent) -and -not (Get-ChildItem -LiteralPath $parent -Force | Select-Object -First 1)) {
        Remove-Item -LiteralPath $parent -Force
    }
}
$cleanupRoot = Split-Path -Parent $ParametersPath
Remove-Item -LiteralPath $cleanupRoot -Recurse -Force
''',
            encoding="utf-8",
        )
        subprocess.Popen(
            [
                str(POWERSHELL_EXE),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(cleanup_script),
                "-ParametersPath",
                str(parameters_path),
            ],
            cwd=tempfile.gettempdir(),
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log(f"uninstall cleanup launch failed: {exc}")


def uninstall() -> None:
    set_startup_enabled(False)
    remove_start_menu_entries()
    remove_uninstall_entry()
    remove_settings = "--keep-settings" not in sys.argv
    if remove_settings:
        delete_all_profile_secrets()
    if getattr(sys, "frozen", False) and is_windows():
        launch_uninstall_cleanup(remove_settings)
    elif remove_settings:
        remove_config_dir()


def main() -> None:
    if "--uninstall" in sys.argv:
        uninstall()
        return

    install_self_if_needed()
    ensure_install_artifacts()

    background = "--background" in sys.argv
    if not acquire_single_instance():
        for _ in range(10):
            if notify_existing_instance(show_settings=not background):
                break
            time.sleep(0.15)
        return

    root = Tk()
    app = MassNotifyApp(root, show_settings_on_start=not background)
    root.protocol("WM_DELETE_WINDOW", app.shutdown)
    root.mainloop()


if __name__ == "__main__":
    main()
