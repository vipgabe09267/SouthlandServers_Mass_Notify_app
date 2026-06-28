from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from tkinter import BooleanVar, StringVar, Text, Tk, filedialog, messagebox
from tkinter import ttk
from typing import Callable

try:
    import winreg
except ImportError:  # pragma: no cover - Windows installer.
    winreg = None


APP_DISPLAY_NAME = "SouthlandServers Mass Notification App"
APP_SHORT_NAME = "SLS_Mass_Notify"
EXE_NAME = "SLS_Mass_Notify.exe"
INSTALLER_EXE_NAME = "SLS_Mass_Notify_Uninstall.exe"
COMPANY_DISPLAY_NAME = "Southland Servers Group"
APP_VERSION = "1.0.4"
AUDIO_DIR_NAME = "audio"
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_REG_PATH = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_SHORT_NAME}"

TERMS_TEXT = """SouthlandServers Mass Notification App Terms of Service

By installing or using this app, you acknowledge that it is a desktop notification client that polls user-configured endpoints and displays returned alert or announcement content.

You are responsible for configuring endpoints, tokens, recipient systems, and server-side alert data accurately. The app does not create weather alerts, verify emergency content, or replace official emergency alerting systems.

Use HTTPS endpoints whenever possible. HTTP endpoints may expose traffic to interception or modification. No-token endpoints may be appropriate only for trusted internal systems and are not recommended for public networks.

The app stores local settings under the current Windows user profile and protects saved tokens with Windows DPAPI when available. The app may check GitHub Releases for updates if automatic updates are enabled during install or in Settings.

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

ProgressCallback = Callable[[str], None]


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
    params = " ".join(f'"{arg}"' for arg in sys.argv[1:])
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        params,
        None,
        1,
    )
    return result > 32


def run_hidden(command: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=False,
        timeout=timeout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def stop_running_app() -> None:
    run_hidden(["taskkill.exe", "/IM", EXE_NAME, "/F"], timeout=10)
    time.sleep(0.5)


def set_startup_enabled(enabled: bool, app_path: Path) -> None:
    if winreg is None:
        return
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(
                key,
                APP_SHORT_NAME,
                0,
                winreg.REG_SZ,
                f'"{app_path}" --background',
            )
        else:
            try:
                winreg.DeleteValue(key, APP_SHORT_NAME)
            except FileNotFoundError:
                pass


def remove_legacy_entries() -> None:
    if winreg is not None:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG_PATH)
        except OSError:
            pass
    if LEGACY_INSTALL_DIR.exists():
        shutil.rmtree(LEGACY_INSTALL_DIR, ignore_errors=True)
    try:
        LEGACY_INSTALL_DIR.parent.rmdir()
    except OSError:
        pass
    if LEGACY_START_MENU_DIR.exists():
        shutil.rmtree(LEGACY_START_MENU_DIR, ignore_errors=True)


def create_shortcut(shortcut_path: Path, target_path: Path, *, arguments: str = "", description: str = "") -> None:
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    script = r"""
param(
    [string]$ShortcutPath,
    [string]$TargetPath,
    [string]$Arguments,
    [string]$Description,
    [string]$WorkingDirectory
)
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $TargetPath
$shortcut.Arguments = $Arguments
$shortcut.Description = $Description
$shortcut.IconLocation = $TargetPath
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
                "-WorkingDirectory",
                str(target_path.parent),
            ],
            timeout=20,
        )
        if result.returncode != 0:
            raise RuntimeError(f"PowerShell shortcut creation failed: {result.returncode}")
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass


def write_uninstall_registry(install_dir: Path, progress: ProgressCallback | None = None) -> None:
    if winreg is None:
        return
    emit(progress, "Registering Windows uninstall entry.")
    app_path = install_dir / EXE_NAME
    uninstaller_path = install_dir / INSTALLER_EXE_NAME
    install_size_kb = max(
        1,
        sum(file.stat().st_size for file in install_dir.glob("*") if file.is_file()) // 1024,
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
            f'"{uninstaller_path}" --uninstall',
        )
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, install_size_kb)


