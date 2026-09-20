"""Windows notification-area health indicator with a dedicated message thread.

Callbacks execute on the tray thread. Callers must post work to their UI queue;
they must never access Tk objects from these callbacks. There is no Tk dependency.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import threading
from typing import Callable
import uuid


LOG = logging.getLogger(__name__)
WM_TRAY = 0x8001
WM_REFRESH = 0x8002
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_CONTEXTMENU = 0x007B
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
NIN_SELECT = 0x0400
NIN_KEYSELECT = 0x0401
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
STATUS_LABELS = {"healthy": "Connected", "degraded": "Degraded", "offline": "Offline"}
MENU_ITEMS = ((1, "Open settings", "open"), (2, "Notification history", "history"),
              (3, "Export diagnostics", "diagnostics"), (4, "Exit", "exit"))


def status_tooltip(status: str, detail: str = "") -> str:
    label = STATUS_LABELS.get(status, STATUS_LABELS["offline"])
    clean = " ".join(str(detail).replace("\x00", "").split())
    result = f"SLS Mass Notify — {label}" + (f": {clean}" if clean else "")
    # szTip is 128 UTF-16 code units, including its terminator.
    return result.encode("utf-16-le", errors="replace")[:254].decode("utf-16-le", errors="ignore")


def callback_action(message: int) -> str | None:
    event = message & 0xFFFF  # NOTIFYICON_VERSION_4 puts icon ID in the high word.
    if event in (NIN_SELECT, NIN_KEYSELECT, WM_LBUTTONDBLCLK):
        return "open"
    if event in (WM_CONTEXTMENU, WM_RBUTTONUP):
        return "menu"
    return None


class _WinAPI:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("The tray indicator is available on Windows only.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                        ctypes.c_size_t, ctypes.c_ssize_t)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", self.WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
                        ("hBalloonIcon", wintypes.HICON)]

        self.WNDCLASS, self.NOTIFYICONDATA = WNDCLASS, NOTIFYICONDATA
        signatures = (
            (self.kernel32.GetModuleHandleW, [wintypes.LPCWSTR], wintypes.HMODULE),
            (self.user32.RegisterClassW, [ctypes.POINTER(WNDCLASS)], wintypes.ATOM),
            (self.user32.UnregisterClassW, [wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
            (self.user32.RegisterWindowMessageW, [wintypes.LPCWSTR], wintypes.UINT),
            (self.user32.CreateWindowExW, [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                         wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                         wintypes.HINSTANCE, wintypes.LPVOID], wintypes.HWND),
            (self.user32.DefWindowProcW, [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_ssize_t),
            (self.user32.GetMessageW, [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT], ctypes.c_int),
            (self.user32.TranslateMessage, [ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
            (self.user32.DispatchMessageW, [ctypes.POINTER(wintypes.MSG)], ctypes.c_ssize_t),
            (self.user32.PostMessageW, [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t], wintypes.BOOL),
            (self.user32.PostQuitMessage, [ctypes.c_int], None),
            (self.user32.DestroyWindow, [wintypes.HWND], wintypes.BOOL),
            (self.user32.IsWindow, [wintypes.HWND], wintypes.BOOL),
            (self.user32.LoadIconW, [wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HICON),
            (self.user32.LoadImageW, [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.HANDLE),
            (self.user32.DestroyIcon, [wintypes.HICON], wintypes.BOOL),
            (self.user32.CreatePopupMenu, [], wintypes.HMENU),
            (self.user32.AppendMenuW, [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR], wintypes.BOOL),
            (self.user32.DestroyMenu, [wintypes.HMENU], wintypes.BOOL),
            (self.user32.SetForegroundWindow, [wintypes.HWND], wintypes.BOOL),
            (self.user32.GetCursorPos, [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            (self.user32.TrackPopupMenu, [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, wintypes.HWND, ctypes.c_void_p], wintypes.UINT),
            (self.shell32.Shell_NotifyIconW, [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)], wintypes.BOOL),
        )
        for function, args, result in signatures:
            function.argtypes, function.restype = args, result


class TrayIcon:
    def __init__(self, on_open: Callable[[], None], on_history: Callable[[], None],
                 on_diagnostics: Callable[[], None], on_exit: Callable[[], None], *,
                 icon_path: str | os.PathLike[str] | None = None) -> None:
        self.callbacks = dict(open=on_open, history=on_history, diagnostics=on_diagnostics, exit=on_exit)
        self.icon_path = Path(icon_path).resolve() if icon_path else None
        self._status, self._detail = "offline", "Starting"
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._api: _WinAPI | None = None
        self._hwnd = None
        self._icon_added = False
        self._custom_icon = None
        self._icons: dict[str, int] = {}
        self._taskbar_created = 0
        self._refresh_pending = False
        self._wndproc_ref = None
        self.error = ""

    @property
    def available(self) -> bool:
        return self._icon_added and self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 2.0) -> bool:
        if os.name != "nt":
            self.error = "The tray indicator is available on Windows only."
            return False
        if self._thread and self._thread.is_alive():
            return self.available
        self._ready.clear()
        self._stop_requested.clear()
        self.error = ""
        self._thread = threading.Thread(target=self._run, name="notification-tray", daemon=True)
        self._thread.start()
        self._ready.wait(max(0.0, min(timeout, 5.0)))
        return self.available

    def update(self, status: str, detail: str = "") -> None:
        if status not in STATUS_LABELS:
            raise ValueError("Tray status must be healthy, degraded, or offline")
        with self._lock:
            changed = (status, detail) != (self._status, self._detail)
            self._status, self._detail = status, str(detail)
            hwnd, api = self._hwnd, self._api
            if (not changed and self._icon_added) or self._refresh_pending or not hwnd or api is None:
                return
            self._refresh_pending = True
        if not api.user32.PostMessageW(hwnd, WM_REFRESH, 0, 0):
            with self._lock:
                self._refresh_pending = False

    def stop(self, timeout: float = 2.0) -> bool:
        self._stop_requested.set()
        with self._lock:
            hwnd, api = self._hwnd, self._api
        if hwnd and api:
            api.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(max(0.0, min(timeout, 5.0)))
        return thread is None or not thread.is_alive()

    def _call(self, action: str) -> None:
        try:
            self.callbacks[action]()
        except Exception:
            LOG.exception("Tray callback failed: %s", action)

    def _notify(self, operation: int) -> bool:
        api = self._api
        if api is None or not self._hwnd:
            return False
        with self._lock:
            status, detail = self._status, self._detail
        data = api.NOTIFYICONDATA()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd, data.uID = self._hwnd, 1
        data.uFlags = 0x01 | 0x02 | 0x04 | 0x80  # MESSAGE, ICON, TIP, SHOWTIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._icons.get(status)
        data.szTip = status_tooltip(status, detail)
        data.uVersion = 4
        result = bool(api.shell32.Shell_NotifyIconW(operation, ctypes.byref(data)))
        if operation == NIM_ADD:
            self._icon_added = result
            if result:
                api.shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))
        elif operation == NIM_DELETE:
            self._icon_added = False
        return result

    def _show_menu(self) -> None:
        api = self._api
        menu = api.user32.CreatePopupMenu()
        if not menu:
            return
        try:
            with self._lock:
                header = status_tooltip(self._status, self._detail)
            api.user32.AppendMenuW(menu, 0x02, 0, header)  # disabled status text
            api.user32.AppendMenuW(menu, 0x0800, 0, None)  # separator
            for command, label, _action in MENU_ITEMS:
                if not api.user32.AppendMenuW(menu, 0, command, label):
                    raise ctypes.WinError(ctypes.get_last_error())
            point = wintypes.POINT()
            if not api.user32.GetCursorPos(ctypes.byref(point)):
                return
            api.user32.SetForegroundWindow(self._hwnd)
            # Right-button selection, return selected command instead of sending it.
            chosen = api.user32.TrackPopupMenu(menu, 0x0100 | 0x0002, point.x, point.y, 0, self._hwnd, None)
            api.user32.PostMessageW(self._hwnd, 0, 0, 0)
            for command, _label, action in MENU_ITEMS:
                if chosen == command:
                    self._call(action)
                    break
        finally:
            api.user32.DestroyMenu(menu)

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == WM_TRAY:
                action = callback_action(lparam)
                if action == "menu":
                    self._show_menu()
                elif action:
                    self._call(action)
                return 0
            if self._taskbar_created and message == self._taskbar_created:
                self._icon_added = False
                self._notify(NIM_ADD)
                return 0
            if message == WM_REFRESH:
                with self._lock:
                    self._refresh_pending = False
                if not self._notify(NIM_MODIFY if self._icon_added else NIM_ADD):
                    self._icon_added = False
                return 0
            if message == WM_CLOSE:
                self._notify(NIM_DELETE)
                self._api.user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                self._api.user32.PostQuitMessage(0)
                return 0
        except Exception:
            # Python exceptions must never unwind through a native callback.
            LOG.exception("Tray window message failed")
            return 0
        return self._api.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _run(self) -> None:
        class_name = f"SLSMassNotifyTray_{os.getpid()}_{uuid.uuid4().hex}"
        registered = False
        instance = None
        try:
            api = self._api = _WinAPI()
            instance = api.kernel32.GetModuleHandleW(None)
            self._taskbar_created = api.user32.RegisterWindowMessageW("TaskbarCreated")
            self._wndproc_ref = api.WNDPROC(self._window_proc)
            window_class = api.WNDCLASS()
            window_class.lpfnWndProc = self._wndproc_ref
            window_class.hInstance = instance
            window_class.lpszClassName = class_name
            if not api.user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True
            # Hidden top-level window receives Explorer's broadcast restart message.
            hwnd = api.user32.CreateWindowExW(0, class_name, "SLS Mass Notify status", 0,
                                             0, 0, 0, 0, None, None, instance, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            with self._lock:
                self._hwnd = hwnd
            for status, resource in (("healthy", 32516), ("degraded", 32515), ("offline", 32513)):
                icon = api.user32.LoadIconW(None, ctypes.c_void_p(resource))
                if not icon:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._icons[status] = icon
            if self.icon_path and self.icon_path.is_file():
                self._custom_icon = api.user32.LoadImageW(None, str(self.icon_path), 1, 0, 0, 0x0010 | 0x0040)
                if self._custom_icon:
                    self._icons["healthy"] = self._custom_icon
            if not self._notify(NIM_ADD):
                self.error = "Windows Explorer did not accept the status icon."
                LOG.warning(self.error)
            self._ready.set()
            if self._stop_requested.is_set():
                api.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            message = wintypes.MSG()
            while True:
                result = api.user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                api.user32.TranslateMessage(ctypes.byref(message))
                api.user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self.error = str(exc)
            LOG.exception("Tray indicator is unavailable")
        finally:
            api = self._api
            if api is not None:
                self._notify(NIM_DELETE)
                if self._hwnd and api.user32.IsWindow(self._hwnd):
                    api.user32.DestroyWindow(self._hwnd)
                if self._custom_icon:
                    api.user32.DestroyIcon(self._custom_icon)
                if registered:
                    api.user32.UnregisterClassW(class_name, instance)
            with self._lock:
                self._hwnd = None
                self._refresh_pending = False
            self._custom_icon = None
            self._icon_added = False
            self._ready.set()
