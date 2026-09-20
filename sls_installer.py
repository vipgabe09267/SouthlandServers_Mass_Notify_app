from __future__ import annotations

import ctypes
import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from tkinter import BooleanVar, StringVar, Text, Tk, filedialog, messagebox
from tkinter import ttk
from typing import Callable

from sls_install_safety import (
    FileTransaction, MANIFEST_NAME, TRANSACTION_NAME, PRODUCT_ID, digest, manifest_for,
    prune_empty, read_manifest, reject_reparse, remove_exact_files,
    remove_staging, write_json,
)
from sls_windowing import fit_window, ScrollableBody
from sls_version import VERSION as APP_VERSION
from sls_install_windows import (
    close_exact_processes, delete_after_reboot, program_files_directory,
    system_directory, verify_protected_directory, protect_new_directory,
    executable_identity, shortcut_identity,
)

try:
    import winreg
except ImportError:  # pragma: no cover - Windows installer.
    winreg = None


APP_DISPLAY_NAME = "SouthlandServers Mass Notification App"
APP_SHORT_NAME = "SLS_Mass_Notify"
EXE_NAME = "SLS_Mass_Notify.exe"
INSTALLER_EXE_NAME = "SLS_Mass_Notify_Uninstall.exe"
MAINTENANCE_DIR = ".maintenance"
DEFAULTS_NAME = "installation-defaults.json"
COMPANY_DISPLAY_NAME = "Southland Servers Group"
CREDENTIAL_TARGET_PREFIX = "SouthlandServers/SLS_Mass_Notify"
AUDIO_DIR_NAME = "audio"
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_REG_PATH = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_SHORT_NAME}"

TERMS_TEXT = """SouthlandServers Mass Notification App Terms of Service

By installing or using this app, you acknowledge that it is a desktop notification client that receives alert and announcement content from user-configured PBX connections using authenticated live streams.

You are responsible for configuring endpoints, recipient systems, and server-side alert data accurately. The app does not create weather alerts, verify emergency content, or replace official emergency alerting systems.

PBX connections require HTTPS with valid certificates. Desktop credentials should be kept private.

The app stores local settings under the current Windows user profile and stores saved passwords in Windows Credential Manager, with DPAPI as a compatibility fallback. Optional update downloads from GitHub Releases are available for administrator review. Installing an update requires administrator-controlled verification and deployment.

This software is provided under the GNU Affero General Public License v3.0 without warranty. You agree to test deployments before operational use and to comply with all applicable laws, policies, and emergency communication requirements."""

PROGRAM_FILES = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
DEFAULT_INSTALL_DIR = PROGRAM_FILES / COMPANY_DISPLAY_NAME / "SLS Mass Notify"
START_MENU_DIR = (
    Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs"
    / COMPANY_DISPLAY_NAME
)
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "SouthlandServers" / APP_SHORT_NAME
CONFIG_PATH = CONFIG_DIR / "settings.json"
LEGACY_INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SouthlandServers" / APP_SHORT_NAME
LEGACY_START_MENU_DIR = (
    Path(os.environ.get("APPDATA", Path.home()))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs"
    / "SouthlandServers"
)
STARTUP_DIR = START_MENU_DIR.parent / "Startup"

ProgressCallback = Callable[[str], None]


class ElevationRequired(PermissionError):
    """A distinct exit status for an unelevated invocation or declined UAC."""


def emit(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    bundled = base / name
    if bundled.exists():
        return bundled
    return Path(__file__).resolve().parent / "dist" / name


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    if is_admin():
        return True
    # A user-writable onedir runtime must not be automatically elevated: DLLs
    # load before Python could verify their integrity. Deploy from a protected
    # administrator-controlled directory or launch from an elevated console.
    if getattr(sys, "frozen", False):
        package = Path(sys.executable).absolute().parent
        reject_reparse(package)
        for item in (package, *package.rglob("*")):
            reject_reparse(item)
            verify_protected_directory(item)
    arguments = sys.argv[1:] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve()), *sys.argv[1:]]
    params = subprocess.list2cmdline(arguments)
    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                              ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    shell_execute.restype = ctypes.c_void_p
    result = shell_execute(
        None,
        "runas",
        sys.executable,
        params,
        None,
        1,
    )
    return bool(result and result > 32)


def validate_install_dir(path: Path) -> Path:
    """Allow only protected dedicated machine-install directories with ownership."""
    reject_reparse(path.absolute())
    resolved = path.resolve()
    protected_root = program_files_directory().resolve() if os.name == "nt" else PROGRAM_FILES.resolve()
    if resolved == protected_root or not resolved.is_relative_to(protected_root):
        raise ValueError("Choose a dedicated application folder inside Windows Program Files.")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("The install location must be a folder.")
    if os.name == "nt":
        for ancestor in (resolved, *resolved.parents):
            if ancestor.exists():
                verify_protected_directory(ancestor)
            if ancestor == protected_root:
                break
    if resolved.exists():
        if (resolved / TRANSACTION_NAME).exists():
            raise RuntimeError("An interrupted upgrade needs administrator recovery; its journal and backup were retained.")
        entries = list(resolved.iterdir())
        if entries:
            installation_manifest(resolved)
    return resolved