def remove_uninstall_registry() -> None:
    if winreg is None:
        return
    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_REG_PATH)
    except OSError:
        pass


def write_auto_update_preference(enabled: bool | None, progress: ProgressCallback | None = None) -> None:
    if enabled is None:
        return
    emit(progress, "Saving automatic update preference.")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config: dict = {}
    try:
        if CONFIG_PATH.exists():
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config = loaded
    except (OSError, json.JSONDecodeError):
        config = {}
    config["auto_update_enabled"] = bool(enabled)
    temp_path = CONFIG_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    temp_path.replace(CONFIG_PATH)


def copy_audio_assets(install_dir: Path, progress: ProgressCallback | None = None) -> None:
    audio_source = resource_path(AUDIO_DIR_NAME)
    if not audio_source.exists() or not audio_source.is_dir():
        emit(progress, "Bundled audio folder was not found; skipping audio asset copy.")
        return
    audio_destination = install_dir / AUDIO_DIR_NAME
    emit(progress, f"Installing alert audio to {audio_destination}")
    audio_destination.mkdir(parents=True, exist_ok=True)
    for source in audio_source.glob("*.wav"):
        if source.is_file():
            shutil.copy2(source, audio_destination / source.name)


def install_app(
    install_dir: Path,
    *,
    startup: bool,
    launch: bool,
    remove_legacy: bool,
    auto_update: bool | None,
    launch_background: bool = False,
    progress: ProgressCallback | None = None,
) -> None:
    app_payload = resource_path(EXE_NAME)
    if not app_payload.exists():
        raise FileNotFoundError(f"Missing bundled app payload: {app_payload}")

    emit(progress, "Stopping any running copy of SLS Mass Notify.")
    stop_running_app()
    if remove_legacy:
        emit(progress, "Removing old LocalAppData prototype install if it exists.")
        remove_legacy_entries()

    emit(progress, f"Creating install folder: {install_dir}")
    install_dir.mkdir(parents=True, exist_ok=True)
    app_path = install_dir / EXE_NAME
    uninstaller_path = install_dir / INSTALLER_EXE_NAME

    emit(progress, f"Installing {EXE_NAME} to {app_path}")
    shutil.copy2(app_payload, app_path)
    copy_audio_assets(install_dir, progress)
    if getattr(sys, "frozen", False):
        emit(progress, f"Installing {INSTALLER_EXE_NAME} to {uninstaller_path}")
        shutil.copy2(Path(sys.executable), uninstaller_path)

    emit(progress, f"Creating Start Menu shortcuts in {START_MENU_DIR}")
    START_MENU_DIR.mkdir(parents=True, exist_ok=True)
    create_shortcut(
        START_MENU_DIR / f"{APP_DISPLAY_NAME}.lnk",
        app_path,
        description=APP_DISPLAY_NAME,
    )
    create_shortcut(
        START_MENU_DIR / f"Uninstall {APP_DISPLAY_NAME}.lnk",
        uninstaller_path if uninstaller_path.exists() else Path(sys.executable),
        arguments="--uninstall",
        description=f"Uninstall {APP_DISPLAY_NAME}",
    )

    emit(progress, "Configuring Windows startup setting.")
    set_startup_enabled(startup, app_path)
    write_auto_update_preference(auto_update, progress)
    write_uninstall_registry(install_dir, progress)

    if launch:
        emit(progress, "Starting SLS Mass Notify.")
        launch_args = [str(app_path), "--background"] if launch_background else [str(app_path)]
        subprocess.Popen(
            launch_args,
            cwd=str(install_dir),
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    emit(progress, "Installation completed successfully.")


def launch_cleanup(install_dir: Path, remove_settings: bool) -> None:
    cleanup_dir = Path(tempfile.gettempdir()) / f"{APP_SHORT_NAME}_uninstall_{os.getpid()}"
    cleanup_dir.mkdir(parents=True, exist_ok=True)
    cleanup_script = cleanup_dir / "finish_uninstall.cmd"
    lines = [
        "@echo off",
        "setlocal",
        f"set UNINSTALLER_PID={os.getpid()}",
        ":wait_for_uninstaller",
        'tasklist /FI "PID eq %UNINSTALLER_PID%" 2>nul | findstr /R /C:"%UNINSTALLER_PID%" >nul',
        "if not errorlevel 1 (",
        "  timeout /t 1 /nobreak >nul",
        "  goto wait_for_uninstaller",
        ")",
        f'taskkill /IM "{EXE_NAME}" /F >nul 2>nul',
        "timeout /t 1 /nobreak >nul",
        f'rmdir /S /Q "{install_dir}" >nul 2>nul',
        f'rmdir "{install_dir.parent}" >nul 2>nul',
    ]
    if remove_settings:
        lines.append(f'rmdir /S /Q "{CONFIG_DIR}" >nul 2>nul')
        lines.append(f'rmdir "{CONFIG_DIR.parent}" >nul 2>nul')
    lines.extend(
        [
            f'rmdir "{cleanup_dir}" >nul 2>nul',
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


def uninstall_app(
    *,
    quiet: bool = False,
    remove_settings: bool | None = None,
    progress: ProgressCallback | None = None,
) -> None:
    install_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else DEFAULT_INSTALL_DIR
    if remove_settings is None:
        remove_settings = True
    if not quiet and progress is None:
        if not messagebox.askyesno(APP_DISPLAY_NAME, "Uninstall SouthlandServers Mass Notification App?"):
            return
        remove_settings = messagebox.askyesno(APP_DISPLAY_NAME, "Remove saved endpoint settings and tokens too?")

    emit(progress, "Stopping any running copy of SLS Mass Notify.")
    stop_running_app()
    emit(progress, "Removing Windows startup entry.")
    set_startup_enabled(False, install_dir / EXE_NAME)
    emit(progress, "Removing Start Menu shortcuts.")
    for shortcut in (
        START_MENU_DIR / f"{APP_DISPLAY_NAME}.lnk",
        START_MENU_DIR / f"Uninstall {APP_DISPLAY_NAME}.lnk",
    ):
        try:
            shortcut.unlink()
        except OSError:
            pass
    try:
        START_MENU_DIR.rmdir()
    except OSError:
        pass
    emit(progress, "Removing Windows uninstall registry entry.")
    remove_uninstall_registry()
    emit(progress, "Scheduling Program Files cleanup.")
    launch_cleanup(install_dir, remove_settings)
    emit(progress, "Uninstall started. The app files will be removed after this window closes.")
    if not quiet:
        messagebox.showinfo(APP_DISPLAY_NAME, "Uninstall started. The app files will be removed in a moment.")


class InstallerWindow:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title(f"{APP_DISPLAY_NAME} Setup")
        self.root.resizable(False, False)
        icon = resource_path("favicon.ico")
        if icon.exists():
            try:
                self.root.iconbitmap(str(icon))
            except Exception:
                pass

        self.install_dir = StringVar(value=str(DEFAULT_INSTALL_DIR))
        self.startup = BooleanVar(value=True)
        self.launch = BooleanVar(value=True)
        self.auto_update = BooleanVar(value=True)
        self.accept_terms = BooleanVar(value=False)
        self.status = StringVar(value="Ready to install.")
        self.install_button: ttk.Button | None = None
        self.cancel_button: ttk.Button | None = None
        self.progressbar: ttk.Progressbar | None = None
        self.log_box: Text | None = None
        self._build()
        self._center()

    def _build(self) -> None:
        frame = ttk.Frame(self.root, padding=18)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frame, text=APP_DISPLAY_NAME, font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            frame,
            text="Install the background notification app into Program Files.",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 16))

        ttk.Label(frame, text="Install folder").grid(row=2, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.install_dir, width=72).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(3, 12)
        )
        ttk.Button(frame, text="Browse", command=self.browse).grid(row=3, column=2, sticky="ew", padx=(8, 0), pady=(3, 12))

        ttk.Checkbutton(frame, text="Run at Windows startup", variable=self.startup).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(0, 6)
        )
        ttk.Checkbutton(frame, text="Open settings after install", variable=self.launch).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(0, 6)
        )
        ttk.Checkbutton(
            frame,
            text="Automatically install new GitHub Release updates",
            variable=self.auto_update,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 6))

        ttk.Label(frame, text="Terms of Service", font=("Segoe UI", 9, "bold")).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(8, 3)
        )
        terms_box = Text(frame, width=86, height=8, wrap="word", state="normal")
        terms_box.insert("1.0", TERMS_TEXT)
        terms_box.configure(state="disabled")
        terms_box.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        ttk.Checkbutton(
            frame,
            text="I accept the Terms of Service",
            variable=self.accept_terms,
            command=self.update_install_button_state,
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self.progressbar = ttk.Progressbar(frame, mode="indeterminate")
        self.progressbar.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        self.log_box = Text(frame, width=86, height=8, wrap="word", state="disabled")
        self.log_box.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(0, 14))

        ttk.Separator(frame).grid(row=12, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        ttk.Label(frame, textvariable=self.status, foreground="#444444").grid(
            row=13, column=0, sticky="w"
        )
        self.cancel_button = ttk.Button(frame, text="Cancel", command=self.root.destroy)
        self.cancel_button.grid(row=13, column=1, sticky="e", padx=(0, 8))
        self.install_button = ttk.Button(frame, text="Install", command=self.install)
        self.install_button.grid(row=13, column=2, sticky="e")
        self.update_install_button_state()

    def update_install_button_state(self) -> None:
        if self.install_button is not None:
            self.install_button.configure(state="normal" if self.accept_terms.get() else "disabled")

    def log(self, message: str) -> None:
        self.status.set(message)
        if self.log_box is not None:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"{message}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.update_idletasks()

    def _center(self) -> None:
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = max(0, int((self.root.winfo_screenwidth() - width) / 2))
        y = max(0, int((self.root.winfo_screenheight() - height) / 3))
        self.root.geometry(f"+{x}+{y}")

    def browse(self) -> None:
        selected = filedialog.askdirectory(initialdir=str(PROGRAM_FILES), title="Choose install folder")
        if selected:
            self.install_dir.set(selected)

    def install(self) -> None:
        try:
            if not self.accept_terms.get():
                messagebox.showwarning(APP_DISPLAY_NAME, "You must accept the Terms of Service before installing.")
                return
            install_dir = Path(self.install_dir.get()).resolve()
            if self.install_button is not None:
                self.install_button.configure(state="disabled")
            if self.cancel_button is not None:
                self.cancel_button.configure(state="disabled")
            if self.progressbar is not None:
                self.progressbar.start(12)
            self.log("Installing...")
            self.root.update_idletasks()
            install_app(
                install_dir,
                startup=self.startup.get(),
                launch=self.launch.get(),
                remove_legacy=True,
                auto_update=self.auto_update.get(),
                launch_background=False,
                progress=self.log,
            )
            if self.progressbar is not None:
                self.progressbar.stop()
            messagebox.showinfo(APP_DISPLAY_NAME, "Installation completed successfully.")
            self.root.destroy()
        except Exception as exc:
            if self.progressbar is not None:
                self.progressbar.stop()
            if self.install_button is not None:
                self.install_button.configure(state="normal")
            if self.cancel_button is not None:
                self.cancel_button.configure(state="normal")
            self.log("Install failed.")
            messagebox.showerror(APP_DISPLAY_NAME, f"Installation failed:\n\n{exc}")

    def run(self) -> None:
        self.root.mainloop()


class UninstallerWindow:
    def __init__(self, *, auto_start: bool = False) -> None:
        self.root = Tk()
        self.root.title(f"{APP_DISPLAY_NAME} Uninstall")
        self.root.resizable(False, False)
        icon = resource_path("favicon.ico")
        if icon.exists():
            try:
                self.root.iconbitmap(str(icon))
            except Exception:
                pass

        self.remove_settings = BooleanVar(value=True)
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
        frame = ttk.Frame(self.root, padding=18)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frame, text=f"Uninstall {APP_DISPLAY_NAME}", font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            frame,
            text="This will stop the background app, remove startup, remove Start Menu shortcuts, and remove Program Files app files.",
            foreground="#555555",
            wraplength=640,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 12))
        ttk.Checkbutton(
            frame,
            text="Remove saved endpoint settings and tokens",
            variable=self.remove_settings,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self.progressbar = ttk.Progressbar(frame, mode="indeterminate")
        self.progressbar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.log_box = Text(frame, width=86, height=7, wrap="word", state="disabled")
        self.log_box.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 14))
        ttk.Separator(frame).grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        ttk.Label(frame, textvariable=self.status, foreground="#444444").grid(row=6, column=0, sticky="w")
        self.cancel_button = ttk.Button(frame, text="Cancel", command=self.root.destroy)
        self.cancel_button.grid(row=6, column=1, sticky="e", padx=(0, 8))
        self.uninstall_button = ttk.Button(frame, text="Uninstall", command=self.uninstall)
        self.uninstall_button.grid(row=6, column=2, sticky="e")
        if auto_start and self.uninstall_button is not None:
            self.uninstall_button.configure(state="disabled")

    def _center(self) -> None:
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = max(0, int((self.root.winfo_screenwidth() - width) / 2))
        y = max(0, int((self.root.winfo_screenheight() - height) / 3))
        self.root.geometry(f"+{x}+{y}")

    def log(self, message: str) -> None:
        self.status.set(message)
        if self.log_box is not None:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"{message}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.update_idletasks()

    def uninstall(self) -> None:
        try:
            if self.uninstall_button is not None:
                self.uninstall_button.configure(state="disabled")
            if self.cancel_button is not None:
                self.cancel_button.configure(state="disabled")
            if self.progressbar is not None:
                self.progressbar.start(12)
            uninstall_app(
                quiet=True,
                remove_settings=self.remove_settings.get(),
                progress=self.log,
            )
            if self.progressbar is not None:
                self.progressbar.stop()
            messagebox.showinfo(
                APP_DISPLAY_NAME,
                "Uninstall started. The app files will be removed after this window closes.",
            )
            self.root.destroy()
        except Exception as exc:
            if self.progressbar is not None:
                self.progressbar.stop()
            if self.uninstall_button is not None:
                self.uninstall_button.configure(state="normal")
            if self.cancel_button is not None:
                self.cancel_button.configure(state="normal")
            self.log("Uninstall failed.")
            messagebox.showerror(APP_DISPLAY_NAME, f"Uninstall failed:\n\n{exc}")

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if os.name != "nt":
        print("This installer is for Windows only.")
        return

    quiet = "--quiet" in sys.argv
    if "--uninstall" in sys.argv:
        if not is_admin() and relaunch_as_admin():
            return
        UninstallerWindow(auto_start=quiet).run()
        return

    if "--silent" in sys.argv:
        if not is_admin() and relaunch_as_admin():
            return
        install_app(
            DEFAULT_INSTALL_DIR,
            startup=True,
            launch=True,
            remove_legacy=True,
            auto_update=None,
            launch_background="--update" in sys.argv,
        )
        return

    if not is_admin():
        if relaunch_as_admin():
            return
        messagebox.showerror(APP_DISPLAY_NAME, "Administrator permission is required to install to Program Files.")
        return

    InstallerWindow().run()


if __name__ == "__main__":
    main()
