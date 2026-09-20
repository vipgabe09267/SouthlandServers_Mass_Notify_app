"""Invisible native Windows layout checks; no settings writes or PBX connections."""
from pathlib import Path
import ctypes
from ctypes import wintypes
import sys
import tempfile
import tkinter as tk
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sls_mass_notify as client
import sls_installer as installer
from sls_windowing import fit_window, monitor_work_area


def check_bounds(window, area):
    window.deiconify()
    fit_window(window, (1140, 840), area=area)
    window.update()
    user = ctypes.windll.user32
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    rect = wintypes.RECT()
    assert user.GetWindowRect(user.GetAncestor(window.winfo_id(), 2), ctypes.byref(rect))
    assert rect.left >= area[0] and rect.top >= area[1], (rect.left, rect.top, area)
    assert rect.right <= area[2] and rect.bottom <= area[3], (rect.right, rect.bottom, area)


def check_control(window, control):
    assert control.winfo_ismapped()
    assert control.winfo_rooty() >= window.winfo_rooty()
    assert control.winfo_rooty()+control.winfo_height() <= window.winfo_rooty()+window.winfo_height()
    assert control.winfo_rootx()+control.winfo_width() <= window.winfo_rootx()+window.winfo_width()


def main():
    with tempfile.TemporaryDirectory(prefix="sls-layout-") as directory:
        for scaling in (1.33, 2.0):
            root = tk.Tk()
            root.withdraw()
            root.attributes("-alpha", 0.0)
            root.tk.call("tk", "scaling", scaling)
            left, top, right, bottom = monitor_work_area(root)
            area = (left, top, min(right, left+1000), min(bottom, top+620))
            try:
                with mock.patch.object(installer, "registered_install_dir", return_value=Path(directory)/"installed"), \
                     mock.patch.object(installer, "saved_machine_preference", side_effect=lambda _, default: default):
                    setup = installer.InstallerWindow(root)
                check_bounds(root, area)
                check_control(root, setup.install_button)
                check_control(root, setup.cancel_button)
                setup.body.canvas.yview_moveto(1)
                root.update()
                assert setup.body.canvas.yview()[1] > .99
            finally:
                root.destroy()
            root = tk.Tk()
            root.withdraw()
            root.tk.call("tk", "scaling", scaling)
            real_toplevel = tk.Toplevel
            def invisible(*args, **kwargs):
                result = real_toplevel(*args, **kwargs)
                result.withdraw()
                result.attributes("-alpha", 0.0)
                return result
            app = mock.Mock(root=root, status_text="Checking delivery.")
            app.get_config.return_value = client.normalize_config({"enabled": False})
            try:
                with mock.patch.object(client, "Toplevel", side_effect=invisible):
                    settings = client.SettingsWindow(app)
                check_bounds(settings.window, area)
                check_control(settings.window, settings.test_button)
                check_control(settings.window, settings.status_label)
            finally:
                root.destroy()
    print("PASS: native Settings and setup frames fit the work area at normal/high scaling; actions remain visible and setup scrolls.")


if __name__ == "__main__":
    main()