def installation_manifest(root: Path) -> dict:
    """Recognize only the registered, protected 1.0.8 installation for migration.

    A filename alone never establishes ownership. Unrelated files and legacy
    audio remain unowned; the transaction backs up only positively identified EXEs.
    This function is read-only, including when the installer window opens.
    """
    if (root / MANIFEST_NAME).exists():
        return read_manifest(root)
    if not root.exists() or not any(root.iterdir()):
        return {}
    registration = snapshot_uninstall_registry() or {}
    values = {name: item[0] for name, item in registration.items()}
    expected = {"DisplayName": APP_DISPLAY_NAME, "Publisher": COMPANY_DISPLAY_NAME,
                "UninstallString": f'"{root / INSTALLER_EXE_NAME}" --uninstall'}
    if (any(values.get(name) != value for name, value in expected.items())
            or str(values.get("DisplayVersion", "")).lower() != "1.0.8-beta"
            or os.path.normcase(str(root.resolve())) != os.path.normcase(str(Path(values.get("InstallLocation", "")).resolve()))):
        raise ValueError("This nonempty folder is not a recognized SLS installation. Choose an empty Program Files folder.")
    reject_reparse(root)
    verify_protected_directory(root)
    files = {}
    for name, original in ((EXE_NAME, EXE_NAME), (INSTALLER_EXE_NAME, "SLS_Mass_Notify_Installer.exe")):
        path = root / name
        reject_reparse(path)
        verify_protected_directory(path)
        identity = executable_identity(path)
        if (identity.get("ProductName") != APP_DISPLAY_NAME or identity.get("CompanyName") != COMPANY_DISPLAY_NAME
                or identity.get("FileVersion", "").lower() != "1.0.8-beta" or identity.get("OriginalFilename") != original):
            raise ValueError(f"The legacy executable does not match the registered SLS product: {path}")
        files[name] = digest(path)
    shortcuts = {}
    for key, path in machine_shortcuts().items():
        if not path.exists():
            continue
        reject_reparse(path)
        verify_protected_directory(path, allow_shell_delete=True)
        link = shortcut_identity(path)
        target = root / (INSTALLER_EXE_NAME if key == "uninstall" else EXE_NAME)
        allowed = {"--uninstall"} if key == "uninstall" else ({"--background"} if key == "startup" else {"", "--settings"})
        if (os.path.normcase(str(Path(link.get("target", "")).resolve())) != os.path.normcase(str(target.resolve()))
                or link.get("arguments", "").strip() not in allowed):
            raise ValueError(f"The existing shortcut belongs to another target and will not be replaced: {path}")
        shortcuts[key] = digest(path)
    return {"schema": 1, "product": PRODUCT_ID, "version": values["DisplayVersion"],
            "files": files, "shortcuts": shortcuts}


