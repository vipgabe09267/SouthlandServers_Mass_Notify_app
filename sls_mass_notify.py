from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, Canvas, IntVar, StringVar, Text, Tk, Toplevel, messagebox
from tkinter import ttk

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
APP_VERSION = "1.0.1"
IPC_HOST = "127.0.0.1"
IPC_PORT = 48572
DEFAULT_POLL_SECONDS = 15
MAX_ENDPOINTS = 3
FAULT_NOTIFY_SECONDS = 5 * 60
FAULT_TOAST_VISIBLE_MS = 18000
ALERT_AUTO_HIDE_MS = 45000
IMAGE_FETCH_LIMIT_BYTES = 5 * 1024 * 1024
ALERT_FOOTER_TEXT = "Copyright \u00a9 Southland Servers Group"
UPDATE_CHECK_SECONDS = 24 * 60 * 60
UPDATE_RETRY_WAKE_SECONDS = 60 * 60
UPDATE_DOWNLOAD_LIMIT_BYTES = 150 * 1024 * 1024
GITHUB_OWNER = "vipgabe09267"
GITHUB_REPO = "SouthlandServers_Mass_Notify_app"
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases?per_page=10"
UPDATE_INSTALLER_ASSET_NAMES = ("SLS_Mass_Notify_Installer.exe",)

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / COMPANY_NAME / APP_SHORT_NAME
CONFIG_PATH = CONFIG_DIR / "settings.json"
LOG_PATH = CONFIG_DIR / "app.log"
UPDATE_DIR = CONFIG_DIR / "updates"
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / COMPANY_NAME / APP_SHORT_NAME
INSTALL_EXE_PATH = INSTALL_DIR / EXE_NAME
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
    return base / name


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
    if not value.startswith(("dpapi:", "plain:")):
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
        "name": f"Endpoint {index + 1}",
        "endpoint": "",
        "enabled": index == 0,
        "no_token": False,
        "token": "",
        "last_event_id": "",
        "last_fingerprint": "",
    }


def normalize_endpoint(value: object, index: int) -> dict:
    endpoint = blank_endpoint(index)
    if isinstance(value, dict):
        endpoint.update(
            {
                "name": safe_string(value.get("name")) or endpoint["name"],
                "endpoint": safe_string(value.get("endpoint") or value.get("url")),
                "enabled": bool(value.get("enabled", endpoint["enabled"])),
                "no_token": bool(value.get("no_token", value.get("noToken", False))),
                "token": safe_string(value.get("token")),
                "last_event_id": safe_string(value.get("last_event_id", value.get("lastEventId", ""))),
                "last_fingerprint": safe_string(
                    value.get("last_fingerprint", value.get("lastFingerprint", ""))
                ),
            }
        )
    return endpoint


