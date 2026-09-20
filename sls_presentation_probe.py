"""Offline native presentation self-check for the packaged Windows runtime."""
import io
import os
from pathlib import Path
import threading
import time
import tkinter as tk
from types import SimpleNamespace

from PIL import Image

from sls_presentation import PendingAlert, _AlertView, presentation_policy
from sls_windowing import apply_window_icon


def check_presentation(configure_style):
    """Use real asynchronous image loading and the app theme, without user data."""
    root = tk.Tk()
    root.withdraw()
    apply_window_icon(root)
    configure_style(SimpleNamespace(window=root))
    data = io.BytesIO()
    Image.new("RGB", (480, 272), "#d42c41").save(data, format="PNG")
    ready = threading.Event()
    finished = threading.Event()
    view = None
    try:
        def image_loader(_alert):
            if not ready.wait(5):
                raise RuntimeError("Image self-check timed out")
            return data.getvalue()

        presenter = SimpleNamespace(root=root, image_loader=image_loader,
                                    _image_slots=threading.BoundedSemaphore(2),
                                    _respond=lambda *_: None, show_history=lambda: None)
        alert = dict(kind="announcement", title="Local presentation check", body="Fixture instructions",
                     image_url="https://fixture.invalid/announcement.png", severity="notice")
        view = _AlertView(presenter, PendingAlert("fixture", alert, 0, presentation_policy(alert)), 0)
        view.window.attributes("-alpha", 0.0)
        root.update()
        assert view.instructions.winfo_ismapped(), "Initial text fallback is hidden"
        deadline = time.monotonic() + 5

        def poll():
            view.poll_image()
            if view._image_ref is not None or time.monotonic() >= deadline:
                finished.set()
                root.quit()
            else:
                root.after(25, poll)

        # Finish the download after Tk has mapped the text and entered its event loop.
        root.after(250, ready.set)
        root.after(275, poll)
        root.mainloop()
        root.update_idletasks()
        assert finished.is_set() and view._image_ref is not None, "Delayed image did not render"
        assert view.image_canvas.winfo_ismapped(), "Delayed image canvas is hidden"
        assert view.image_canvas.find_withtag("announcement"), "Image canvas is empty"
        assert not view.instructions.winfo_ismapped(), "Duplicate text remains visible"
        assert view.ack.winfo_ismapped() and view.ack.cget("text") == "Dismiss"
        assert view.ack.winfo_rooty() + view.ack.winfo_height() <= view.window.winfo_rooty() + view.window.winfo_height()
        for window in (root, view.window):
            assert Path(getattr(window, "_sls_icon_path", "")).name == "favicon.ico", "SLS icon is not set on the window"
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                user = ctypes.windll.user32
                user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
                user.GetAncestor.restype = wintypes.HWND
                user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                user.SendMessageW.restype = ctypes.c_ssize_t
                handle = user.GetAncestor(window.winfo_id(), 2)
                for icon_size in (0, 1):
                    assert user.SendMessageW(handle, 0x7F, icon_size, 0), "Native window icon is missing"
        view.window.geometry("560x380")
        root.update()
        assert view.image_canvas.winfo_ismapped() and view._image_ref.width() > 1
        assert view._image_ref.width() <= view.image_canvas.winfo_width()
        assert view._image_ref.height() <= view.image_canvas.winfo_height()
    finally:
        ready.set()
        if view is not None:
            view.close()
        root.destroy()