def run_hidden(command: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    if not command or not Path(command[0]).is_absolute():
        raise ValueError("Installer subprocesses require a trusted absolute executable path.")
    return subprocess.run(
        command,
        check=False,
        timeout=timeout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(system_directory()) if os.name == "nt" else str(Path(command[0]).parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def stop_running_app(install_dir: Path, progress: ProgressCallback | None = None) -> None:
    close_exact_processes(install_dir / EXE_NAME, force=True, progress=progress)


def launch_through_user_shell(app_path: Path, *, background: bool = False) -> None:
    """Launch through the existing Explorer shell so the app does not inherit Setup elevation."""
    explorer = system_directory().parent / "explorer.exe"
    if not explorer.exists():
        raise FileNotFoundError("Windows Explorer was not found; start SLS Mass Notify from the Start Menu.")
    shortcut = STARTUP_DIR / f"{APP_DISPLAY_NAME}.lnk" if background else START_MENU_DIR / f"{APP_DISPLAY_NAME}.lnk"
    subprocess.Popen(
        [str(explorer), str(shortcut)], cwd=str(app_path.parent), close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def startup_entry_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_SHORT_NAME)
        return bool(str(value).strip())
    except OSError:
        return False


def saved_startup_preference(default: bool = True) -> bool:
    """Preserve the user's app preference when a reinstall has no Run entry yet."""
    if startup_entry_enabled():
        return True
    try:
        if CONFIG_PATH.exists():
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("startup_enabled"), bool):
                return loaded["startup_enabled"]
    except (OSError, json.JSONDecodeError):
        pass
    return bool(default)


def command_line_value(name: str) -> str:
    try:
        index = sys.argv.index(name)
    except ValueError:
        return ""
    if index + 1 >= len(sys.argv):
        return ""
    return str(sys.argv[index + 1]).strip()


def remove_legacy_entries() -> None:
    """Legacy directories lack ownership manifests and are deliberately retained."""
    return


def create_shortcut(shortcut_path: Path, target_path: Path, *, arguments: str = "", description: str = "") -> None:
    reject_reparse(shortcut_path)
    create_protected_directories(shortcut_path.parent, allow_shell_delete=True)
    if os.name == "nt":
        verify_protected_directory(shortcut_path.parent, allow_shell_delete=True)
    data = base64.b64encode(json.dumps({
        "shortcut": str(shortcut_path), "target": str(target_path),
        "arguments": arguments, "description": description,
        "directory": str(target_path.parent),
    }).encode("utf-8")).decode("ascii")
    # Constant script + base64 JSON avoids command interpolation and a mutable
    # privileged .ps1 file. PowerShell itself comes from GetSystemDirectoryW.
    script = (
        "$ErrorActionPreference='Stop'; "
        "$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + data + "'))|ConvertFrom-Json; "
        "$s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut($p.shortcut); "
        "$l.TargetPath=$p.target; $l.Arguments=$p.arguments; $l.Description=$p.description; "
        "$l.IconLocation=$p.target; $l.WorkingDirectory=$p.directory; $l.Save()"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    executable = system_directory() / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    result = run_hidden([str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], timeout=20)
    if result.returncode != 0 or not shortcut_path.is_file():
        raise RuntimeError(f"Shortcut creation failed: {result.returncode}")


def registered_install_dir() -> Path:
    if winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_REG_PATH, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, "InstallLocation")
            candidate = Path(value)
            return validate_install_dir(candidate)
        except (OSError, ValueError):
            pass
    return DEFAULT_INSTALL_DIR


def saved_machine_preference(name: str, default: bool) -> bool:
    path = registered_install_dir() / DEFAULTS_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get(name), bool):
            return value[name]
    except (OSError, ValueError):
        pass
    return default


def installed_uninstaller(install_dir: Path) -> Path:
    return install_dir / MAINTENANCE_DIR / Path(sys.executable).name


def snapshot_uninstall_registry() -> dict | None:
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_REG_PATH, 0, winreg.KEY_READ) as key:
            values = {}
            for index in range(winreg.QueryInfoKey(key)[1]):
                name, value, kind = winreg.EnumValue(key, index)
                values[name] = (value, kind)
            return values
    except FileNotFoundError:
        return None


def restore_uninstall_registry(values: dict | None) -> None:
    remove_uninstall_registry()
    if values is not None and winreg is not None:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            for name, (value, kind) in values.items():
                winreg.SetValueEx(key, name, 0, kind, value)


def write_uninstall_registry(install_dir: Path, progress: ProgressCallback | None = None) -> None:
    if winreg is None:
        return
    emit(progress, "Registering Windows uninstall entry.")
    app_path = install_dir / EXE_NAME
    uninstaller_path = installed_uninstaller(install_dir)
    install_size_kb = max(
        1,
        sum(file.stat().st_size for file in install_dir.rglob("*") if file.is_file()) // 1024,
    )
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_DISPLAY_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, COMPANY_DISPLAY_NAME)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(install_dir))
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(app_path))
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'"{uninstaller_path}" --uninstall')
        winreg.SetValueEx(
            key,
            "QuietUninstallString",
            0,
            winreg.REG_SZ,
            f'"{uninstaller_path}" --uninstall --quiet',
        )
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, install_size_kb)


def remove_uninstall_registry() -> None:
    if winreg is None:
        return
    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_REG_PATH)
    except FileNotFoundError:
        pass


def require_admin() -> None:
    if os.name != "nt" or not is_admin():
        raise ElevationRequired("An elevated Windows administrator session is required.")


def machine_shortcuts() -> dict[str, Path]:
    return {
        "application": START_MENU_DIR / f"{APP_DISPLAY_NAME}.lnk",
        "uninstall": START_MENU_DIR / f"Uninstall {APP_DISPLAY_NAME}.lnk",
        "startup": STARTUP_DIR / f"{APP_DISPLAY_NAME}.lnk",
    }