def normalize_endpoints(config: dict) -> list[dict]:
    endpoints: list[dict] = []
    configured = config.get("endpoints")
    if isinstance(configured, list):
        for index, endpoint in enumerate(configured[:MAX_ENDPOINTS]):
            endpoints.append(normalize_endpoint(endpoint, index))

    if not endpoints and (config.get("endpoint") or config.get("token")):
        endpoints.append(
            normalize_endpoint(
                {
                    "name": "Endpoint 1",
                    "endpoint": config.get("endpoint", ""),
                    "enabled": True,
                    "no_token": bool(config.get("no_token", False)),
                    "token": config.get("token", ""),
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
    normalized["no_token"] = bool(first.get("no_token", False))
    normalized["last_event_id"] = first.get("last_event_id", "")
    normalized["last_fingerprint"] = first.get("last_fingerprint", "")
    return normalized


def endpoint_has_credentials(endpoint: dict) -> bool:
    if endpoint.get("no_token"):
        return True
    return bool(unprotect_secret(endpoint.get("token", "")))


def endpoint_display_name(index: int, endpoint: dict) -> str:
    name = safe_string(endpoint.get("name")) or f"Endpoint {index + 1}"
    url = safe_string(endpoint.get("endpoint"))
    if url:
        parsed = urlparse(url)
        if parsed.netloc:
            return f"{name} ({parsed.netloc})"
    return name


def endpoint_url_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.netloc:
        return True
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    return False


def active_endpoints(config: dict) -> list[tuple[int, dict]]:
    result: list[tuple[int, dict]] = []
    for index, endpoint in enumerate(normalize_endpoints(config)):
        url = safe_string(endpoint.get("endpoint"))
        if endpoint.get("enabled", True) and url and endpoint_url_allowed(url) and endpoint_has_credentials(endpoint):
            result.append((index, endpoint))
    return result


def default_config() -> dict:
    return {
        "auto_update_enabled": True,
        "endpoint": "",
        "enabled": True,
        "endpoints": [blank_endpoint(index) for index in range(MAX_ENDPOINTS)],
        "last_update_check_ts": 0.0,
        "last_update_commit": "",
        "last_update_error": "",
        "last_update_release_id": "",
        "last_update_release_name": "",
        "last_update_release_tag": "",
        "no_token": False,
        "poll_seconds": DEFAULT_POLL_SECONDS,
        "startup_enabled": True,
        "token": "",
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
    except ET.ParseError as exc:
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
    kind = safe_string(lookup(source, KIND_KEYS))
    event = safe_string(lookup(source, EVENT_KEYS))
    title = safe_string(lookup(source, TITLE_KEYS)) or phone.title
    severity = safe_string(lookup(source, SEVERITY_KEYS))
    priority = safe_string(lookup(source, PRIORITY_KEYS))
    priority_label = safe_string(lookup(source, PRIORITY_LABEL_KEYS))
    image_url = safe_string(lookup(source, IMAGE_KEYS))
    recipients = safe_string(lookup(source, RECIPIENT_KEYS))
    timestamp = safe_string(lookup(source, TIMESTAMP_KEYS))
    area = safe_string(lookup(source, AREA_KEYS))
    effective = safe_string(lookup(source, EFFECTIVE_KEYS))
    expires = safe_string(lookup(source, EXPIRES_KEYS))
    body = safe_string(lookup_preferred(source, ("body", "description", "text", "message", "desc")))
    description = safe_string(lookup_preferred(source, ("description", "body", "text", "message", "desc")))
    event_id = safe_string(lookup(source, EVENT_ID_KEYS))
    recent_events = format_recent_events(lookup(data, RECENT_EVENT_KEYS))

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
        title = "SIP NOTIFY Alert"

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
    )


class ApiError(Exception):
    pass


class UnauthorizedError(ApiError):
    pass


def fetch_endpoint(endpoint: str, token: str) -> tuple[object, str]:
    headers = {
        "Accept": "application/json, application/xml, text/xml, text/plain, */*",
        "User-Agent": f"{APP_SHORT_NAME}/{APP_VERSION}",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(endpoint, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "")
            raw_bytes = response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise UnauthorizedError("Unauthorized request. Check endpoint and token.") from exc
        raise ApiError(f"HTTP {exc.code}: {exc.reason}") from exc
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


def fetch_image_bytes(image_url: str) -> bytes:
    request = urllib.request.Request(
        image_url,
        headers={
            "Accept": "image/png,image/gif,image/*,*/*",
            "User-Agent": f"{APP_SHORT_NAME}/{APP_VERSION}",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
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
    with urllib.request.urlopen(request, timeout=12) as response:
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
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = safe_string(asset.get("name"))
            if name in UPDATE_INSTALLER_ASSET_NAMES or name.lower().endswith("_installer.exe"):
                selected_asset = asset
                break
        if selected_asset is None:
            continue

        download_url = safe_string(selected_asset.get("browser_download_url"))
        parsed = urlparse(download_url)
        if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
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
            "download_url": download_url,
        }

    raise ApiError("No GitHub release with SLS_Mass_Notify_Installer.exe was found")


def download_update_installer(release: dict) -> Path:
    release_id = safe_string(release.get("id"))
    download_url = safe_string(release.get("download_url"))
    asset_name = safe_string(release.get("asset_name")) or "SLS_Mass_Notify_Installer.exe"
    parsed = urlparse(download_url)
    if not release_id or parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
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
    with urllib.request.urlopen(request, timeout=45) as response:
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
    temp_path.replace(installer_path)
    return installer_path


def launch_update_installer(installer_path: Path) -> None:
    if not installer_path.exists():
        raise ApiError(f"Update installer is missing: {installer_path}")
    subprocess.Popen(
        [str(installer_path), "--silent", "--update"],
        cwd=str(installer_path.parent),
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def play_alert_sound() -> None:
    if winsound is None:
        return
    for name in ("eas_tone.wav", "eas_totne.wav"):
        sound_path = resource_path(name)
        if sound_path.exists():
            try:
                winsound.PlaySound(str(sound_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            except RuntimeError as exc:
                log(f"sound failed: {exc}")
            return


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
        self.window = Toplevel(app.root)
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
        self.window.lift()
        self.window.focus_force()
        self.window.after(750, self._drop_topmost)
        self.window.after(ALERT_AUTO_HIDE_MS, self.hide)
        self._load_screen_image()

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
            font=("Segoe UI", 34),
        )
        canvas.create_text(
            sx + sw / 2,
            sy + 178,
            text=wrap_for_canvas(priority, 30, 2),
            fill=palette["text"],
            justify="center",
            font=("Segoe UI", 30, "bold"),
        )
        canvas.create_text(
            sx + sw / 2,
            sy + 286,
            text=wrap_for_canvas(area, 44, 2),
            fill=palette["text"],
            justify="center",
            width=690,
            font=("Segoe UI", 24, "bold"),
        )
        if timing:
            canvas.create_text(
                sx + sw / 2,
                sy + 392,
                text=wrap_for_canvas(timing, 68, 2),
                fill=palette["muted"],
                justify="center",
                width=700,
                font=("Segoe UI", 17, "bold"),
            )

    def _announcement_text_parts(self) -> tuple[str, str, str]:
        title = self.alert.title or self.alert.event or "Announcement"
        body = self.alert.body or self.alert.description or self.alert.event or "Announcement"
        created = display_alert_time(self.alert.timestamp)
        return title, body, created

    def _draw_announcement_screen(self, canvas) -> None:
        sx, sy, sw, sh = self.SCREEN_X, self.SCREEN_Y, self.SCREEN_W, self.SCREEN_H
        title, body, _created = self._announcement_text_parts()
        canvas.create_rectangle(sx, sy, sx + sw, sy + sh, fill="#f6f8fb", outline="#c6d1df")
        canvas.create_rectangle(sx + 2, sy + 2, sx + sw - 2, sy + sh - 2, fill="#ffffff", outline="#d7e0eb")

        triangle = [
            sx + sw / 2,
            sy + 56,
            sx + sw / 2 - 44,
            sy + 132,
            sx + sw / 2 + 44,
            sy + 132,
        ]
        canvas.create_polygon(triangle, fill="#f5b82e", outline="#98690a", width=3)
        canvas.create_text(
            sx + sw / 2,
            sy + 104,
            text="!",
            fill="#332300",
            justify="center",
            font=("Segoe UI", 42, "bold"),
        )

        canvas.create_text(
            sx + sw / 2,
            sy + 182,
            text=wrap_for_canvas(title or "Announcement", 30, 2),
            fill="#172033",
            justify="center",
            width=700,
            font=("Segoe UI", 32, "bold"),
        )
        canvas.create_text(
            sx + sw / 2,
            sy + 292,
            text=wrap_for_canvas(body, 58, 5),
            fill="#263447",
            justify="center",
            width=660,
            font=("Segoe UI", 22),
        )

    def _load_screen_image(self) -> None:
        if self._is_announcement():
            return
        image_url = resolve_image_url(self.alert.image_url, self.alert.source_endpoint)
        if not image_url:
            return

        def worker() -> None:
            try:
                image_bytes = fetch_image_bytes(image_url)
            except Exception as exc:
                log(f"alert image fetch failed: {exc}")
                return
            self.app.root.after(0, lambda: self._show_screen_image(image_bytes))

        threading.Thread(target=worker, name="AlertImageLoader", daemon=True).start()

    def _show_screen_image(self, image_bytes: bytes) -> None:
        import tkinter as tk

        if not self.window.winfo_exists():
            return
        try:
            if Image is not None and ImageTk is not None:
                image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
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
        width = self.window.winfo_width()
        height = self.window.winfo_height()
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        x = max(12, int((screen_w - width) / 2))
        y = max(12, int((screen_h - height) / 2))
        self.window.geometry(f"+{x}+{y}")

    def _drop_topmost(self) -> None:
        if self.window.winfo_exists():
            try:
                self.window.attributes("-topmost", False)
            except Exception:
                pass

    def _handle_focus_out(self, _event) -> None:
        self.window.after(140, self._lower_if_unfocused)

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
        text = Text(frame, wrap="word", font=("Consolas", 10))
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
        self.window.title("SLS Mass Notify Settings")
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
        self.interval_var = IntVar(value=int(cfg.get("poll_seconds", DEFAULT_POLL_SECONDS)))
        self.endpoint_forms: list[dict] = []

        self._build(cfg)
        self.window.update_idletasks()
        self._center()
        self.window.lift()
        self.window.focus_force()

    def _set_initial_geometry(self) -> None:
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        width = max(780, min(960, screen_w - 80))
        height = max(560, min(680, screen_h - 140))
        self.window.geometry(f"{width}x{height}")
        self.window.minsize(760, 560)

    def _configure_style(self) -> None:
        style = ttk.Style(self.window)
        for theme in ("clam", "vista", "xpnative"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except Exception:
                    pass
        self.window.configure(bg="#eef3f8")
        style.configure(".", background="#eef3f8", foreground="#102033", font=("Segoe UI", 9))
        style.configure("Surface.TFrame", background="#eef3f8")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("Header.TFrame", background="#0b5cad")
        style.configure("Header.TLabel", background="#0b5cad", foreground="#ffffff", font=("Segoe UI", 17, "bold"))
        style.configure("HeaderHint.TLabel", background="#0b5cad", foreground="#d9ebff", font=("Segoe UI", 9))
        style.configure("Section.TLabel", background="#ffffff", foreground="#102033", font=("Segoe UI", 11, "bold"))
        style.configure("Hint.TLabel", background="#ffffff", foreground="#607084", font=("Segoe UI", 9))
        style.configure("Status.TLabel", background="#eef3f8", foreground="#445166", font=("Segoe UI", 9))
        style.configure("TCheckbutton", background="#ffffff", foreground="#102033")
        style.configure("TEntry", fieldbackground="#ffffff")
        style.configure("Accent.TButton", background="#0b72df", foreground="#ffffff", font=("Segoe UI", 9, "bold"), padding=(12, 6))
        style.map("Accent.TButton", background=[("active", "#075eb8"), ("pressed", "#054f9f")])

    def _build(self, cfg: dict) -> None:
        # Configure main window grid layout
        self.window.rowconfigure(0, weight=1)  # Scrollable content expands
        self.window.columnconfigure(0, weight=1)
        
        # Create outer container for canvas and scrollbar
        scroll_frame = ttk.Frame(self.window, style="Surface.TFrame")
        scroll_frame.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)

        # Create canvas and scrollbar
        canvas = Canvas(scroll_frame, bg="#eef3f8", highlightthickness=0)
        scrollbar = ttk.Scrollbar(scroll_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas, padding=18, style="Surface.TFrame")
        
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")

        # Pack canvas and scrollbar
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            units = int(-1 * (event.delta / 120)) if event.delta else 0
            if units == 0:
                units = -1 if event.delta > 0 else 1
            canvas.yview_scroll(units, "units")
            return "break"

        def _on_scroll_up(_event):
            canvas.yview_scroll(-1, "units")
            return "break"

        def _on_scroll_down(_event):
            canvas.yview_scroll(1, "units")
            return "break"

        def _bind_mousewheel(widget):
            widget.bind("<MouseWheel>", _on_mousewheel, add="+")
            widget.bind("<Button-4>", _on_scroll_up, add="+")
            widget.bind("<Button-5>", _on_scroll_down, add="+")
            for child in widget.winfo_children():
                _bind_mousewheel(child)

        # Update canvas scrollregion after widgets are created
        def _update_scroll_region(event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            # Make inner frame width match canvas width
            if canvas.winfo_width() > 1:
                canvas.itemconfig(canvas_window, width=canvas.winfo_width())

        scrollable_frame.bind("<Configure>", _update_scroll_region)
        canvas.bind("<Configure>", lambda e: _update_scroll_region() if e.width > 1 else None)

        # Build header
        header = ttk.Frame(scrollable_frame, padding=18, style="Header.TFrame")
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="SLS Mass Notify", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Background alert monitoring, startup behavior, automatic updates, and up to three SIP notify endpoints.",
            style="HeaderHint.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        general = ttk.Frame(scrollable_frame, padding=16, style="Card.TFrame")
        general.pack(fill="x", pady=(0, 14))
        ttk.Label(general, text="General", style="Section.TLabel").grid(row=0, column=0, columnspan=5, sticky="w")
        ttk.Label(
            general,
            text="Choose how the background monitor runs on this Windows user profile.",
            style="Hint.TLabel",
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(2, 12))
        ttk.Checkbutton(general, text="Enable background monitoring", variable=self.enabled_var).grid(
            row=2, column=0, sticky="w", padx=(0, 18), pady=(0, 8)
        )
        ttk.Checkbutton(general, text="Run at Windows startup", variable=self.startup_var).grid(
            row=2, column=1, sticky="w", padx=(0, 18), pady=(0, 8)
        )
        ttk.Checkbutton(
            general,
            text="Automatically check GitHub for updates once daily",
            variable=self.auto_update_var,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(
            general,
            text="Updates download the installer from the latest repository commit and may request Windows admin approval.",
            style="Hint.TLabel",
        ).grid(row=4, column=0, columnspan=5, sticky="w", pady=(0, 10))
        ttk.Label(general, text="Poll every", background="#ffffff").grid(row=5, column=0, sticky="w", padx=(0, 6))
        ttk.Spinbox(general, from_=5, to=3600, textvariable=self.interval_var, width=7).grid(
            row=5, column=1, sticky="w"
        )
        ttk.Label(general, text="seconds", background="#ffffff").grid(row=5, column=2, sticky="w", padx=(6, 0))
        general.columnconfigure(5, weight=1)

        endpoints_frame = ttk.Frame(scrollable_frame, padding=16, style="Card.TFrame")
        endpoints_frame.pack(fill="both", expand=True, pady=(0, 12))
        ttk.Label(endpoints_frame, text="Alert Endpoints", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            endpoints_frame,
            text="Each active endpoint may use its own bearer token, or No token mode for trusted direct endpoints.",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(2, 12))
        for index, endpoint in enumerate(normalize_endpoints(cfg)):
            group = ttk.Frame(endpoints_frame, padding=12, style="Card.TFrame")
            group.pack(fill="x", pady=(0, 12 if index < MAX_ENDPOINTS - 1 else 0))
            ttk.Label(group, text=f"Endpoint {index + 1}", style="Section.TLabel").grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
            )
            self._build_endpoint_tab(group, index, endpoint)

        # Footer stays at bottom with separator
        ttk.Separator(self.window).grid(row=1, column=0, sticky="ew", pady=(2, 0))
        footer = ttk.Frame(self.window, padding=(2, 0), style="Surface.TFrame")
        footer.grid(row=2, column=0, sticky="ew", padx=12, pady=8)
        footer.columnconfigure(0, weight=1)  # Status expands
        
        # Status label on left side
        self.status_label = ttk.Label(
            footer,
            text=self.app.status_text,
            style="Status.TLabel",
            wraplength=520,
        )
        self.status_label.grid(row=0, column=0, sticky="ew", padx=(0, 12))

        actions = ttk.Frame(footer)
        actions.grid(row=0, column=1, sticky="e")
        ttk.Button(actions, text="Test Active", command=self.test_now).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Save", style="Accent.TButton", command=self.save).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Close", command=self.close).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Quit App", command=self.quit_app).pack(side="left")

        _bind_mousewheel(self.window)

    def _build_endpoint_tab(self, parent: ttk.Frame, index: int, endpoint: dict) -> None:
        form = {
            "name": StringVar(value=safe_string(endpoint.get("name")) or f"Endpoint {index + 1}"),
            "endpoint": StringVar(value=safe_string(endpoint.get("endpoint"))),
            "token": StringVar(value=unprotect_secret(safe_string(endpoint.get("token")))),
            "enabled": BooleanVar(value=bool(endpoint.get("enabled", index == 0))),
            "no_token": BooleanVar(value=bool(endpoint.get("no_token", False))),
            "show_token": BooleanVar(value=False),
            "token_entry": None,
        }
        self.endpoint_forms.append(form)

        row_offset = 1
        ttk.Checkbutton(parent, text="Enabled", variable=form["enabled"]).grid(row=row_offset, column=0, sticky="w")
        ttk.Checkbutton(
            parent,
            text="No token",
            variable=form["no_token"],
            command=lambda idx=index: self.toggle_no_token(idx),
        ).grid(row=row_offset, column=1, sticky="w", padx=(18, 0))
        ttk.Checkbutton(
            parent,
            text="Show token",
            variable=form["show_token"],
            command=lambda idx=index: self.toggle_token(idx),
        ).grid(row=row_offset, column=2, sticky="w", padx=(18, 0))

        ttk.Label(parent, text="Name", background="#ffffff").grid(row=row_offset + 1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(parent, textvariable=form["name"], width=24).grid(
            row=row_offset + 2, column=0, sticky="ew", pady=(3, 0), padx=(0, 10)
        )

        ttk.Label(parent, text="Endpoint URL", background="#ffffff").grid(
            row=row_offset + 1, column=1, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Entry(parent, textvariable=form["endpoint"], width=64).grid(
            row=row_offset + 2, column=1, columnspan=2, sticky="ew", pady=(3, 0), padx=(0, 10)
        )

        ttk.Label(parent, text="Authorization token / key", background="#ffffff").grid(
            row=row_offset + 3, column=0, sticky="w", pady=(10, 0)
        )
        token_entry = ttk.Entry(parent, textvariable=form["token"], width=80, show="*")
        token_entry.grid(row=row_offset + 4, column=0, columnspan=3, sticky="ew", pady=(3, 0), padx=(0, 10))
        form["token_entry"] = token_entry
        ttk.Label(
            parent,
            text="No token calls this endpoint directly without an Authorization header.",
            style="Hint.TLabel",
        ).grid(row=row_offset + 5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=2)
        parent.columnconfigure(2, weight=2)
        self.toggle_no_token(index)

    def _center(self) -> None:
        width = self.window.winfo_width()
        height = self.window.winfo_height()
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        x = max(0, int((screen_w - width) / 2))
        y = max(0, int((screen_h - height) / 3))
        self.window.geometry(f"+{x}+{y}")

    def toggle_token(self, index: int) -> None:
        form = self.endpoint_forms[index]
        entry = form["token_entry"]
        if entry is not None:
            entry.configure(show="" if form["show_token"].get() else "*")

    def toggle_no_token(self, index: int) -> None:
        form = self.endpoint_forms[index]
        entry = form["token_entry"]
        if entry is not None:
            entry.configure(state="disabled" if form["no_token"].get() else "normal")

    def collect_settings(self) -> tuple[list[dict], int] | None:
        try:
            interval = int(self.interval_var.get())
        except Exception:
            interval = DEFAULT_POLL_SECONDS
        interval = max(5, interval)

        endpoints: list[dict] = []
        active_count = 0
        for index, form in enumerate(self.endpoint_forms):
            name = form["name"].get().strip() or f"Endpoint {index + 1}"
            url = form["endpoint"].get().strip()
            token = form["token"].get().strip()
            no_token = bool(form["no_token"].get())
            endpoint_enabled = bool(form["enabled"].get())

            if url and not endpoint_url_allowed(url):
                messagebox.showerror(
                    "Endpoint URL",
                    f"Endpoint {index + 1} must use https://. http:// is only allowed for localhost testing.",
                )
                return None
            if endpoint_enabled and url:
                if not no_token and not token:
                    messagebox.showerror(
                        "Token required",
                        f"Endpoint {index + 1} needs a token, or check No token.",
                    )
                    return None
                active_count += 1

            endpoints.append(
                {
                    "name": name,
                    "endpoint": url,
                    "enabled": endpoint_enabled,
                    "no_token": no_token,
                    "token": "" if no_token else protect_secret(token),
                    "last_event_id": self.app.get_endpoint_state(index, "last_event_id"),
                    "last_fingerprint": self.app.get_endpoint_state(index, "last_fingerprint"),
                }
            )

        if self.enabled_var.get() and active_count == 0:
            messagebox.showerror("No active endpoint", "Enable and configure at least one endpoint, or disable monitoring.")
            return None
        return endpoints, interval

    def save(self) -> bool:
        collected = self.collect_settings()
        if collected is None:
            return False
        endpoints, interval = collected
        self.app.update_settings(
            endpoints=endpoints,
            enabled=self.enabled_var.get(),
            startup_enabled=self.startup_var.get(),
            auto_update_enabled=self.auto_update_var.get(),
            poll_seconds=interval,
        )
        self.status_label.configure(text="Saved. Monitoring will use the active endpoint tabs.")
        return True

    def test_now(self) -> None:
        if not self.save():
            return
        self.status_label.configure(text="Testing active endpoints...")
        self.app.test_now(lambda message: self.status_label.configure(text=message))

    def close(self) -> None:
        self.window.destroy()
        self.app.settings_window = None

    def quit_app(self) -> None:
        self.app.shutdown()


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
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.settings_window: SettingsWindow | None = None
        self.status_text = "Waiting for first endpoint check."
        self.faults: dict[str, dict] = {}

        self.command_thread = threading.Thread(target=self.command_server, name="CommandServer", daemon=True)
        self.command_thread.start()

        self.poll_thread = threading.Thread(target=self.poll_loop, name="EndpointPoller", daemon=True)
        self.poll_thread.start()

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
        poll_seconds: int,
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
            self.config["poll_seconds"] = int(poll_seconds)
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
        if key is None:
            self.faults.clear()
        else:
            self.faults.pop(key, None)

    def poll_loop(self) -> None:
        while not self.stop_event.is_set():
            with self.config_lock:
                cfg = normalize_config(dict(self.config))
            enabled = bool(cfg.get("enabled", True))
            interval = max(5, int(cfg.get("poll_seconds", DEFAULT_POLL_SECONDS) or DEFAULT_POLL_SECONDS))

            endpoints = active_endpoints(cfg) if enabled else []
            if endpoints:
                ok_count = 0
                for index, endpoint_cfg in endpoints:
                    endpoint = safe_string(endpoint_cfg.get("endpoint"))
                    token = "" if endpoint_cfg.get("no_token") else unprotect_secret(endpoint_cfg.get("token", ""))
                    fault_key = f"endpoint-{index}"
                    try:
                        data, raw_text = fetch_endpoint(endpoint, token)
                        ensure_api_ok(data)
                        alert = extract_alert(data, raw_text)
                        normalize_alert_urls(alert, endpoint)
                        self.clear_fault(fault_key)
                        ok_count += 1
                        dedupe_key = alert.event_id or alert.fingerprint
                        last_seen = (
                            endpoint_cfg.get("last_event_id")
                            if alert.event_id
                            else endpoint_cfg.get("last_fingerprint")
                        )
                        if dedupe_key and dedupe_key != last_seen:
                            with self.config_lock:
                                self.config = normalize_config(self.config)
                                self.config["endpoints"][index]["last_event_id"] = (
                                    alert.event_id or self.config["endpoints"][index].get("last_event_id", "")
                                )
                                self.config["endpoints"][index]["last_fingerprint"] = alert.fingerprint
                                first = self.config["endpoints"][0]
                                self.config["endpoint"] = first.get("endpoint", "")
                                self.config["token"] = first.get("token", "")
                                self.config["last_event_id"] = first.get("last_event_id", "")
                                self.config["last_fingerprint"] = first.get("last_fingerprint", "")
                                save_config(self.config)
                            self.ui_queue.put(("alert", alert))
                    except UnauthorizedError as exc:
                        message = f"{endpoint_display_name(index, endpoint_cfg)} authorization fault: {exc}"
                        self.status_text = message
                        self.record_fault(fault_key, message)
                        log(message)
                    except ApiError as exc:
                        message = f"{endpoint_display_name(index, endpoint_cfg)} endpoint check failed: {exc}"
                        self.status_text = message
                        self.record_fault(fault_key, message)
                        log(message)
                    except Exception as exc:
                        message = f"{endpoint_display_name(index, endpoint_cfg)} unexpected poll error: {exc}"
                        self.status_text = message
                        self.record_fault(fault_key, message)
                        log(message)
                if ok_count:
                    total = len(endpoints)
                    self.status_text = (
                        f"Last checked {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}: "
                        f"{ok_count}/{total} endpoint{'s' if total != 1 else ''} OK"
                    )
            else:
                self.clear_fault()
                self.status_text = "Disabled or waiting for endpoint settings."

            self.wakeup_event.wait(interval)
            self.wakeup_event.clear()

    def update_loop(self) -> None:
        if self.stop_event.wait(45):
            return
        while not self.stop_event.is_set():
            try:
                self.check_for_updates_if_due()
            except Exception as exc:
                log(f"auto update loop failed: {exc}")
            if self.stop_event.wait(UPDATE_RETRY_WAKE_SECONDS):
                break

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
            with self.config_lock:
                current_release_id = safe_string(self.config.get("last_update_release_id"))
                if not current_release_id:
                    current_release_id = safe_string(self.config.get("last_update_commit"))
            if not current_release_id:
                with self.config_lock:
                    self.config["last_update_release_id"] = release_id
                    self.config["last_update_release_name"] = release_name
                    self.config["last_update_release_tag"] = safe_string(latest_release.get("tag_name"))
                    self.config["last_update_error"] = ""
                    self.config = normalize_config(self.config)
                    save_config(self.config)
                log(f"auto update baseline set to GitHub release {release_name or release_id}")
                return
            if release_id == current_release_id:
                log(f"auto update check OK: already at tracked release {release_name or release_id}")
                return

            log(f"auto update found release {release_name or release_id}; downloading installer")
            installer_path = download_update_installer(latest_release)
            with self.config_lock:
                self.config["last_update_release_id"] = release_id
                self.config["last_update_release_name"] = release_name
                self.config["last_update_release_tag"] = safe_string(latest_release.get("tag_name"))
                self.config["last_update_error"] = ""
                self.config = normalize_config(self.config)
                save_config(self.config)
            log(f"launching update installer: {installer_path}")
            launch_update_installer(installer_path)
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
            if kind == "alert" and isinstance(value, AlertData):
                self.present_alert(value)
            elif kind == "fault":
                FaultToast(self, safe_string(value))
            elif kind == "settings":
                self.show_settings()
        if not self.stop_event.is_set():
            self.root.after(200, self.process_ui_queue)

    def present_alert(self, alert: AlertData) -> None:
        play_alert_sound()
        AlertWindow(self, alert)

    def show_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.window.winfo_exists():
            self.settings_window.window.lift()
            self.settings_window.window.focus_force()
            return
        self.settings_window = SettingsWindow(self)

    def test_now(self, callback) -> None:
        def worker() -> None:
            with self.config_lock:
                cfg = normalize_config(dict(self.config))
            endpoints = active_endpoints(cfg)
            if not endpoints:
                message = "No active endpoints are configured."
                self.status_text = message
                self.root.after(0, lambda: callback(message))
                return

            failures: list[str] = []
            successes = 0
            first_alert: AlertData | None = None
            for index, endpoint_cfg in endpoints:
                endpoint = safe_string(endpoint_cfg.get("endpoint"))
                token = "" if endpoint_cfg.get("no_token") else unprotect_secret(endpoint_cfg.get("token", ""))
                try:
                    data, raw_text = fetch_endpoint(endpoint, token)
                    ensure_api_ok(data)
                    alert = extract_alert(data, raw_text)
                    normalize_alert_urls(alert, endpoint)
                    successes += 1
                    if first_alert is None:
                        first_alert = alert
                except Exception as exc:
                    failures.append(f"{endpoint_display_name(index, endpoint_cfg)}: {exc}")

            if first_alert is not None:
                self.ui_queue.put(("alert", first_alert))
            if failures:
                message = f"Test completed: {successes}/{len(endpoints)} OK. " + " | ".join(failures[:2])
            else:
                message = f"Test succeeded: {successes}/{len(endpoints)} endpoint{'s' if len(endpoints) != 1 else ''} OK."
            self.status_text = message
            if failures:
                log(message)
            self.root.after(0, lambda: callback(message))

        threading.Thread(target=worker, name="EndpointTest", daemon=True).start()

    def shutdown(self) -> None:
        self.stop_event.set()
        self.wakeup_event.set()
        self.root.after(50, self.root.destroy)


def remove_config_dir() -> None:
    try:
        if CONFIG_DIR.exists():
            shutil.rmtree(CONFIG_DIR)
    except OSError as exc:
        log(f"uninstall config cleanup failed: {exc}")


def launch_uninstall_cleanup(remove_settings: bool) -> None:
    try:
        cleanup_dir = Path(tempfile.gettempdir()) / f"{APP_SHORT_NAME}_uninstall_{os.getpid()}"
        cleanup_dir.mkdir(parents=True, exist_ok=True)
        cleanup_script = cleanup_dir / "finish_uninstall.cmd"
        lines = [
            "@echo off",
            "setlocal",
            f"set APP_PID={os.getpid()}",
            ":wait_for_app",
            'tasklist /FI "PID eq %APP_PID%" 2>nul | findstr /R /C:"%APP_PID%" >nul',
            "if not errorlevel 1 (",
            "  timeout /t 1 /nobreak >nul",
            "  goto wait_for_app",
            ")",
            f'taskkill /IM "{EXE_NAME}" /F >nul 2>nul',
            "timeout /t 1 /nobreak >nul",
            f'del /F /Q "{START_MENU_APP_SHORTCUT}" >nul 2>nul',
            f'del /F /Q "{START_MENU_UNINSTALL_SHORTCUT}" >nul 2>nul',
            f'rmdir "{START_MENU_DIR}" >nul 2>nul',
            f'rmdir /S /Q "{INSTALL_DIR}" >nul 2>nul',
        ]
        if remove_settings:
            lines.append(f'rmdir /S /Q "{CONFIG_DIR}" >nul 2>nul')
        lines.extend(
            [
                f'rmdir "{CONFIG_DIR.parent}" >nul 2>nul',
                f'rmdir "{INSTALL_DIR.parent}" >nul 2>nul',
                f'del /F /Q "{cleanup_script}" >nul 2>nul',
                "endlocal",
            ]
        )
        cleanup_script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        subprocess.Popen(
            ["cmd.exe", "/c", str(cleanup_script)],
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
