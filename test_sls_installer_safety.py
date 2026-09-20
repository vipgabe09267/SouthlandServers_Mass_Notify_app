"""Installer tests use isolated temporary files and mock all Windows mutations."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import sls_installer as installer
import sls_install_safety as safety


class ManifestSafetyTests(unittest.TestCase):
    def test_manifest_rejects_traversal_and_windows_alternate_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in ("../notes.txt", "/notes.txt", "C:/notes.txt", "a\\b", "a:stream", "a/./b", "a//b", ".. /escape", "NUL", "app.exe."):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    safety.owned_path(root, value)

    def test_filename_does_not_establish_directory_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            protected = Path(directory)
            target = protected / "SLS"
            target.mkdir()
            (target / installer.EXE_NAME).write_bytes(b"untrusted download")
            with mock.patch.object(installer, "program_files_directory", return_value=protected), \
                    mock.patch.object(installer, "verify_protected_directory"), \
                    self.assertRaises(ValueError):
                installer.validate_install_dir(target)

    def test_uninstall_preserves_unowned_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "owned.exe").write_bytes(b"owned")
            manifest = safety.manifest_for(root, version="1")
            (root / "customer-notes.txt").write_text("keep me", encoding="utf-8")
            self.assertFalse(safety.remove_exact_files(root, manifest["files"]))
            self.assertFalse((root / "owned.exe").exists())
            self.assertEqual((root / "customer-notes.txt").read_text(), "keep me")

    def test_changed_owned_file_prevents_any_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.exe").write_bytes(b"a")
            (root / "b.exe").write_bytes(b"b")
            manifest = safety.manifest_for(root, version="1")
            (root / "b.exe").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                safety.remove_exact_files(root, manifest["files"])
            self.assertTrue((root / "a.exe").exists())

    def test_manifest_rejects_case_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            safety.write_json(root / safety.MANIFEST_NAME, {
                "product": safety.PRODUCT_ID, "schema": 1,
                "files": {"app.exe": "0" * 64, "APP.exe": "0" * 64},
            })
            with self.assertRaises(ValueError):
                safety.read_manifest(root)

    def test_successful_upgrade_removes_retired_owned_files_and_keeps_unowned(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stage = Path(directory) / "install", Path(directory) / "stage"
            root.mkdir()
            stage.mkdir()
            (root / "app.exe").write_bytes(b"old")
            (root / "retired.dll").write_bytes(b"old dependency")
            safety.write_json(root / safety.MANIFEST_NAME, safety.manifest_for(root, version="1"))
            (root / "user.txt").write_bytes(b"user data")
            (stage / "app.exe").write_bytes(b"new")
            with safety.FileTransaction(root, stage, safety.manifest_for(stage, version="2")) as transaction:
                transaction.commit()
            self.assertEqual((root / "app.exe").read_bytes(), b"new")
            self.assertFalse((root / "retired.dll").exists())
            self.assertEqual((root / "user.txt").read_bytes(), b"user data")
            self.assertEqual(safety.read_manifest(root)["version"], "2")
            self.assertFalse((root / safety.TRANSACTION_NAME).exists())

    def test_failure_after_file_install_rolls_back_every_owned_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stage = Path(directory) / "install", Path(directory) / "stage"
            root.mkdir()
            stage.mkdir()
            (root / "app.exe").write_bytes(b"old")
            previous = safety.manifest_for(root, version="1")
            safety.write_json(root / safety.MANIFEST_NAME, previous)
            (stage / "app.exe").write_bytes(b"new")
            (stage / "new.dll").write_bytes(b"new dependency")
            with self.assertRaisesRegex(RuntimeError, "registration"):
                with safety.FileTransaction(root, stage, safety.manifest_for(stage, version="2")):
                    raise RuntimeError("registration failed")
            self.assertEqual((root / "app.exe").read_bytes(), b"old")
            self.assertFalse((root / "new.dll").exists())
            self.assertEqual(safety.read_manifest(root), previous)

    def test_failed_replace_rolls_back_partial_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stage = Path(directory) / "install", Path(directory) / "stage"
            root.mkdir()
            stage.mkdir()
            for name in ("a.exe", "b.dll"):
                (root / name).write_bytes(b"old")
                (stage / name).write_bytes(b"new")
            safety.write_json(root / safety.MANIFEST_NAME, safety.manifest_for(root, version="1"))
            replace = safety.os.replace

            def fail_second(source, target):
                if Path(source) == stage / "b.dll":
                    raise PermissionError("file locked")
                return replace(source, target)

            with mock.patch.object(safety.os, "replace", side_effect=fail_second), self.assertRaises(PermissionError):
                with safety.FileTransaction(root, stage, safety.manifest_for(stage, version="2")):
                    self.fail("Must fail during apply")
            self.assertEqual((root / "a.exe").read_bytes(), b"old")
            self.assertEqual((root / "b.dll").read_bytes(), b"old")
            self.assertEqual(safety.read_manifest(root)["version"], "1")

    def test_interrupted_upgrade_journal_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stage = Path(directory) / "install", Path(directory) / "stage"
            root.mkdir()
            stage.mkdir()
            (root / "app.exe").write_bytes(b"old")
            safety.write_json(root / safety.MANIFEST_NAME, safety.manifest_for(root, version="1"))
            (root / safety.TRANSACTION_NAME).write_text("existing journal")
            (stage / "app.exe").write_bytes(b"new")
            with self.assertRaises(RuntimeError):
                with safety.FileTransaction(root, stage, safety.manifest_for(stage, version="2")):
                    pass
            self.assertEqual((root / safety.TRANSACTION_NAME).read_text(), "existing journal")


class InstallerFlowTests(unittest.TestCase):
    def test_onedir_payload_keeps_runtime_but_excludes_duplicate_bundled_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package, stage = root / "package", root / "stage"
            payload = package / "_internal" / installer.APP_SHORT_NAME
            payload.mkdir(parents=True)
            stage.mkdir()
            (payload / installer.EXE_NAME).write_bytes(b"app")
            (package / "setup.exe").write_bytes(b"installer")
            (package / "_internal" / "python.dll").write_bytes(b"runtime")
            with mock.patch.object(installer.sys, "frozen", True, create=True), \
                    mock.patch.object(installer.sys, "executable", str(package / "setup.exe")), \
                    mock.patch.object(installer, "resource_path", return_value=payload):
                installer.stage_payload(stage)
            self.assertEqual((stage / installer.EXE_NAME).read_bytes(), b"app")
            self.assertTrue((stage / ".maintenance" / "_internal" / "python.dll").is_file())
            self.assertFalse((stage / ".maintenance" / "_internal" / installer.APP_SHORT_NAME).exists())

    def test_onedir_uninstall_targets_application_root_and_preserves_unowned_files(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            maintenance = root / installer.MAINTENANCE_DIR
            maintenance.mkdir()
            executable = maintenance / "setup.exe"
            executable.write_bytes(b"setup")
            (root / installer.EXE_NAME).write_bytes(b"app")
            safety.write_json(root / safety.MANIFEST_NAME, safety.manifest_for(root, version="1"))
            (root / "user-document.txt").write_bytes(b"preserve")
            stack.enter_context(mock.patch.object(installer.sys, "frozen", True, create=True))
            stack.enter_context(mock.patch.object(installer.sys, "executable", str(executable)))
            for name in ("require_admin", "stop_running_app", "remove_uninstall_registry", "delete_after_reboot"):
                stack.enter_context(mock.patch.object(installer, name))
            validate = stack.enter_context(mock.patch.object(installer, "validate_install_dir", side_effect=lambda value: value))
            stack.enter_context(mock.patch.object(installer, "machine_shortcuts", return_value={}))
            self.assertFalse(installer.uninstall_app(quiet=True))
            validate.assert_called_once_with(root.resolve())
            self.assertEqual((root / "user-document.txt").read_bytes(), b"preserve")
            self.assertFalse((root / installer.EXE_NAME).exists())

    def test_locked_owned_file_is_scheduled_without_deleting_unknown_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "setup.exe"
            target.write_bytes(b"locked")
            manifest = safety.manifest_for(root, version="1")
            (root / "notes.txt").write_bytes(b"keep")
            unlink = Path.unlink

            def locked(path, *args, **kwargs):
                if path == target:
                    raise PermissionError("running executable")
                return unlink(path, *args, **kwargs)

            schedule = mock.Mock()
            with mock.patch.object(Path, "unlink", locked):
                self.assertTrue(safety.remove_exact_files(root, manifest["files"], defer_locked=schedule))
            schedule.assert_called_once_with(target)
            self.assertEqual((root / "notes.txt").read_bytes(), b"keep")

    def test_silent_elevation_failure_never_creates_gui_or_installs(self):
        with mock.patch.object(installer.sys, "argv", ["setup.exe", "--silent", "--accept-terms"]), \
                mock.patch.object(installer, "is_admin", return_value=False), \
                mock.patch.object(installer, "InstallerWindow") as gui, \
                mock.patch.object(installer, "install_app") as install, \
                mock.patch.object(installer, "relaunch_as_admin") as elevate:
            self.assertEqual(installer.main(), 740)
            gui.assert_not_called()
            install.assert_not_called()
            elevate.assert_not_called()

    def test_quiet_uninstall_returns_reboot_code_without_gui(self):
        with mock.patch.object(installer.sys, "argv", ["setup.exe", "--uninstall", "--quiet"]), \
                mock.patch.object(installer, "is_admin", return_value=True), \
                mock.patch.object(installer, "UninstallerWindow") as gui, \
                mock.patch.object(installer, "uninstall_app", return_value=True) as uninstall:
            self.assertEqual(installer.main(), 3010)
            uninstall.assert_called_once_with(quiet=True, remove_settings=False)
            gui.assert_not_called()

    def test_silent_upgrade_preserves_unspecified_preferences_and_does_not_launch(self):
        with mock.patch.object(installer.sys, "argv", ["setup.exe", "--silent", "--update"]), \
                mock.patch.object(installer, "is_admin", return_value=True), \
                mock.patch.object(installer, "registered_install_dir", return_value=Path("C:/Program Files/SLS")), \
                mock.patch.object(installer, "install_app") as install:
            self.assertEqual(installer.main(), 0)
            self.assertIsNone(install.call_args.kwargs["startup"])
            self.assertIsNone(install.call_args.kwargs["auto_update"])
            self.assertFalse(install.call_args.kwargs["launch"])

    def test_machine_uninstall_refuses_cross_user_settings_deletion(self):
        with mock.patch.object(installer, "require_admin"), \
                mock.patch.object(installer, "stop_running_app") as stop, \
                self.assertRaisesRegex(ValueError, "each user's settings"):
            installer.uninstall_app(quiet=True, remove_settings=True)
        stop.assert_not_called()

    def test_install_integration_failure_restores_old_payload(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            parent = Path(directory)
            root = parent / "installed"
            root.mkdir()
            (root / installer.EXE_NAME).write_bytes(b"old app")
            safety.write_json(root / installer.DEFAULTS_NAME, {"startup_enabled": False, "auto_update_enabled": False})
            old_manifest = safety.manifest_for(root, version="1")
            safety.write_json(root / safety.MANIFEST_NAME, old_manifest)
            shortcuts = {key: parent / f"{key}.lnk" for key in ("application", "uninstall", "startup")}
            for function in ("require_admin", "verify_protected_directory", "protect_new_directory", "stop_running_app", "restore_uninstall_registry"):
                stack.enter_context(mock.patch.object(installer, function))
            stack.enter_context(mock.patch.object(installer, "validate_install_dir", return_value=root))
            stack.enter_context(mock.patch.object(installer, "machine_shortcuts", return_value=shortcuts))
            stack.enter_context(mock.patch.object(installer, "snapshot_uninstall_registry", return_value=None))
            stack.enter_context(mock.patch.object(installer, "stage_payload", side_effect=lambda stage: (stage / installer.EXE_NAME).write_bytes(b"new app")))
            stack.enter_context(mock.patch.object(installer, "create_shortcut", side_effect=lambda path, *args, **kwargs: path.write_bytes(b"shortcut")))
            stack.enter_context(mock.patch.object(installer, "write_uninstall_registry", side_effect=OSError("registry denied")))
            with self.assertRaisesRegex(OSError, "registry denied"):
                installer.install_app(root, startup=None, launch=False, remove_legacy=False, auto_update=None)
            self.assertEqual((root / installer.EXE_NAME).read_bytes(), b"old app")
            self.assertEqual(safety.read_manifest(root), old_manifest)
            self.assertFalse(any(path.exists() for path in shortcuts.values()))

    def test_install_persists_machine_preferences_without_touching_user_config(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            parent = Path(directory)
            root = parent / "installed"
            user_config = parent / "user-settings.json"
            user_config.write_text('{"private": "preserve"}')
            shortcuts = {key: parent / f"{key}.lnk" for key in ("application", "uninstall", "startup")}
            for function in ("require_admin", "verify_protected_directory", "protect_new_directory", "stop_running_app", "write_uninstall_registry"):
                stack.enter_context(mock.patch.object(installer, function))
            stack.enter_context(mock.patch.object(installer, "validate_install_dir", return_value=root))
            stack.enter_context(mock.patch.object(installer, "machine_shortcuts", return_value=shortcuts))
            stack.enter_context(mock.patch.object(installer, "snapshot_uninstall_registry", return_value=None))
            stack.enter_context(mock.patch.object(installer, "CONFIG_PATH", user_config))
            stack.enter_context(mock.patch.object(installer, "stage_payload", side_effect=lambda stage: (stage / installer.EXE_NAME).write_bytes(b"new app")))
            stack.enter_context(mock.patch.object(installer, "create_shortcut", side_effect=lambda path, *args, **kwargs: path.write_bytes(b"shortcut")))
            installer.install_app(root, startup=False, launch=False, remove_legacy=False, auto_update=False)
            self.assertEqual(json.loads((root / installer.DEFAULTS_NAME).read_text()), {
                "startup_enabled": False, "auto_update_enabled": False,
            })
            self.assertEqual(json.loads(user_config.read_text()), {"private": "preserve"})
            self.assertFalse(shortcuts["startup"].exists())


class ShortcutDirectoryTests(unittest.TestCase):
    def test_shell_policy_reaches_existing_directory_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def verify(path, *, allow_shell_delete=False):
                if not allow_shell_delete:
                    raise PermissionError("Deletion-only shell ACL")
            with mock.patch.object(installer, "verify_protected_directory", side_effect=verify):
                installer.create_protected_directories(root, allow_shell_delete=True)
                with self.assertRaises(PermissionError):
                    installer.create_protected_directories(root)

    def test_new_shell_folder_checks_ancestor_and_protects_new_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "vendor"
            with mock.patch.object(installer, "verify_protected_directory") as verify, \
                 mock.patch.object(installer, "protect_new_directory") as protect:
                installer.create_protected_directories(target, allow_shell_delete=True)
            verify.assert_called_once_with(root, allow_shell_delete=True)
            protect.assert_called_once_with(target)
            self.assertTrue(target.is_dir())

    def test_untrusted_writes_still_prevent_shell_folder_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "vendor"
            with mock.patch.object(installer, "verify_protected_directory", side_effect=PermissionError("Untrusted writes")), \
                 self.assertRaises(PermissionError):
                installer.create_protected_directories(target, allow_shell_delete=True)
            self.assertFalse(target.exists())

    def test_shortcut_creation_checks_shell_policy_before_running_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shortcut = root / "app.lnk"
            checks = []
            def verify(path, *, allow_shell_delete=False):
                self.assertTrue(allow_shell_delete)
                checks.append(path)
            def write(*args, **kwargs):
                self.assertTrue(checks)
                shortcut.write_bytes(b"link")
                return mock.Mock(returncode=0)
            with mock.patch.object(installer, "verify_protected_directory", side_effect=verify), \
                 mock.patch.object(installer, "run_hidden", side_effect=write):
                installer.create_shortcut(shortcut, root / "app.exe")
            self.assertTrue(shortcut.exists())


if __name__ == "__main__":
    unittest.main()