def create_protected_directories(path: Path, *, allow_shell_delete: bool = False) -> None:
    """Create protected folders, applying shell deletion policy only for links.

    Existing Start Menu ancestors can allow users to delete entries without
    granting permission to create or modify them. Code/staging callers retain
    the strict default, and newly created directories always get strict ACLs.
    """
    reject_reparse(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    verify_protected_directory(current, allow_shell_delete=allow_shell_delete)
    for directory in reversed(missing):
        directory.mkdir()
        protect_new_directory(directory)


def copy_payload_tree(source: Path, destination: Path, *, skip: Path | None = None) -> None:
    reject_reparse(source)
    source = source.resolve()
    if skip is not None:
        reject_reparse(skip)
        skip = skip.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"Missing onedir package: {source}")
    for item in source.rglob("*"):
        reject_reparse(item)
        if skip is not None and (item == skip or item.is_relative_to(skip)):
            continue
        if item.is_file():
            target = destination / item.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def stage_payload(stage: Path) -> None:
    app_payload = resource_path(APP_SHORT_NAME)
    if not (app_payload / EXE_NAME).is_file():
        raise FileNotFoundError(f"Missing bundled onedir app payload: {app_payload / EXE_NAME}")
    copy_payload_tree(app_payload, stage)
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Install from the packaged onedir installer, not the source script.")
    setup_dir = Path(sys.executable).resolve().parent
    if not (setup_dir / "_internal").is_dir():
        raise RuntimeError("Elevated onefile installers are unsupported. Use the complete onedir release package.")
    copy_payload_tree(setup_dir, stage / MAINTENANCE_DIR, skip=app_payload)


def validate_shortcut_ownership(previous: dict) -> dict[str, bytes | None]:
    snapshots = {}
    expected = previous.get("shortcuts", {})
    for key, path in machine_shortcuts().items():
        reject_reparse(path)
        if path.exists():
            if not path.is_file() or digest(path) != expected.get(key):
                raise ValueError(f"An unowned or changed shortcut will not be overwritten: {path}")
            snapshots[key] = path.read_bytes()
        else:
            snapshots[key] = None
    return snapshots


def install_app(
    install_dir: Path, *, startup: bool | None, launch: bool, remove_legacy: bool,
    auto_update: bool | None, launch_background: bool = False,
    progress: ProgressCallback | None = None,
) -> None:
    require_admin()
    install_dir = validate_install_dir(install_dir)
    previous = installation_manifest(install_dir)
    preferences = {"startup_enabled": True, "auto_update_enabled": False}
    if previous and (install_dir / DEFAULTS_NAME).exists():
        loaded = json.loads((install_dir / DEFAULTS_NAME).read_text(encoding="utf-8"))
        for key in preferences:
            if isinstance(loaded.get(key), bool):
                preferences[key] = loaded[key]
    if startup is not None:
        preferences["startup_enabled"] = bool(startup)
    if auto_update is not None:
        preferences["auto_update_enabled"] = bool(auto_update)
    snapshots = validate_shortcut_ownership(previous)
    registration = snapshot_uninstall_registry()
    create_protected_directories(install_dir.parent)
    verify_protected_directory(install_dir.parent)
    stage = Path(tempfile.mkdtemp(prefix=".sls-stage-", dir=install_dir.parent))
    try:
        protect_new_directory(stage)
        emit(progress, "Staging and validating the complete application package.")
        stage_payload(stage)
        write_json(stage / DEFAULTS_NAME, preferences)
        manifest = manifest_for(stage, version=APP_VERSION)
        emit(progress, "Closing the installed application before replacing files.")
        stop_running_app(install_dir, progress)
        with FileTransaction(install_dir, stage, manifest, secure_directory=protect_new_directory, previous=previous) as transaction:
            try:
                verify_protected_directory(install_dir)
                shortcuts = machine_shortcuts()
                create_shortcut(shortcuts["application"], install_dir / EXE_NAME, description=APP_DISPLAY_NAME)
                create_shortcut(shortcuts["uninstall"], installed_uninstaller(install_dir),
                                arguments="--uninstall", description=f"Uninstall {APP_DISPLAY_NAME}")
                if preferences["startup_enabled"]:
                    create_shortcut(shortcuts["startup"], install_dir / EXE_NAME,
                                    arguments="--background", description=APP_DISPLAY_NAME)
                else:
                    shortcuts["startup"].unlink(missing_ok=True)
                manifest["shortcuts"] = {key: digest(path) for key, path in shortcuts.items() if path.exists()}
                write_uninstall_registry(install_dir, progress)
                transaction.commit()
            except BaseException:
                # Restore machine integration before the context restores files.
                for key, original in snapshots.items():
                    path = machine_shortcuts()[key]
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(original)
                restore_uninstall_registry(registration)
                raise
    finally:
        remove_staging(stage)
    if remove_legacy:
        emit(progress, "Unmanifested legacy installations and per-user settings were retained.")
    if launch:
        try:
            launch_through_user_shell(install_dir / EXE_NAME, background=launch_background)
        except Exception as exc:
            # The committed installation remains usable even if Explorer is absent
            # in a remote management session. This is not an upgrade rollback.
            emit(progress, f"Installed successfully; start from the Start Menu: {exc}")
    emit(progress, "Installation completed successfully.")


