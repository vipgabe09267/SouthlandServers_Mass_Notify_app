"""Native announcement image/fallback/footer checks, with no PBX traffic."""
import io
from pathlib import Path
import sys
import threading
import tkinter as tk
from types import SimpleNamespace
from unittest import mock

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sls_presentation as presentation
from sls_mass_notify import SettingsWindow
from sls_presentation_probe import check_presentation
from sls_windowing import fit_window, monitor_work_area
from smoke_window_layout import check_control


def main():
    check_presentation(SettingsWindow._configure_style)
    image = Image.new("RGB", (960, 544), "#1f2937")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 960, 125), fill="#111827")
    draw.rectangle((0, 125, 960, 135), fill="#ffc000")
    draw.text((35, 35), "Color announcement", fill="white", font_size=40)
    draw.text((35, 175), "Fixture message. Sent by: test operator", fill="white", font_size=28)
    data = io.BytesIO()
    image.save(data, format="PNG")
    for scaling in (1.33, 2.0, 2.5):
        root = tk.Tk()
        root.withdraw()
        SettingsWindow._configure_style(SimpleNamespace(window=root))
        root.tk.call("tk", "scaling", scaling)
        real_toplevel = tk.Toplevel

        def invisible(*args, **kwargs):
            result = real_toplevel(*args, **kwargs)
            result.withdraw()
            result.attributes("-alpha", 0.0)
            return result

        left, top, right, bottom = monitor_work_area(root)
        area = (left, top, min(right, left+800), min(bottom, top+540))
        presenter = mock.Mock(root=root, image_loader=lambda _: data.getvalue(),
                              _image_slots=threading.BoundedSemaphore(2))
        views = []
        try:
            with mock.patch.object(tk, "Toplevel", side_effect=invisible), \
                 mock.patch.object(presentation.threading.Thread, "start"), \
                 mock.patch.object(presentation, "fit_window", side_effect=lambda window, size, **kwargs:
                                   fit_window(window, size, area=area, **kwargs)):
                for kind, explicit_test, failed in (("announcement", False, False),
                                                   ("announcement", True, False),
                                                   ("announcement", False, True),
                                                   ("alert", False, False)):
                    alert = dict(kind=kind, title="Color announcement", body="Fixture instructions",
                                 image_url="https://fixture.invalid/image.png", background_color="#1f2937",
                                 severity="critical" if kind == "alert" else "notice",
                                 raw={"is_test": True} if explicit_test else {})
                    item = presentation.PendingAlert("fixture", alert, 0, presentation.presentation_policy(alert))
                    view = presentation._AlertView(presenter, item, 0)
                    views.append(view)
                    if failed:
                        presenter.image_loader = lambda _: b"invalid image"
                    else:
                        presenter.image_loader = lambda _: data.getvalue()
                    view._load_image()
                    view.poll_image()
                    root.after(40, view.poll_image)
                    root.after(80, root.quit)
                    root.mainloop()
                    check_control(view.window, view.ack)
                    assert view.instructions.winfo_ismapped() == (failed or kind == "alert"), (
                        scaling, kind, explicit_test, failed, view.status_var.get(), view.window.winfo_geometry())
                    assert view.image_canvas.winfo_ismapped() == (not failed)
                    if not failed:
                        check_control(view.window, view.image_canvas)
                        assert view._image_ref.width() <= view.image_canvas.winfo_width()
                        assert view._image_ref.height() <= view.image_canvas.winfo_height()
                        assert abs(view._image_ref.width()/view._image_ref.height() - 960/544) < .03
                    if explicit_test:
                        def test_labels(widget):
                            return ([widget] if widget.winfo_class() == "TLabel" and
                                    str(widget.cget("text")).startswith("TEST / EXERCISE") else []) + [
                                label for child in widget.winfo_children() for label in test_labels(child)]
                        marker = test_labels(view.window)[0]
                        check_control(view.window, marker)
                    if kind == "announcement":
                        assert view.ack.cget("text") == "Dismiss"
                        view.ack.invoke()
                        presenter._respond.assert_called_with("fixture", "dismissed")
                    else:
                        assert view.ack.cget("text") == "I have read this alert"
                    # Resizing must resize the image, without covering the footer.
                    fit_window(view.window, (560, 380), area=area)
                    root.update()
                    check_control(view.window, view.ack)
                    if not failed and kind == "announcement":
                        check_control(view.window, view.image_canvas)
                    view.close()
        finally:
            for view in views:
                view.close()
            root.destroy()
    print("PASS: color image only, readable failure fallback, visible Dismiss, persistent-alert read control, "
          "drill marker and image resizing at normal/high scaling in a restricted work area.")


if __name__ == "__main__":
    main()
