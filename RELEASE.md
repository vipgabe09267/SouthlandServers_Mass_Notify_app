# Release and deployment

Development builds require Windows x64, **CPython 3.14.7** with OpenSSL 3.5.7 or
later. Production releases additionally require OpenSSL **3.5.8 or later**,
the committed wheel-hash lock, a real publisher code-signing certificate,
and an RFC3161 timestamp provider. The existing Python 3.13.0 environment is suitable
only for source-level regression tests; it is rejected before any build mutation.
Python 3.13.15 still bundles the EOL OpenSSL 3.0 branch, so updating only within
3.13 does not meet this policy. Review Python/OpenSSL updates at each release.

The official Python 3.14.7 bundle contains OpenSSL 3.5.7. OpenSSL 3.5.8 adds
security fixes (maximum published severity Moderate), so the release-only runtime
gate intentionally rejects this development runtime. A reviewed patched runtime
or updated official Python version/pin is still required before production
release. The current Python dependency advisory scan does not cover native-library
advisories. See [OpenSSL 3.5 release notes](https://openssl-library.org/news/openssl-3.5-notes/).

For an isolated workspace build, `tools/prepare_workspace_python.py` downloads the
official full Windows ZIP into ignored `.tools/`, compares the versioned official
manifest and reviewed SHA-256 pin, and extracts without running an installer or
changing registration/PATH. Before running its interpreter, verify `python.exe`,
`python314.dll`, `DLLs/_ssl.pyd`, and `DLLs/_tkinter.pyd` have valid Authenticode
signatures from the Python Software Foundation. Then pass the absolute
`.tools/python-3.14.7/python.exe` path as `-PythonExecutable`.

Sources: [CPython 3.14.7 dependencies](https://github.com/python/cpython/blob/v3.14.7/PCbuild/get_externals.bat),
[OpenSSL 3.0 end of life](https://openssl-library.org/post/2026-09-16-eol30/),
[PyInstaller's privileged onefile warning](https://pyinstaller.org/en/stable/operating-mode.html).

## Building

GitHub beta prereleases may distribute the verified unsigned installer EXE with
its unsigned status and remaining production requirements stated in the release
notes. This does not satisfy the signed production release requirements below.
For `v1.1.6-beta`, attach only `dist/SLS_Mass_Notify_Installer.exe`; keep the local
ZIP and checksum out of the GitHub release assets.

Development: `./build-installer.ps1 -Clean -PythonExecutable C:\Python314\python.exe`.
This produces an explicitly unsigned development ZIP, never a production release.

Release: invoke the same command with `-Release`, an absolute `-SignTool` path,
`-CertificateThumbprint` (the certificate's SHA-1 Windows store selector),
`-TrustedSignerSha256` (independently obtained SHA-256 of the DER certificate), and
`-TimestampUrl` (the certificate provider's HTTPS RFC3161 endpoint). Supply actual
provisioned values; no keys, passwords, or invented pins belong in source control.
The certificate must already be available to the signing account/store/provider.

The release script requires timestamped, trusted signatures on the application,
internal setup executable, and self-contained setup launcher. The launcher's
signature covers its embedded setup runtime and application archive. A timestamped
SHA-256 Windows catalog additionally covers the final launcher.
It produces `dist/packages/SLS_Mass_Notify_Installer.zip` and a SHA-256 sidecar. GitHub asset
metadata must also expose an exact size and `sha256:` digest for client downloads.

The application uses onedir packaging: never distribute its EXE without its
`_internal` runtime. The installer is `dist/SLS_Mass_Notify_Installer.exe`, a
self-contained .NET Framework launcher. Its internal Python setup package is built
under `build/installer-payload/` and embedded into the launcher, together with the
complete application from `dist/SLS_Mass_Notify/`. Build hosts need the Windows
.NET Framework 4.x C# compiler. No Python onefile runtime is elevated from a user
temporary directory.
`build-inventory.json` records the interpreter, OpenSSL, packages, and file hashes.
It is diagnostic metadata; the signed catalog establishes bundle authenticity.

## Deployment trust

Automatic execution/elevation by the desktop updater remains disabled. The setup
launcher requests elevation when opened interactively, then extracts its embedded
payload into a randomly named, protected Program Files staging directory. It checks
the staging owner/ACL and reparse points, validates archive paths and sizes, and
runs Python setup only from that protected directory. It deletes its staging tree
after setup exits. Silent invocation without administrator rights returns 740 and
does not display UAC. Elevation follows the [Windows application manifest and UAC
model](https://learn.microsoft.com/en-us/windows/win32/sbscs/application-manifests).

For production, use enterprise deployment tooling to stage and verify the signed
launcher in an administrator-controlled directory. Interactive development setup
can be opened from Documents/Downloads; it carries its own runtime rather than
loading elevated Python DLLs from those directories.

Before executing the installer, validate all of the following in the protected
directory:

1. `Get-AuthenticodeSignature` reports `Valid` for the adjacent
   `SLS_Mass_Notify_Installer.cat`, and its signer certificate SHA-256 matches the
   independently distributed organization pin.
2. `Test-FileCatalog -Path <installer-exe> -CatalogFilePath <catalog>` reports
   `Valid` with SHA-256; reject additions, removals, or changed files.
3. The launcher has a valid signature from the expected
   publisher and timestamps. Keep previous approved packages for recovery.

The next automatic-update integration requires a protected bootstrap/management
service that stages and validates the **whole** catalog-covered bundle under its
own privileged boundary, plus a signing/key-rotation policy. The desktop client
must never decide trust from a downloaded publisher name or user-editable pin.

For managed installation, invoke the setup EXE from an already-elevated deployment
process with `--silent --accept-terms`, optionally `--install-dir <protected-path>`,
`--startup on|off`, `--auto-update on|off`, and `--launch`. A silent update uses
`--silent --update` and preserves machine defaults. Exit codes are 0 for success,
740 when elevation is required, 1603 for installation failure, and 3010 when
uninstall requires a reboot. Silent operation never displays an elevation prompt.
Existing folders require an installation manifest, except for a specifically
recognized registered 1.0.8-Beta installation. That migration checks the protected
path, HKLM registration, both legacy PE product/version identities, and matching
shortcut targets. It adopts only those two executables and verified shortcuts;
unrelated files and legacy audio remain untouched. The transaction restores the
original executables and registry/shortcuts on handled failure. Other unrecognized
folders require a new dedicated directory. Full elevated deployment acceptance
testing remains required.

Setup work runs off the Tk UI thread. Shutdown first posts close requests and
waits eight seconds, then may terminate remaining processes only after verifying
the executable path on an open process handle. It stops children before onefile
launchers and waits for Windows to release them. This also handles background
processes in other sessions without relying on cross-session window messages.
No process-name-wide or arbitrary descendant termination is used. Exact path
matching expands Windows short names, and access failures report the process ID.
The build runs a real-process temporary-installation smoke test; full payload
validation is available with `tools/smoke_setup_runtime.py --full-payload`.

Supported operating systems must be explicitly serviced Windows editions.
Windows 10 requires a supported LTSC edition or applicable ESU; ordinary Windows
10 servicing ended in October 2025. Prefer supported Windows 11 enterprise images
and maintain a tested OS/architecture/deployment-tool support matrix.

## CI and release review

CI uses exact GitHub Action commits, a fixed Windows Python runtime, advisory checks
for every locked package, unit tests, dependency consistency checks, and onedir
packaging. CI development artifacts expire after seven days. A separate manually
invoked signing workflow is restricted to `main`, the `release-signing` environment,
and a dedicated `sls-signing` runner. It creates an artifact for review; it does not
publish or deploy. Configure required reviewers and restricted branch access on
that environment before provisioning the runner/certificate.

Builds install only committed hashed wheels into a fresh `.venv-build`. To change
dependencies, edit explicit pins in `tools/refresh_build_lock.py`, regenerate the
lock, review versions/hashes/advisories, and run CI. Changes never auto-update the
lock. Runtime versions, external signing providers, and supported operating-system
editions need a human release review as well.

Before production rollout, test signed installation, upgrade/rollback, uninstall,
standard-user initialization, alternate administrator credentials, shared/RDP
sessions, and Windows application-control/endpoint-protection policies on clean VMs.
The repository's tests cannot establish those deployment guarantees.