def uninstall_app(
    *, quiet: bool = False, remove_settings: bool | None = None,
    progress: ProgressCallback | None = None,
) -> bool:
    """Remove manifest-owned files. Return True only when a reboot is required."""
    require_admin()
    if remove_settings:
        raise ValueError("Machine uninstall retains each user's settings. Remove them in that user's account before uninstalling.")
    executable = Path(sys.executable).resolve()
    install_dir = executable.parent.parent if getattr(sys, "frozen", False) and executable.parent.name == MAINTENANCE_DIR else registered_install_dir()
    install_dir = validate_install_dir(install_dir)
    manifest = read_manifest(install_dir)
    snapshots = validate_shortcut_ownership(manifest)
    if not quiet and progress is None:
        if not messagebox.askyesno(APP_DISPLAY_NAME, "Uninstall SouthlandServers Mass Notification App? Per-user settings will be retained."):
            return False
    emit(progress, "Requesting a graceful exit from the installed application.")
    stop_running_app(install_dir, progress)
    # Verify ALL owned files before removing any of them. Registry/shortcuts
    # remain available if validation fails or a locked file cannot be scheduled.
    reboot = remove_exact_files(install_dir, manifest["files"], defer_locked=delete_after_reboot)
    if reboot:
        # MoveFileEx directory removal never recurses. Scheduling deepest first
        # cleans empty owned folders after locked files; unrelated files remain.
        directories = set()
        for name in manifest["files"]:
            parent = (install_dir / name).parent
            while parent.is_relative_to(install_dir):
                directories.add(parent)
                if parent == install_dir:
                    break
                parent = parent.parent
        for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
            if directory.exists():
                delete_after_reboot(directory)
    for key, original in snapshots.items():
        if original is not None:
            machine_shortcuts()[key].unlink()
    remove_uninstall_registry()
    (install_dir / MANIFEST_NAME).unlink()
    prune_empty(install_dir, list(manifest["files"]))
    try:
        install_dir.rmdir()
    except OSError:
        pass  # Unowned files and files awaiting reboot are retained.
    emit(progress, "Uninstall scheduled; restart Windows to remove locked files." if reboot else "Uninstall completed. Per-user settings retained.")
    return reboot


def configure_modern_style(root: Tk) -> None:
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    page = "#111418"
    surface = "#181c21"
    field = "#0f1317"
    ink = "#f3f4f6"
    muted = "#a7afb9"
    border = "#343b44"
    root.configure(bg=page)
    style.configure(".", background=page, foreground=ink, font=("Segoe UI", 9))
    style.configure("Page.TFrame", background=page)
    style.configure("Card.TFrame", background=surface, borderwidth=1, relief="solid")
    style.configure("Header.TFrame", background=surface)
    style.configure("Header.TLabel", background=surface, foreground=ink, font=("Segoe UI Semibold", 16))
    style.configure("HeaderHint.TLabel", background=surface, foreground=muted, font=("Segoe UI", 9))
    style.configure("Section.TLabel", background=surface, foreground=ink, font=("Segoe UI Semibold", 10))
    style.configure("Hint.TLabel", background=surface, foreground=muted, font=("Segoe UI", 9))
    style.configure("Status.TLabel", background=surface, foreground=muted, font=("Segoe UI", 9))
    style.configure("Card.TCheckbutton", background=surface, foreground=ink, padding=(0, 2))
    style.map("Card.TCheckbutton", background=[("active", surface)])
    style.configure("TEntry", fieldbackground=field, foreground=ink, bordercolor=border, lightcolor=border, darkcolor=border, padding=(7, 5))
    style.configure("TButton", background="#2a3038", foreground=ink, padding=(12, 6), font=("Segoe UI", 9), bordercolor=border)
    style.map("TButton", background=[("active", "#343c46"), ("pressed", "#3d4652")], foreground=[("disabled", "#727b86")])
    style.configure("Accent.TButton", background="#2f81f7", foreground="#ffffff", padding=(14, 6), font=("Segoe UI Semibold", 9))
    style.map("Accent.TButton", background=[("active", "#388bfd"), ("pressed", "#1f6feb")], foreground=[("disabled", "#89929d")])
    style.configure("Horizontal.TProgressbar", background="#2f81f7", troughcolor="#252b32", borderwidth=0)


class SetupOperation:
    """Run blocking setup work off Tk; dispatch every UI callback on Tk's thread."""
    def __init__(self, root, progress, finished) -> None:
        self.root, self.progress, self.finished = root, progress, finished
        self.events = queue.Queue()
        self.active = False
        self.thread = None

    def start(self, work) -> None:
        if self.active:
            raise RuntimeError("A setup operation is already running.")
        self.active = True
        def run():
            try:
                value = work(lambda message: self.events.put(("progress", message)))
                self.events.put(("done", (value, None)))
            except Exception as exc:
                self.events.put(("done", (None, exc)))
        # A transaction must finish/roll back even if its window is closing.
        self.thread = threading.Thread(target=run, name="SetupWorker", daemon=False)
        try:
            self.thread.start()
        except Exception:
            self.active = False
            raise
        self.root.after(50, self.poll)

    def poll(self) -> None:
        for _ in range(32):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                self.progress(value)
            else:
                self.active = False
                self.finished(*value)
                return
        if self.active:
            self.root.after(50, self.poll)


