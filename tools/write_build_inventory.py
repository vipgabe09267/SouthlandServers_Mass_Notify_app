"""Write artifact hashes and build provenance for review and support."""
import hashlib
import importlib.metadata
import json
import platform
import re
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sls_version import VERSION


def main():
    root = Path(sys.argv[1]).resolve(strict=True)
    openssl_match = re.match(r"OpenSSL (\d+)\.(\d+)\.(\d+)", ssl.OPENSSL_VERSION)
    openssl_version = tuple(map(int, openssl_match.groups())) if openssl_match else (0, 0, 0)
    inventory = {"application_version": VERSION, "python": platform.python_version(), "openssl": ssl.OPENSSL_VERSION,
                 "runtime_security_warning": ("Development runtime only: OpenSSL 3.5.8 security fixes are not included. Production release is blocked pending a reviewed patched runtime."
                                              if openssl_version < (3, 5, 8) else ""),
                 "native_advisory_source": "https://openssl-library.org/news/openssl-3.5-notes/",
                 "platform": platform.platform(), "packages": sorted(
                     ({"name": entry.metadata["Name"], "version": entry.version} for entry in importlib.metadata.distributions()),
                     key=lambda entry: entry["name"].lower()), "files": []}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"Refusing symlink in release bundle: {path}")
        if path.is_file() and path != root / "build-inventory.json":
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            inventory["files"].append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": digest})
    (root / "build-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
