"""Legacy migration uses explicit product evidence and rollback, never filenames alone."""
import contextlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import sls_installer as installer
import sls_install_safety as safety


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.parent = Path(self.directory.name)
        self.root = self.parent / "old"
        self.root.mkdir()
        for name in (installer.EXE_NAME, installer.INSTALLER_EXE_NAME):
            (self.root / name).write_bytes(b"legacy")
        (self.root / "customer-notes.txt").write_bytes(b"preserve")
        self.registration = {key: (value, 1) for key, value in {
            "DisplayName": installer.APP_DISPLAY_NAME, "Publisher": installer.COMPANY_DISPLAY_NAME,
            "InstallLocation": str(self.root), "DisplayVersion": "1.0.8-Beta",
            "UninstallString": f'"{self.root / installer.INSTALLER_EXE_NAME}" --uninstall',
        }.items()}
        self.enterContext(mock.patch.object(installer, "snapshot_uninstall_registry", return_value=self.registration))
        self.enterContext(mock.patch.object(installer, "verify_protected_directory"))
        self.enterContext(mock.patch.object(installer, "machine_shortcuts", return_value={}))
        self.enterContext(mock.patch.object(installer, "executable_identity", side_effect=self.identity))

    @staticmethod
    def identity(path):
        return {"ProductName": installer.APP_DISPLAY_NAME, "CompanyName": installer.COMPANY_DISPLAY_NAME,
                "FileVersion": "1.0.8-Beta", "OriginalFilename": installer.EXE_NAME if path.name == installer.EXE_NAME
                else "SLS_Mass_Notify_Installer.exe"}

    def test_recognized_legacy_manifest_owns_only_two_verified_executables(self):
        manifest = installer.installation_manifest(self.root)
        self.assertEqual(set(manifest["files"]), {installer.EXE_NAME, installer.INSTALLER_EXE_NAME})
        self.assertFalse((self.root / safety.MANIFEST_NAME).exists())

    def test_registry_and_executable_identity_must_both_match(self):
        for field in ("InstallLocation", "Publisher", "DisplayName", "DisplayVersion", "UninstallString"):
            with self.subTest(field=field), mock.patch.dict(self.registration, {field: ("mismatch", 1)}):
                with self.assertRaises(ValueError):
                    installer.installation_manifest(self.root)
        with mock.patch.object(installer, "executable_identity", return_value={}), self.assertRaises(ValueError):
            installer.installation_manifest(self.root)

    def test_untrusted_legacy_files_cannot_be_adopted(self):
        with mock.patch.object(installer, "verify_protected_directory", side_effect=PermissionError("writable")):
            with self.assertRaises(PermissionError):
                installer.installation_manifest(self.root)

    def test_unrelated_shortcut_cannot_be_adopted(self):
        shortcut = self.parent / "app.lnk"
        shortcut.write_bytes(b"unrelated")
        with mock.patch.object(installer, "machine_shortcuts", return_value={"application": shortcut}), \
             mock.patch.object(installer, "shortcut_identity", return_value={"target": str(self.parent / "other.exe")}):
            with self.assertRaises(ValueError):
                installer.installation_manifest(self.root)

    def test_legacy_commit_removes_old_uninstaller_and_preserves_unrelated_files(self):
        previous = installer.installation_manifest(self.root)
        stage = self.parent / "stage"
        stage.mkdir()
        (stage / installer.EXE_NAME).write_bytes(b"updated")
        with safety.FileTransaction(self.root, stage, safety.manifest_for(stage, version="new"), previous=previous) as transaction:
            transaction.commit()
        self.assertEqual((self.root / installer.EXE_NAME).read_bytes(), b"updated")
        self.assertFalse((self.root / installer.INSTALLER_EXE_NAME).exists())
        self.assertEqual((self.root / "customer-notes.txt").read_bytes(), b"preserve")
        self.assertEqual(safety.read_manifest(self.root)["version"], "new")

    def test_legacy_failed_upgrade_restores_both_executables_and_no_new_manifest(self):
        previous = installer.installation_manifest(self.root)
        stage = self.parent / "stage"
        stage.mkdir()
        (stage / installer.EXE_NAME).write_bytes(b"updated")
        with self.assertRaisesRegex(RuntimeError, "registration"):
            with safety.FileTransaction(self.root, stage, safety.manifest_for(stage, version="new"), previous=previous):
                raise RuntimeError("registration failed")
        for name in (installer.EXE_NAME, installer.INSTALLER_EXE_NAME):
            self.assertEqual((self.root / name).read_bytes(), b"legacy")
        self.assertFalse((self.root / safety.MANIFEST_NAME).exists())
        self.assertEqual((self.root / "customer-notes.txt").read_bytes(), b"preserve")

    def test_ui_check_never_elevates_or_installs(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(installer.sys, "argv", ["setup.exe", "--check-ui"]))
            root = stack.enter_context(mock.patch.object(installer, "Tk")).return_value
            stack.enter_context(mock.patch.object(installer, "InstallerWindow"))
            install = stack.enter_context(mock.patch.object(installer, "install_app"))
            elevate = stack.enter_context(mock.patch.object(installer, "relaunch_as_admin"))
            self.assertEqual(installer.main(), 0)
            install.assert_not_called()
            elevate.assert_not_called()
            root.withdraw.assert_called_once()
            root.destroy.assert_called_once()
