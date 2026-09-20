"""Taskbar-aware sizing shared by the desktop client and setup."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk


def apply_window_icon(window):
    """Set both this window's icon and the default for future Tk windows."""
    icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "favicon.ico"
    if not icon.is_file():
        return
    try:
        window.iconbitmap(default=str(icon))
        window.iconbitmap(str(icon))
        window._sls_icon_path = str(icon)
    except tk.TclError:
        pass


def monitor_work_area(window):
    if os.name == "nt":
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
        user = ctypes.windll.user32
        user.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user.MonitorFromPoint.restype = wintypes.HANDLE
        user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        point, info = wintypes.POINT(), MonitorInfo()
        info.size = ctypes.sizeof(info)
        if user.GetCursorPos(ctypes.byref(point)) and user.GetMonitorInfoW(user.MonitorFromPoint(point, 2), ctypes.byref(info)):
            return info.work.left, info.work.top, info.work.right, info.work.bottom
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def window_rectangle(area, preferred, decoration=(16, 48), margin=12):
    left, top, right, bottom = area
    available_w, available_h = max(1, right-left-2*margin), max(1, bottom-top-2*margin)
    width = max(1, min(preferred[0], available_w-decoration[0]))
    height = max(1, min(preferred[1], available_h-decoration[1]))
    x = left + max(0, (right-left-width-decoration[0]) // 2)
    y = top + max(0, (bottom-top-height-decoration[1]) // 2)
    return width, height, x, y


def fit_window(window, preferred=None, *, minimum=(480, 320), area=None):
    """Fit the whole native frame, including its title bar, inside the work area."""
    apply_window_icon(window)
    window.update_idletasks()
    area = area or monitor_work_area(window)
    preferred = preferred or (window.winfo_reqwidth(), window.winfo_reqheight())
    decoration = (16, 48)
    handle = None
    if os.name == "nt":
        user = ctypes.windll.user32
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        handle = user.GetAncestor(window.winfo_id(), 2)
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user.GetClientRect.argtypes = user.GetWindowRect.argtypes
        outer, inner = wintypes.RECT(), wintypes.RECT()
        if handle and user.GetWindowRect(handle, ctypes.byref(outer)) and user.GetClientRect(handle, ctypes.byref(inner)):
            decoration = (max(0, outer.right-outer.left-inner.right), max(0, outer.bottom-outer.top-inner.bottom))
    width, height, x, y = window_rectangle(area, preferred, decoration)
    window.minsize(min(width, minimum[0]), min(height, minimum[1]))
    window.geometry(f"{width}x{height}")
    window.update_idletasks()
    if handle:
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.UINT]
        # Native coordinates support monitors left/above the primary screen;
        # Tk's negative geometry offsets instead mean right/bottom anchoring.
        user.SetWindowPos(handle, None, x, y, 0, 0, 0x1 | 0x4 | 0x10)
    else:
        window.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
    return width, height


class ScrollableBody(ttk.Frame):
    """Both scrollbars stay reachable when content exceeds a small work area."""
    def __init__(self, parent, *, style="Page.TFrame"):
        super().__init__(parent, style=style)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#111418", width=760, height=480)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.content = ttk.Frame(self.canvas, style=style)
        item = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        def resize(_event=None):
            self.canvas.itemconfigure(item, width=max(self.content.winfo_reqwidth(), self.canvas.winfo_width()))
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.content.bind("<Configure>", resize)
        self.canvas.bind("<Configure>", resize)
        def wheel(event):
            widget = event.widget
            while widget is not None and widget is not self:
                if isinstance(widget, tk.Text):
                    return  # Text widgets keep their own scrolling.
                widget = getattr(widget, "master", None)
            if widget is self and event.delta:
                self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return "break"
        parent.winfo_toplevel().bind("<MouseWheel>", wheel, add="+")
