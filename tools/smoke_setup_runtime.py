"""Windows setup integration smoke: real harmless processes, hidden UI, temp files.

No production app, PBX, registry, shared shortcuts, elevation or Program Files mutation.
Windows privilege/registration boundaries are replaced with temporary fixtures;
process shutdown, file replacement, rollback infrastructure, and Tk are real.
"""
from contextlib import ExitStack
from functools import partial
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sls_installer as installer
import sls_install_safety as safety
import sls_install_windows as windows


FIXTURE = r'''
using System;
using System.Diagnostics;
using System.Reflection;
using System.Threading;
class Fixture {
    static int Main(string[] args) {
        if (args.Length == 1 && args[0] == "--parent") {
            using (Process child = Process.Start(new ProcessStartInfo {
                FileName = Assembly.GetExecutingAssembly().Location,
                Arguments = "--child", UseShellExecute = false, CreateNoWindow = true
            })) { child.WaitForExit(); return child.ExitCode; }
        }
        Thread.Sleep(300000);
        return 0;
    }
}
'''


def main():
    full_payload = "--full-payload" in sys.argv
    # Read-only validation against the host's real Start Menu ACLs. Exercise
    # the directory helper too: checking only the link validator missed a
    # stricter ancestor check that rejected otherwise valid shell directories.
    for location in (installer.START_MENU_DIR, installer.STARTUP_DIR):
        existing = location
        while not existing.exists():
            existing = existing.parent
        installer.create_protected_directories(existing, allow_shell_delete=True)
    print("PASS: real shared Start Menu directory permissions (read-only).", flush=True)
    compiler = windows.system_directory().parent / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    with tempfile.TemporaryDirectory(prefix="sls-setup-smoke-") as directory:
        base = Path(directory)
        target, other = base / "installed", base / "unrelated"
        target.mkdir()
        other.mkdir()
        source = base / "Fixture.cs"
        source.write_text(FIXTURE, encoding="utf-8")
        executable = target / installer.EXE_NAME
        subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:x64",
                        "/out:" + str(executable), str(source)], check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        replacement = ((ROOT / "dist" / "SLS_Mass_Notify" / installer.EXE_NAME).read_bytes()
                       if full_payload else executable.read_bytes() + b"replacement-build")
        other_exe = other / installer.EXE_NAME
        shutil.copy2(executable, other_exe)
        safety.write_json(target / safety.MANIFEST_NAME, safety.manifest_for(target, version="fixture-old"))
        (target / "customer-notes.txt").write_text("preserve", encoding="utf-8")
        app = unrelated = root = window = None
        try:
            app = subprocess.Popen([str(executable), "--parent"], creationflags=subprocess.CREATE_NO_WINDOW)
            unrelated = subprocess.Popen([str(other_exe), "--idle"], creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 10
            while len(windows.exact_processes(executable)) != 2:
                if time.monotonic() > deadline:
                    raise AssertionError("Fixture parent/child failed to start")
                time.sleep(0.05)
            assert not windows.terminate_exact_process(unrelated.pid, executable), "Cross-path process was authorized"
            assert unrelated.poll() is None
            assert windows.children_first(windows.exact_processes(executable), windows.process_parents())[-1] == app.pid
            shortcuts = {name: base / (name + ".lnk") for name in ("application", "uninstall", "startup")}
            real_stage_payload = installer.stage_payload

            def stage(path):
                if full_payload:
                    real_stage_payload(path)
                else:
                    (path / installer.EXE_NAME).write_bytes(replacement)
                    (path / "new-runtime.txt").write_text("new", encoding="utf-8")

            with ExitStack() as patches:
                if full_payload:
                    package = ROOT / "build" / "installer-payload" / "SLS_Mass_Notify_Installer"
                    patches.enter_context(mock.patch.object(installer.sys, "frozen", True, create=True))
                    patches.enter_context(mock.patch.object(installer.sys, "executable", str(package / "SLS_Mass_Notify_Installer.exe")))
                    patches.enter_context(mock.patch.object(installer.sys, "_MEIPASS", str(package / "_internal"), create=True))
                for name in ("require_admin", "verify_protected_directory", "protect_new_directory", "write_uninstall_registry", "restore_uninstall_registry"):
                    patches.enter_context(mock.patch.object(installer, name))
                patches.enter_context(mock.patch.object(installer, "registered_install_dir", return_value=target))
                patches.enter_context(mock.patch.object(installer, "validate_install_dir", side_effect=lambda path: path))
                patches.enter_context(mock.patch.object(installer, "snapshot_uninstall_registry", return_value=None))
                patches.enter_context(mock.patch.object(installer, "machine_shortcuts", return_value=shortcuts))
                patches.enter_context(mock.patch.object(installer, "stage_payload", side_effect=stage))
                patches.enter_context(mock.patch.object(installer, "close_exact_processes", side_effect=partial(windows.close_exact_processes, timeout=0.3)))
                success = patches.enter_context(mock.patch.object(installer.messagebox, "showinfo"))
                failure = patches.enter_context(mock.patch.object(installer.messagebox, "showerror", side_effect=lambda *a, **k: root.quit()))
                root = tk.Tk()
                root.withdraw()
                window = installer.InstallerWindow(root)
                window.accept_terms.set(True)
                window.launch.set(False)
                window.startup.set(False)
                ticks = []
                timeout_fired = []
                def heartbeat():
                    ticks.append(time.monotonic())
                    root.after(10, heartbeat)
                def timed_out():
                    timeout_fired.append(True)
                    root.quit()
                root.after(10, heartbeat)
                root.after(120000 if full_payload else 15000, timed_out)
                window.install()
                window.install()  # Duplicate clicks cannot start another transaction.
                root.mainloop()
                if window.operation.thread:
                    window.operation.thread.join(5)
                assert not failure.called, str(failure.call_args)
                assert not timeout_fired, "Setup did not complete within the integration-test deadline"
                assert success.call_count == 1 and not failure.called, str(failure.call_args)
                assert len(ticks) >= 10, "Tk did not keep processing timer events during installation"
                assert windows.exact_processes(executable) == []
                assert app.wait(timeout=5) == 0
                assert unrelated.poll() is None, "An unrelated copy was stopped"
                assert executable.read_bytes() == replacement
                assert (target / "customer-notes.txt").read_text() == "preserve"
                assert safety.read_manifest(target)["version"] == installer.APP_VERSION
                assert windows.normalized_image_path(windows.shortcut_identity(shortcuts["application"])["target"]) == windows.normalized_image_path(executable)
                uninstall_link = windows.shortcut_identity(shortcuts["uninstall"])
                assert uninstall_link["arguments"] == "--uninstall"
                if full_payload:
                    expected_uninstaller = target / installer.MAINTENANCE_DIR / "SLS_Mass_Notify_Installer.exe"
                    assert windows.normalized_image_path(uninstall_link["target"]) == windows.normalized_image_path(expected_uninstaller)
                    assert expected_uninstaller.is_file(), "Uninstall shortcut points to a missing runtime"
                assert not shortcuts["startup"].exists()
                print("PASS: " + ("complete packaged payload upgrade, " if full_payload else "")
                      + "real stuck parent/child shutdown, same-name other-path protection, responsive hidden setup UI, native shell links, file replacement and preserved unrelated content.")
        finally:
            if window is not None and window.operation.thread is not None:
                window.operation.thread.join(10)
            if root is not None:
                try:
                    root.destroy()
                except tk.TclError:
                    pass
            for path in (executable, other_exe):
                for pid in windows.exact_processes(path):
                    windows.terminate_exact_process(pid, path)
            for process in (app, unrelated):
                if process is not None:
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
