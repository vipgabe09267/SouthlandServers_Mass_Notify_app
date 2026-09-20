"""Manifest-owned filesystem operations for the Windows machine installer.

Never infer directory ownership from an executable's name. These routines are
deliberately usable without Windows APIs so their failure paths can be tested.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

PRODUCT_ID = "SouthlandServers.SLSMassNotify"
MANIFEST_NAME = ".sls-install-manifest.json"
TRANSACTION_NAME = ".sls-install-transaction.json"


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def reject_reparse(path: Path) -> None:
    """Check the lexical path before resolve() can hide a junction or symlink."""
    for item in (path, *path.parents):
        try:
            attrs = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(attrs.st_mode) or getattr(attrs, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Reparse points are not permitted in installation paths: {item}")


def owned_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("Invalid installation manifest path.")
    part = PurePosixPath(relative)
    if part.is_absolute() or any(value in (".", "..", "") for value in relative.split("/")):
        raise ValueError("Installation manifest path escapes the installation directory.")
    if any(value.endswith((" ", ".")) or ntpath.isreserved(value) for value in part.parts):
        raise ValueError("Installation manifest contains a reserved Windows path.")
    target = root.joinpath(*part.parts)
    reject_reparse(target)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Installation manifest path escapes the installation directory.")
    return target


def read_manifest(root: Path, *, required: bool = True) -> dict:
    reject_reparse(root)
    manifest = root / MANIFEST_NAME
    reject_reparse(manifest)
    if not manifest.exists() and not required:
        return {}
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("The folder has no valid SLS installation manifest; no files will be removed.") from exc
    if not isinstance(value, dict) or value.get("product") != PRODUCT_ID or value.get("schema") != 1:
        raise ValueError("The installation manifest does not identify this product.")
    files = value.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("The installation manifest has no owned files.")
    seen = set()
    for name, checksum in files.items():
        owned_path(root, name)
        if name.casefold() in seen or name in (MANIFEST_NAME, TRANSACTION_NAME):
            raise ValueError("Duplicate or reserved installation manifest path.")
        seen.add(name.casefold())
        if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError("Invalid installation file checksum.")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".sls-write-", dir=path.parent)
    temporary = Path(temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def manifest_for(root: Path, *, version: str, shortcuts: dict | None = None) -> dict:
    files = {}
    for path in sorted(root.rglob("*")):
        reject_reparse(path)
        if path.is_file() and path.name not in (MANIFEST_NAME, TRANSACTION_NAME):
            files[path.relative_to(root).as_posix()] = digest(path)
    return {"schema": 1, "product": PRODUCT_ID, "version": version, "files": files,
            "shortcuts": shortcuts or {}}


def prune_empty(root: Path, names: list[str]) -> None:
    directories = set()
    for name in names:
        parent = owned_path(root, name).parent
        while parent != root and parent.is_relative_to(root):
            directories.add(parent)
            parent = parent.parent
    for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        try:
            directory.rmdir()
        except (OSError, FileNotFoundError):
            pass  # Unknown files and nonempty directories are intentionally retained.


def remove_exact_files(root: Path, files: dict[str, str], *, defer_locked=None) -> bool:
    """Preflight all ownership checks, then delete only known, unchanged files.

    A deferred deletion callback may schedule locked files for reboot. Returning
    True means a reboot is required, not that deletion has already completed.
    """
    targets = []
    for name, expected in files.items():
        target = owned_path(root, name)
        if target.exists():
            if not target.is_file() or digest(target) != expected:
                raise ValueError(f"Owned file changed; retained for administrator review: {target}")
            targets.append(target)
    deferred = False
    for target in targets:
        try:
            target.unlink()
        except PermissionError:
            if defer_locked is None:
                raise
            defer_locked(target)
            deferred = True
    prune_empty(root, list(files))
    return deferred


def remove_staging(root: Path) -> None:
    """Delete only files we inventoried inside a newly created staging directory."""
    reject_reparse(root)
    if not root.exists():
        return
    manifest = manifest_for(root, version="staging")
    remove_exact_files(root, manifest["files"])
    for reserved in (MANIFEST_NAME, TRANSACTION_NAME):
        candidate = root / reserved
        reject_reparse(candidate)
        candidate.unlink(missing_ok=True)
    try:
        root.rmdir()
    except OSError:
        pass


class FileTransaction:
    """Per-file upgrade with rollback and a durable interrupted-upgrade journal.

    Caller validates protected parent ACLs before using this class. Staging and
    backup directories must be on the same protected volume as the installation.
    """
    def __init__(self, root: Path, stage: Path, manifest: dict, *, secure_directory=None, previous=None):
        self.root = root
        self.stage = stage
        self.manifest = manifest
        self.previous = read_manifest(root, required=False) if previous is None else previous
        self.backup = Path(tempfile.mkdtemp(prefix=".sls-backup-", dir=root.parent))
        self.secure_directory = secure_directory
        if secure_directory is not None:
            try:
                secure_directory(self.backup)
            except BaseException:
                remove_staging(self.backup)
                raise
        self.changes: list[str] = []
        self.original_manifest = (root / MANIFEST_NAME).read_bytes() if (root / MANIFEST_NAME).exists() else None
        self.committed = False
        self.started = False

    def __enter__(self):
        old_files = self.previous.get("files", {})
        try:
            root_existed = self.root.exists()
            self.root.mkdir(parents=True, exist_ok=True)
            if not root_existed and self.secure_directory is not None:
                self.secure_directory(self.root)
            if (self.root / TRANSACTION_NAME).exists():
                raise RuntimeError("An interrupted upgrade requires recovery before another installation.")
            self.started = True
            for name in set(old_files) | set(self.manifest["files"]):
                target = owned_path(self.root, name)
                if target.exists():
                    if name not in old_files or not target.is_file() or digest(target) != old_files[name]:
                        raise ValueError(f"Refusing to replace an unowned or changed file: {target}")
                    backup_file = owned_path(self.backup, name)
                    backup_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup_file)
            if self.original_manifest is not None:
                (self.backup / MANIFEST_NAME).write_bytes(self.original_manifest)
            for name in sorted(set(old_files) | set(self.manifest["files"])):
                # Journal before changing the destination, so rollback is safe
                # even when replacement fails after the OS has accepted it.
                self.changes.append(name)
                write_json(self.root / TRANSACTION_NAME, {
                    "product": PRODUCT_ID, "backup": str(self.backup),
                    "changes": self.changes, "previous_manifest": self.previous,
                })
                target = owned_path(self.root, name)
                if name in self.manifest["files"]:
                    source = owned_path(self.stage, name)
                    if digest(source) != self.manifest["files"][name]:
                        raise ValueError(f"Staged payload changed: {source}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, target)
                else:
                    target.unlink(missing_ok=True)
            return self
        except BaseException:
            self.rollback()
            raise

    def commit(self) -> None:
        write_json(self.root / MANIFEST_NAME, self.manifest)
        (self.root / TRANSACTION_NAME).unlink(missing_ok=True)
        self.committed = True

    def rollback(self) -> None:
        if not self.started:
            remove_staging(self.backup)
            return
        failures = []
        for name in reversed(self.changes):
            try:
                original = owned_path(self.backup, name)
                target = owned_path(self.root, name)
                if original.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(original, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError("Upgrade rollback needs administrator recovery. Backup retained at "
                               f"{self.backup}: {'; '.join(failures)}")
        if self.original_manifest is not None:
            (self.root / MANIFEST_NAME).write_bytes(self.original_manifest)
        else:
            (self.root / MANIFEST_NAME).unlink(missing_ok=True)
        (self.root / TRANSACTION_NAME).unlink(missing_ok=True)
        prune_empty(self.root, self.changes)
        remove_staging(self.backup)

    def __exit__(self, kind, value, traceback):
        if not self.committed:
            self.rollback()
        else:
            remove_staging(self.backup)
            prune_empty(self.root, list(self.previous.get("files", {})))
        return False
