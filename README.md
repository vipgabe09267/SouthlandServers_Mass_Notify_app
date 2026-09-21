# SLS Mass Notify

Windows desktop notifications for the Southland Servers FreePBX module.

**[Download 1.0.9-beta](https://github.com/vipgabe09267/SouthlandServers_Mass_Notify_app/releases/download/v1.0.9-beta/SLS_Mass_Notify_Installer.exe)** | [Changelog](CHANGELOG.md)

## Install

1. Run the installer on Windows x64 and approve the administrator prompt. Python is included.
2. Open SLS Mass Notify from the Start Menu.
3. Enter your PBX HTTPS address and the desktop username and password configured on that PBX.
4. Save and test the connection. Use **Local preview** to test the notification window; the connection test does not send an announcement.

Up to three PBX connections are supported. Use desktop credentials, not PBX administrator credentials. Passwords are protected using Windows Credential Manager or user-scoped DPAPI.

## Notifications

- Announcements arrive live. Messages missed while asleep, shut down, or disconnected are skipped.
- If live streaming stalls, the app checks for new announcements every five seconds. It remembers that fallback for 24 hours.
- Queued and visible announcements expire ten minutes after publication, or earlier if the sender specifies an expiry. Weather alerts use their own expiry.
- Colored announcements display the sender's design. **Details** shows the text, which also appears if the image cannot load.
- Routine notifications have one **Dismiss** button. Persistent alerts require **I have read this alert**. These actions are local.
- The tray menu provides Settings, History, Health, and Exit. Settings includes sound controls and diagnostic export.

Receipt acknowledgements are automatic and confirm receipt by the app, not that someone read the message. Server v0.1.4-beta may still show "Awaiting optional client acknowledgement" after receipt; correcting that status requires a server update.

## Settings and updates

Settings, history, logs, imported sounds, and downloaded updates are stored in `%APPDATA%\SouthlandServers\SLS_Mass_Notify\`. Notification history defaults to 30 days. Uninstalling preserves per-user data.

Update downloads are optional and off by default. The app verifies download size and SHA-256; you run the downloaded installer yourself.

This release is an unsigned beta. Publisher signing, a production runtime update, and installation testing on clean Windows machines remain outstanding. Enrollment, panic activation, human responses, and reliable incident cancellation/all-clear require coordinated server and client changes.

## Build and test

Build on Windows x64 with CPython 3.14.7 and the .NET Framework 4.x C# compiler. Dependencies are pinned in `requirements-build.txt`. To prepare a Python runtime inside the workspace and build:

```powershell
python tools/prepare_workspace_python.py
.\build-installer.ps1 -Clean -PythonExecutable "$PWD\.tools\python-3.14.7\python.exe"
```

`-Clean` replaces generated `build` and `dist` output. The build installs dependencies in `.venv-build`, runs unit tests and native UI/installer checks, and creates:

```text
dist/
  SLS_Mass_Notify_Installer.exe
  SLS_Mass_Notify/
    SLS_Mass_Notify.exe
    _internal/
    build-inventory.json
  packages/
    SLS_Mass_Notify_Installer.zip
    SLS_Mass_Notify_Installer.zip.sha256
```

Distribute the installer EXE. To run the app directly, keep its EXE and `_internal` folder together. GitHub beta releases attach only the installer; ZIP and checksum files are local packaging output.

Run the tests separately after building:

```powershell
.\.venv-build\Scripts\python.exe -m unittest discover -v
```

Tests and contract fixtures are in `tests/`; build helpers and native checks are in `tools/`. App and installer versions come from `sls_version.py`, using `major.minor.patch-beta`.

Signed builds require `-Release`, `-SignTool`, `-CertificateThumbprint`, `-TrustedSignerSha256`, and `-TimestampUrl`. The runtime check requires OpenSSL 3.5.8 or later for release builds; the prepared Python runtime contains 3.5.7 and is accepted only for development builds. Signed packages include a catalog: verify it with `Get-AuthenticodeSignature` and `Test-FileCatalog`, check the expected signer, and stage deployment files in an administrator-controlled directory. Test installation, upgrade, rollback, and uninstall on clean machines before deployment.

For managed installs, run the installer from an elevated process with `--silent --accept-terms`. Optional arguments include `--install-dir`, `--startup on|off`, `--auto-update on|off`, and `--launch`. Use `--silent --update` for updates. Exit codes: `0` success, `740` elevation required, `1603` installation failure, `3010` reboot required after uninstall.

## License

[GNU Affero General Public License v3.0](LICENSE) | [Terms of service](TERMS_OF_SERVICE.md)
