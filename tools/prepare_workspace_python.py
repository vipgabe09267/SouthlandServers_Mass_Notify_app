"""Extract the official pinned Python runtime inside .tools without installing it.

The archive and checksum manifest are fetched over certificate-validated HTTPS
from python.org. Verify the extracted PSF Authenticode signatures before execution.
No installer, registry update, PATH change or installed runtime modification occurs.
"""
import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

VERSION = "3.14.7"
BASE = f"https://www.python.org/ftp/python/{VERSION}/"
ARCHIVE = f"python-{VERSION}-amd64.zip"
EXPECTED_SHA256 = "ac1a727a71738e11de80b76e975f9b8a258aea6412bfc31696b929d59c6aafd0"


def main():
    root = Path(__file__).resolve().parent.parent / ".tools"
    root.mkdir(exist_ok=True)
    if root.is_symlink() or root.resolve().parent != Path(__file__).resolve().parent.parent:
        raise SystemExit("Workspace tools directory is not a direct local directory")
    with urllib.request.urlopen(BASE + f"windows-{VERSION}.json", timeout=30) as response:
        manifest_bytes = response.read(1024 * 1024)
        if response.geturl() != BASE + f"windows-{VERSION}.json":
            raise SystemExit("Unexpected manifest redirect")
    manifest = json.loads(manifest_bytes)
    entry = next(item for item in manifest["versions"] if item.get("url") == BASE + ARCHIVE)
    expected = entry["hash"]["sha256"]
    if expected != EXPECTED_SHA256:
        raise SystemExit("Official manifest differs from the reviewed runtime SHA256 pin")
    archive = root / ARCHIVE
    if archive.is_symlink():
        raise SystemExit("Refusing runtime archive symlink")
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        with urllib.request.urlopen(entry["url"], timeout=30) as response, archive.open("wb") as output:
            if response.geturl() != entry["url"]:
                raise SystemExit("Unexpected runtime redirect")
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 64 * 1024 * 1024:
                    raise SystemExit("Unexpected runtime archive size")
                output.write(chunk)
    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise SystemExit("Official runtime SHA256 does not match the versioned manifest")
    destination = root / f"python-{VERSION}"
    if destination.exists():
        raise SystemExit(f"Verified archive available; refusing to overwrite existing runtime: {destination}")
    with zipfile.ZipFile(archive) as bundle:
        if sum(item.file_size for item in bundle.infolist()) > 300 * 1024 * 1024:
            raise SystemExit("Runtime extraction size limit exceeded")
        for item in bundle.infolist():
            relative = PurePosixPath(item.filename)
            if relative.is_absolute() or ".." in relative.parts or ":" in item.filename or "\\" in item.filename or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise SystemExit("Unsafe path in runtime archive")
            target = destination.joinpath(*relative.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    (root / f"windows-{VERSION}.json").write_bytes(manifest_bytes)
    print(json.dumps({"runtime": str(destination), "source": entry["url"], "sha256": actual,
                      "next": "Verify Python Software Foundation Authenticode publisher before execution"}, indent=2))


if __name__ == "__main__":
    main()
