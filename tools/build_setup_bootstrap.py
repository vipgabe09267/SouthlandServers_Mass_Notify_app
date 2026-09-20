"""Wrap the verified onedir setup in a self-contained .NET Framework launcher."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sls_version import VERSION, VERSION_TUPLE
from sls_install_windows import system_directory


def source_digest():
    return hashlib.sha256(b"".join((ROOT / "tools" / name).read_bytes()
                                  for name in ("setup-bootstrap.cs", "setup-bootstrap.manifest"))).hexdigest()


def main():
    stage = ROOT / "build" / "installer-payload" / "SLS_Mass_Notify_Installer"
    archive = ROOT / "build" / "setup-payload.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(stage).as_posix())
    payload_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    version = ".".join(map(str, VERSION_TUPLE))
    metadata = ROOT / "build" / "SetupBuildInfo.cs"
    metadata.write_text(
        'using System.Reflection;\n'
        f'[assembly: AssemblyVersion("{version}")]\n'
        f'[assembly: AssemblyFileVersion("{version}")]\n'
        f'[assembly: AssemblyInformationalVersion("{VERSION}")]\n'
        '[assembly: AssemblyTitle("SLS Mass Notify Installer")]\n'
        '[assembly: AssemblyProduct("SouthlandServers Mass Notification App")]\n'
        '[assembly: AssemblyCompany("Southland Servers Group")]\n'
        'static class BuildInfo {\n'
        f'public const string Version = "{VERSION}";\n'
        f'public const string PayloadSha256 = "{payload_hash}";\n'
        f'public const string SourceSha256 = "{source_digest()}";\n'
        '}\n', encoding="utf-8")
    compiler = system_directory().parent / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if not compiler.is_file():
        raise SystemExit("The Windows .NET Framework 4.x C# compiler is required.")
    executable = ROOT / "dist" / "SLS_Mass_Notify_Installer.exe"
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/optimize+",
                    "/reference:System.Windows.Forms.dll", "/reference:System.IO.Compression.dll",
                    "/win32icon:" + str(ROOT / "favicon.ico"),
                    "/win32manifest:" + str(ROOT / "tools" / "setup-bootstrap.manifest"),
                    "/resource:" + str(archive) + ",setup-payload.zip",
                    "/out:" + str(executable), str(ROOT / "tools" / "setup-bootstrap.cs"), str(metadata)], check=True)
    print(json.dumps({"installer": str(executable), "version": VERSION, "payload_sha256": payload_hash}))


if __name__ == "__main__":
    main()
