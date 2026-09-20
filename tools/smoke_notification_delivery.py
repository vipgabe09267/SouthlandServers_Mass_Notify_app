"""Isolated fresh publication -> polling -> SQLite -> real Tk alert/receipt test."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sls_mass_notify as client


def main():
    with tempfile.TemporaryDirectory(prefix="sls-delivery-") as directory:
        root = tk.Tk()
        root.withdraw()
        real_toplevel = tk.Toplevel
        def invisible(*args, **kwargs):
            window = real_toplevel(*args, **kwargs)
            window.withdraw()
            window.attributes("-alpha", 0.0)
            return window
        profile = client.normalize_endpoint({"enabled": True, "endpoint": "https://fixture.invalid",
            "username": "fixture", "password": "fixture"}, 0)
        cfg = client.normalize_config({"enabled": True, "endpoints": [profile]})
        now = time.time()
        def notice(name, published):
            return dict(id=name, kind="announcement", title="Local delivery fixture", message="Test instructions",
                desktop_recipients=["fixture"], created_at=datetime.fromtimestamp(published, timezone.utc).isoformat(),
                display_timeout_seconds=0, image_url="https://fixture.invalid/optional.png")
        old, fresh = notice("old-fixture", now-120), notice("fresh-fixture", now)
        app = None
        try:
            with mock.patch.object(client, "CONFIG_DIR", Path(directory)), \
                 mock.patch.object(client, "CONFIG_PATH", Path(directory)/"settings.json"), \
                 mock.patch.object(client, "load_config", return_value=cfg), \
                 mock.patch.object(client, "log"), mock.patch.object(client, "TrayIcon"), \
                 mock.patch.object(client, "winsound", None), \
                 mock.patch.object(client.threading.Thread, "start"), \
                 mock.patch.object(client.MassNotifyApp, "play_notification_sound", return_value=0), \
                 mock.patch.object(tk, "Toplevel", side_effect=invisible):
                app = client.MassNotifyApp(root, False)
                worker = client.EndpointTransportWorker(app, 0, profile)
                app.workers[0] = worker
                with mock.patch.object(client, "fetch_endpoint", side_effect=[
                    ({"ok": True, "events": [old]}, "", now),
                    ({"ok": True, "events": [old, fresh]}, "", now+5)]), \
                     mock.patch.object(worker.stop_event, "wait", side_effect=[False, True]):
                    worker._run_live_polling()
                app.pump_inbox()
                root.update()
                history = app.inbox.history()
                assert len(history) == 1 and history[0]["event_id"] == "fresh-fixture", history
                assert history[0]["state"] == "displayed", history[0]["error"]
                assert len(app.presenter.visible) == 1
                window = next(iter(app.presenter.visible.values())).window
                assert window.winfo_ismapped()
                def button_labels(widget):
                    labels = []
                    for child in widget.winfo_children():
                        if child.winfo_class() == "TButton":
                            labels.append(str(child.cget("text")))
                        labels.extend(button_labels(child))
                    return labels
                labels = button_labels(window)
                assert "Dismiss" in labels, labels
                assert "I have read this announcement" not in labels, labels
                assert app.inbox.pending_receipts(client.profile_namespace(profile))[0]["event_id"] == "fresh-fixture"
                app.presenter.shutdown()
            print("PASS: fresh-only fallback publication stored, displayed in a real Tk alert, and queued for receipt; old history skipped.")
        finally:
            if app:
                app.stop_event.set()
                app.inbox.close()
            root.destroy()


if __name__ == "__main__":
    main()