class InstallerWindow:
    def __init__(self, root: Tk | None = None) -> None:
        self.root = root if root is not None else Tk()
        self.root.title(f"{APP_DISPLAY_NAME} Setup")
        self.root.resizable(True, True)
        self.operation = SetupOperation(self.root, self.log, self.install_finished)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        configure_modern_style(self.root)
        icon = resource_path("favicon.ico")
        if icon.exists():
            try:
                self.root.iconbitmap(default=str(icon))
            except Exception:
                pass

        self.install_dir = StringVar(value=str(registered_install_dir()))
        self.startup = BooleanVar(value=saved_machine_preference("startup_enabled", True))
        self.launch = BooleanVar(value=True)
        self.auto_update = BooleanVar(value=saved_machine_preference("auto_update_enabled", False))
        self.accept_terms = BooleanVar(value=False)
        self.status = StringVar(value="Ready to install.")
        self.install_button: ttk.Button | None = None
        self.cancel_button: ttk.Button | None = None
        self.progressbar: ttk.Progressbar | None = None
        self.log_box: Text | None = None
        self._build()
        self._center()

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=1)

        header = ttk.Frame(self.root, padding=(20, 12), style="Header.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="Install SLS Mass Notify", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text=f"Version {APP_VERSION}",
            style="HeaderHint.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        self.root.rowconfigure(1, weight=1)
        self.body = ScrollableBody(self.root)
        self.body.grid(row=1, column=0, sticky="nsew")
        frame = ttk.Frame(self.body.content, padding=(16, 14), style="Page.TFrame")
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        options = ttk.Frame(frame, padding=14, style="Card.TFrame")
        options.grid(row=0, column=0, columnspan=2, sticky="ew")
        options.columnconfigure(0, weight=1)
        ttk.Label(options, text="Install options", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(options, text="Setup closes this installation's app, including stuck background processes.", style="Hint.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(3, 12)
        )
        ttk.Label(options, text="Install folder", style="Hint.TLabel").grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Entry(options, textvariable=self.install_dir, width=48).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(4, 12)
        )
        ttk.Button(options, text="Browse", command=self.browse).grid(row=3, column=2, sticky="ew", padx=(8, 0), pady=(4, 12))
        ttk.Checkbutton(options, text="Run at Windows startup", variable=self.startup, style="Card.TCheckbutton").grid(
            row=4, column=0, columnspan=3, sticky="w"
        )
        ttk.Checkbutton(options, text="Open settings after install", variable=self.launch, style="Card.TCheckbutton").grid(
            row=5, column=0, columnspan=3, sticky="w"
        )
        ttk.Checkbutton(
            options,
            text="Download updates for administrator review",
            variable=self.auto_update,
            style="Card.TCheckbutton",
        ).grid(row=6, column=0, columnspan=3, sticky="w")

        terms = ttk.Frame(frame, padding=14, style="Card.TFrame")
        terms.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        terms.columnconfigure(0, weight=1)
        ttk.Label(terms, text="Terms of Service", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(terms, text="Review and accept the terms before installation.", style="Hint.TLabel").grid(
            row=1, column=0, sticky="w", pady=(3, 9)
        )
        terms_box = Text(
            terms,
            width=88,
            height=6,
            wrap="word",
            state="normal",
            font=("Segoe UI", 9),
            bg="#0f1317",
            fg="#e6edf3",
            insertbackground="#e6edf3",
            selectbackground="#264f78",
            relief="flat",
            padx=10,
            pady=9,
            highlightthickness=1,
            highlightbackground="#343b44",
        )
        terms_box.insert("1.0", TERMS_TEXT)
        terms_box.configure(state="disabled")
        terms_box.grid(row=2, column=0, sticky="ew")
        ttk.Checkbutton(
            terms,
            text="I accept the Terms of Service",
            variable=self.accept_terms,
            command=self.update_install_button_state,
            style="Card.TCheckbutton",
        ).grid(row=3, column=0, sticky="w", pady=(9, 0))

        progress = ttk.Frame(frame, padding=12, style="Card.TFrame")
        progress.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        progress.columnconfigure(0, weight=1)
        self.progressbar = ttk.Progressbar(progress, mode="indeterminate")
        self.progressbar.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        ttk.Label(progress, textvariable=self.status, style="Status.TLabel").grid(row=1, column=0, sticky="w")
        self.log_box = Text(progress, width=88, height=5, state="disabled", wrap="word", font=("Consolas", 8))
        self.log_box.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        actions = ttk.Frame(self.root, padding=(16, 10), style="Page.TFrame")
        actions.grid(row=2, column=0, sticky="e")
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self.close)
        self.cancel_button.pack(side="left", padx=(0, 8))
        self.install_button = ttk.Button(actions, text="Install now", style="Accent.TButton", command=self.install)
        self.install_button.pack(side="left")
        self.update_install_button_state()

    def update_install_button_state(self) -> None:
        if self.install_button is not None:
            self.install_button.configure(state="normal" if self.accept_terms.get() and not self.operation.active else "disabled")

    def log(self, message: str) -> None:
        self.status.set(message)
        if self.log_box is not None:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"{message}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.update_idletasks()

    def _center(self) -> None:
        fit_window(self.root, (850, 760))

    def browse(self) -> None:
        selected = filedialog.askdirectory(initialdir=str(PROGRAM_FILES), title="Choose install folder")
        if selected:
            self.install_dir.set(selected)

    def install(self) -> None:
        if self.operation.active:
            return
        try:
            if not self.accept_terms.get():
                messagebox.showwarning(APP_DISPLAY_NAME, "You must accept the Terms of Service before installing.")
                return
            # Capture Tk variables here; all validation, file work, subprocesses,
            # and shutdown waits happen on the worker without touching widgets.
            install_dir = Path(self.install_dir.get())
            startup, launch, auto_update = self.startup.get(), self.launch.get(), self.auto_update.get()
            if self.install_button is not None:
                self.install_button.configure(state="disabled")
            if self.cancel_button is not None:
                self.cancel_button.configure(state="disabled")
            if self.progressbar is not None:
                self.progressbar.start(12)
            self.log("Installing...")
            self.operation.start(lambda report: install_app(
                install_dir,
                startup=startup,
                launch=launch,
                remove_legacy=True,
                auto_update=auto_update,
                launch_background=False,
                progress=report,
            ))
        except Exception as exc:
            self.install_finished(None, exc)

    def close(self) -> None:
        if self.operation.active:
            self.status.set("Setup is still working. Please wait for it to finish before closing.")
            return
        self.root.destroy()

    def install_finished(self, _result, error) -> None:
        if self.progressbar is not None:
            self.progressbar.stop()
        if error is None:
            messagebox.showinfo(APP_DISPLAY_NAME, "Installation completed successfully.")
            self.root.destroy()
        else:
            self.update_install_button_state()
            if self.cancel_button is not None:
                self.cancel_button.configure(state="normal")
            self.log(f"Installation failed: {error}")
            messagebox.showerror(APP_DISPLAY_NAME, f"Installation failed:\n\n{error}")

    def run(self) -> None:
        self.root.mainloop()


class UninstallerWindow:
    def __init__(self, *, auto_start: bool = False) -> None:
        self.root = Tk()
        self.root.title(f"{APP_DISPLAY_NAME} Uninstall")
        self.root.resizable(True, True)
        self.operation = SetupOperation(self.root, self.log, self.uninstall_finished)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        configure_modern_style(self.root)
        icon = resource_path("favicon.ico")
        if icon.exists():
            try:
                self.root.iconbitmap(default=str(icon))
            except Exception:
                pass

        self.remove_settings = BooleanVar(value=False)
        self.status = StringVar(value="Ready to uninstall.")
        self.uninstall_button: ttk.Button | None = None
        self.cancel_button: ttk.Button | None = None
        self.progressbar: ttk.Progressbar | None = None
        self.log_box: Text | None = None
        self._build(auto_start)
        self._center()
        if auto_start:
            self.root.after(350, self.uninstall)

    def _build(self, auto_start: bool) -> None:
        self.root.columnconfigure(0, weight=1)
        header = ttk.Frame(self.root, padding=(20, 12), style="Header.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="Uninstall SLS Mass Notify", style="Header.TLabel").pack(anchor="w")
        ttk.Label(header, text=f"Version {APP_VERSION}", style="HeaderHint.TLabel").pack(
            anchor="w", pady=(3, 0)
        )

        self.root.rowconfigure(1, weight=1)
        self.body = ScrollableBody(self.root)
        self.body.grid(row=1, column=0, sticky="nsew")
        page = ttk.Frame(self.body.content, padding=16, style="Page.TFrame")
        page.pack(fill="both", expand=True)
        frame = ttk.Frame(page, padding=14, style="Card.TFrame")
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frame, text="Removal options", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            frame,
            text="The app will be stopped and removed from Program Files, startup, the Start Menu, and Windows Apps.",
            style="Hint.TLabel",
            wraplength=650,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 12))
        ttk.Checkbutton(
            frame,
            text="Per-user PBX settings and credentials are retained",
            state="disabled",
            variable=self.remove_settings,
            style="Card.TCheckbutton",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 12))

        self.progressbar = ttk.Progressbar(frame, mode="indeterminate")
        self.progressbar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.log_box = Text(
            frame,
            width=86,
            height=7,
            wrap="word",
            state="disabled",
            font=("Consolas", 8),
            bg="#0f1317",
            fg="#e6edf3",
            insertbackground="#e6edf3",
            selectbackground="#264f78",
            relief="flat",
            padx=9,
            pady=7,
            highlightthickness=1,
            highlightbackground="#343b44",
        )
        self.log_box.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 14))
        ttk.Label(frame, textvariable=self.status, style="Status.TLabel").grid(row=6, column=0, sticky="w")
        self.cancel_button = ttk.Button(frame, text="Cancel", command=self.close)
        self.cancel_button.grid(row=6, column=1, sticky="e", padx=(0, 8))
        self.uninstall_button = ttk.Button(frame, text="Uninstall", style="Accent.TButton", command=self.uninstall)
        self.uninstall_button.grid(row=6, column=2, sticky="e")
        if auto_start and self.uninstall_button is not None:
            self.uninstall_button.configure(state="disabled")

    def _center(self) -> None:
        fit_window(self.root, (850, 760))

    def log(self, message: str) -> None:
        self.status.set(message)
        if self.log_box is not None:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"{message}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.update_idletasks()

    def uninstall(self) -> None:
        if self.operation.active:
            return
        try:
            remove_settings = self.remove_settings.get()
            if self.uninstall_button is not None:
                self.uninstall_button.configure(state="disabled")
            if self.cancel_button is not None:
                self.cancel_button.configure(state="disabled")
            if self.progressbar is not None:
                self.progressbar.start(12)
            self.operation.start(lambda report: uninstall_app(
                quiet=True,
                remove_settings=remove_settings,
                progress=report,
            ))
        except Exception as exc:
            self.uninstall_finished(None, exc)

    def close(self) -> None:
        if self.operation.active:
            self.status.set("Uninstall is still working. Please wait for it to finish before closing.")
            return
        self.root.destroy()

    def uninstall_finished(self, reboot, error) -> None:
        if self.progressbar is not None:
            self.progressbar.stop()
        if error is None:
            messagebox.showinfo(
                APP_DISPLAY_NAME,
                "Uninstall finished. " + ("Restart Windows to remove locked files. " if reboot else "")
                + "Per-user settings were retained.",
            )
            self.root.destroy()
        else:
            if self.uninstall_button is not None:
                self.uninstall_button.configure(state="normal")
            if self.cancel_button is not None:
                self.cancel_button.configure(state="normal")
            self.log(f"Uninstall failed: {error}")
            messagebox.showerror(APP_DISPLAY_NAME, f"Uninstall failed:\n\n{error}")

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    if os.name != "nt":
        print("This installer is for Windows only.")
        return 1633
    check_ui = "--check-ui" in sys.argv
    quiet = "--quiet" in sys.argv or "--silent" in sys.argv or check_ui
    try:
        if check_ui:
            # Packaging smoke test: construct the actual hidden setup window,
            # without elevation, installation, registry writes, or app launch.
            root = Tk()
            root.withdraw()
            try:
                InstallerWindow(root)
                root.update_idletasks()
            finally:
                root.destroy()
            return 0
        if not is_admin():
            if quiet:
                # Management agents require a deterministic failure, not a UAC
                # prompt, detached child process, or an apparent success.
                raise ElevationRequired("Silent setup requires an elevated administrator session.")
            if relaunch_as_admin():
                return 0
            raise ElevationRequired("Administrator elevation was declined or failed.")
        if "--uninstall" in sys.argv:
            if quiet:
                return 3010 if uninstall_app(quiet=True, remove_settings="--remove-settings" in sys.argv) else 0
            UninstallerWindow().run()
            return 0
        if quiet:
            if "--accept-terms" not in sys.argv and "--update" not in sys.argv:
                raise ValueError("Silent installation requires --accept-terms.")
            requested = command_line_value("--install-dir")
            startup_value = command_line_value("--startup")
            update_value = command_line_value("--auto-update")
            for value in (startup_value, update_value):
                if value not in ("", "on", "off"):
                    raise ValueError("--startup and --auto-update accept on or off.")
            install_app(
                Path(requested) if requested else registered_install_dir(),
                startup=None if not startup_value else startup_value == "on",
                launch="--launch" in sys.argv, remove_legacy=False,
                auto_update=None if not update_value else update_value == "on",
                launch_background="--update" in sys.argv,
            )
            return 0
        InstallerWindow().run()
        return 0
    except Exception as exc:
        if quiet:
            if sys.stderr is not None:
                print(f"Setup failed: {exc}", file=sys.stderr)
        else:
            messagebox.showerror(APP_DISPLAY_NAME, f"Setup failed: {exc}")
        return 740 if isinstance(exc, ElevationRequired) else 1603


if __name__ == "__main__":
    sys.exit(main())
