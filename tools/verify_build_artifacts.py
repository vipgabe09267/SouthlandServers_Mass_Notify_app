"""Inspect packaged artifacts without launching the desktop client or installer."""
import hashlib
import json
import marshal
import ssl
import sys
import subprocess
import types
from pathlib import Path

import pefile
from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sls_version import VERSION
from build_setup_bootstrap import source_digest


def normalized_code(code):
    return code.replace(co_filename="<source>", co_consts=tuple(
        normalized_code(item) if isinstance(item, types.CodeType) else item for item in code.co_consts))


def code_equal(left, right):
    # Compare bytecode recursively. Marshal reference/interning flags can differ
    # even for equal code objects, so compare normalized objects directly.
    return normalized_code(left) == normalized_code(right)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(directory, main):
    inventory = json.loads((directory / "build-inventory.json").read_text(encoding="utf-8"))
    if inventory["application_version"] != VERSION or inventory["python"] != "3.14.7" or inventory["openssl"] != ssl.OPENSSL_VERSION:
        raise SystemExit(f"Unexpected runtime or application version in {directory}")
    expected = {item["path"] for item in inventory["files"]}
    # Nested application inventories are already covered as files in the installer
    # inventory; exclude only the inventory for the current root.
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file() and path != directory / "build-inventory.json"}
    if actual != expected:
        raise SystemExit(f"Artifact inventory does not cover exact bundle content: {directory}")
    for item in inventory["files"]:
        path = directory / item["path"]
        if path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
            raise SystemExit(f"Artifact inventory mismatch: {path}")
    executable = directory / ("SLS_Mass_Notify.exe" if main == "sls_mass_notify" else "SLS_Mass_Notify_Installer.exe")
    with pefile.PE(str(executable)) as image:
        if image.FILE_HEADER.Machine != 0x8664:
            raise SystemExit(f"Unexpected executable architecture: {executable}")
        versions = []
        for section in getattr(image, "FileInfo", []):
            for item in section:
                for strings in getattr(item, "StringTable", []):
                    versions.append(strings.entries.get(b"FileVersion", b"").decode("utf-8"))
        if VERSION not in versions:
            raise SystemExit(f"Executable version resource does not match {VERSION}: {executable}")
    archive = CArchiveReader(str(executable))
    source = compile((ROOT / f"{main}.py").read_bytes(), str(ROOT / f"{main}.py"), "exec", optimize=0)
    if not code_equal(marshal.loads(archive.extract(main)), source):
        raise SystemExit(f"Packaged entry point differs from source: {main}")
    modules = archive.open_embedded_archive("PYZ.pyz")
    for name in modules.toc:
        if name.startswith("sls_") and (ROOT / f"{name}.py").exists():
            source = compile((ROOT / f"{name}.py").read_bytes(), str(ROOT / f"{name}.py"), "exec", optimize=0)
            if not code_equal(modules.extract(name), source):
                raise SystemExit(f"Packaged module differs from current source: {name}")
    base_dll = Path(sys.base_prefix) / "python314.dll"
    if digest(directory / "_internal" / "python314.dll") != digest(base_dll):
        raise SystemExit("Packaged Python DLL differs from the checked build interpreter")
    return executable


def main():
    distribution = ROOT / "dist"
    expected_executables = {
        "SLS_Mass_Notify/SLS_Mass_Notify.exe",
        "SLS_Mass_Notify_Installer.exe",
    }
    actual_executables = {path.relative_to(distribution).as_posix()
                          for path in distribution.rglob("*")
                          if path.is_file() and path.suffix.lower() == ".exe"}
    if actual_executables != expected_executables:
        raise SystemExit("Unexpected or missing EXEs in dist; rebuild with -Clean. "
                         f"Extra: {sorted(actual_executables - expected_executables)}; "
                         f"missing: {sorted(expected_executables - actual_executables)}")
    application = verify(ROOT / "dist" / "SLS_Mass_Notify", "sls_mass_notify")
    subprocess.run([str(application), "--check-presentation"], timeout=15,
                   creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    installer = verify(ROOT / "build" / "installer-payload" / "SLS_Mass_Notify_Installer", "sls_installer")
    embedded = installer.parent / "_internal" / "SLS_Mass_Notify" / "SLS_Mass_Notify.exe"
    if digest(embedded) != digest(application):
        raise SystemExit("Installer contains a different application payload")
    subprocess.run([str(installer), "--check-ui"], timeout=45,
                   creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    bootstrap = distribution / "SLS_Mass_Notify_Installer.exe"
    report = subprocess.run([str(bootstrap), "--check-package"], capture_output=True, text=True,
                            timeout=30, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    expected_report = f"SLS_PACKAGE_OK {VERSION} {digest(ROOT / 'build' / 'setup-payload.zip')} {source_digest()}"
    if report.stdout.strip() != expected_report:
        raise SystemExit(f"Bootstrap embedded package verification failed: {report.stdout!r}")
    print(f"Verified app and self-contained installer: version {VERSION}, Python 3.14.7, {ssl.OPENSSL_VERSION}; inventories, source bytecode, identical app payload, and bootstrap package self-check.")


if __name__ == "__main__":
    main()
