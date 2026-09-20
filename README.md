# SLS Mass Notify

Windows desktop notifications for the Southland Servers FreePBX module.

The current beta is **1.1.6-beta**. Download [SLS_Mass_Notify_Installer.exe](https://github.com/vipgabe09267/SouthlandServers_Mass_Notify_app/releases/download/v1.1.6-beta/SLS_Mass_Notify_Installer.exe) or read the [changelog](CHANGELOG.md).

This is an unsigned beta. See [implementation status](IMPLEMENTATION_STATUS.md) for remaining production requirements and [release instructions](RELEASE.md) for packaging and deployment.

Beta versions use `major.minor.patch-beta` (for example, `1.1.6-beta`, then `1.1.7-beta`). `sls_version.py` supplies the version for the app, installer, and build metadata.

## Setup

1. Download and run the installer EXE on Windows x64. Approve the administrator prompt to install or update.
2. Open SLS Mass Notify from the Start Menu.
3. Enter the PBX HTTPS hostname and the desktop username/password configured on that PBX.
4. Save settings and test the connection. The test verifies authentication and delivery activity; it does not send an announcement.
5. Use **Local preview** and **Play sound** to check this device without sending an alert to the PBX.

The app supports three independent PBX profiles. Passwords are stored with Windows Credential Manager or user-scoped DPAPI. Changes are saved atomically before old credentials are retired. The app never needs PBX administrator credentials or provider secrets.

## Delivery and presentation

- Authenticated HTTPS SSE protocol 2, including `authenticated`, `notification`, `cursor_reset`, `revoked`, and `reconnect` events.
- Delivery is **live-only**. Every SSE connection starts at the server's current journal tail without a replay cursor. Broadcasts missed while shut down, asleep, or disconnected are skipped.
- If an authenticated stream stalls or stream capacity is exhausted, the app uses five-second authenticated snapshot checks. The first snapshot is discarded as a baseline; only newly published records observed during continuous polling are accepted. Every interruption resets that baseline, skipping missed broadcasts. HTTP Retry-After is respected, and the server clock determines which publications are new. Health/status identifies this fallback. A failed stream route is remembered for 24 hours so restarting or reconnecting does not repeat the initial stream timeout. See [stream investigation](STREAM_DIAGNOSTICS.md).
- Previous pending/displayed messages expire on startup. A UI pause of five seconds or more clears the local delivery queue and reconnects from the current tail; stale socket buffers are rejected. Reconnect also retires waiting messages for that profile.
- Notification receipt, deduplication, and observed stream IDs are committed together in SQLite before presentation. Saved IDs are used for diagnostics, never to request replay. Notification SSE IDs must match payload IDs.
- Receipt ACKs use `POST /api/sipnotify/desktop/ack` with `{ "event_id": "..." }`, at most one every two seconds per profile, with durable retry and HTTP 429 backoff.
- The released server v0.1.4 delivery row does not reconcile saved receipts and can remain `published` after a successful ACK. A tested source patch is prepared separately; the PBX needs that server change deployed. See [backend receipt fix](BACKEND_RECEIPT_HANDOFF.md).
- Receipt ACK means device receipt and is sent automatically. Routine notifications have one **Dismiss** button; persistent alerts require **I have read this alert**. These actions are local, not human responses or incident closure sent to the server.
- Color announcements show the server-rendered design as the main content, without a duplicate white text box or repeated heading. Images fit the available space, and the footer stays visible. Details retains the message text; if the image cannot load, readable text appears instead. Explicit test markers remain visible.
- Image loading is checked after the themed window has opened; text remains available until the image is drawn. Each window explicitly uses the bundled SLS icon. Build verification runs an offline `--check-presentation` test inside the packaged app, including asynchronous image display and native window icons.
- Announcements expire locally **10 minutes after publication (`created_at`)**, including queued and visible announcements. An earlier server `display_expires_at` still applies. This local maximum also applies when the sender requests timeout zero. Missing or future publication times are capped at ten minutes from receipt; they cannot create an unlimited queue.
- Weather `effective` and `expires` are respected even for severe alerts.
- Scrollable instructions, bounded visible windows, priority scheduling, optional images, local history, health details, tray status, and diagnostic export.
- Tests use explicit structured metadata such as `is_test: true` or `severity: "Test"`. Wording such as `Lightning Test` or `All Clear` is not interpreted as a lifecycle command.
- Schema-invalid records are retained in a bounded quarantine and reported in Health. A repaired payload can subsequently be accepted. Unreadable/oversized frames or storage failures stop cursor advancement.

The current published backend lacks an immutable revision journal, reliable cancellation delivery, and a structured incident all-clear. The client contains guarded handling for explicit, positively numbered incident revisions, but current backend traffic does not establish that workflow.

Settings, setup, and notification windows fit and center within the active monitor's usable area, including space for the title bar and taskbar. Settings and setup content scrolls on smaller displays; settings controls wrap, and the Install/Cancel buttons remain outside the scrolling setup content.

## Local controls and storage

The tray menu opens Settings, History, and Health, and exits the application. Settings also offers reconnect, local preview, and diagnostic export. Diagnostic exports exclude credentials, PBX addresses, usernames, and notification contents.

Per-user data is stored under:

```text
%APPDATA%\SouthlandServers\SLS_Mass_Notify\
  settings.json
  notifications.sqlite3
  app.log
  audio\
  updates\
```

Terminal notification content defaults to 30 days of retention, checked at startup and hourly. Expiring the delivery queue preserves local history and device-receipt retries; it does not redisplay missed broadcasts. Minimal deduplication and incident-ordering records survive content pruning. Logs rotate at 2 MiB with four backups. Imported WAVs are validated PCM or mu-law, at most 32 MiB and 120 seconds; importing a duplicate filename does not overwrite an existing sound.

## Updates and installation

Update downloads are optional and disabled by default. Downloads require an exact GitHub asset size and SHA-256 digest, a recognized version/channel, and approved redirect hosts. The client does not elevate or automatically execute downloaded files.

Builds contain two programs to launch under `dist`:

| Purpose | Executable |
| --- | --- |
| Install or update the app | `dist\SLS_Mass_Notify_Installer.exe` |
| Run the app directly | `dist\SLS_Mass_Notify\SLS_Mass_Notify.exe` |

The installer is now a **single self-contained EXE**. Double-click it and approve the Windows administrator prompt. It carries the setup runtime and an identical copy of the app inside the EXE, then stages them in a protected Program Files folder before opening setup. It can be launched from Documents or Downloads without the previous "untrusted owner" failure. Setup staging is removed when setup exits. Silent deployment still requires an already elevated session.

The runnable app remains a folder-based package: keep `SLS_Mass_Notify.exe` with its adjacent `_internal` folder. Copying the app EXE alone will not work. There are exactly two EXEs in the generated `dist` tree; internal installer build files stay under `build`.

To transfer the installer, send the installer EXE or `dist\packages\SLS_Mass_Notify_Installer.zip`. The ZIP contains that same installer EXE; extract it before running. Its `.sha256` file is a small text checksum for verifying the ZIP, not another installer. Packaging files stay under `packages`; documentation stays in this repository README.

The GitHub beta release attaches only `SLS_Mass_Notify_Installer.exe`. No separate Python installation, ZIP, or runtime download is needed to install it.

Production deployment requires a real Windows publisher certificate, timestamped signatures, complete signed-catalog verification, and an administrator-controlled staging directory. See [RELEASE.md](RELEASE.md).

The installer uses a file-ownership manifest and protected Program Files locations. Installation and removal run in a background worker so the window stays responsive, with a visible progress log. Setup first requests the installed app to exit; after eight seconds, it ends only remaining processes whose executable path is verified through the same open process handle. Child processes stop before their launcher, and copies in other locations are untouched. Windows short and long path spellings are handled consistently. A busy setup window cannot be closed midway through a transaction.

Setup preserves unrelated files, stages upgrades, and rolls back handled failures. Silent installation has explicit exit codes and no interactive UAC prompt. Machine installation does not write settings or credentials into an elevated administrator's user profile.

Start Menu shortcut folders may grant users deletion rights. Setup accepts those rights consistently when checking existing shortcut folders and their parents, while still rejecting untrusted content-write permissions. Application and runtime folders retain the stricter permissions check.

Registered **1.0.8-Beta** installations can be upgraded in place after checking the protected installation path, Windows registration, both executable product identities, and existing shortcut targets. Only the two verified legacy EXEs and matching shortcuts are adopted; unrelated files and legacy audio are retained. Failed upgrades restore the old files. Other unrecognized nonempty folders require a new dedicated installation folder. A power loss can leave a transaction journal and backup requiring administrator recovery. Machine uninstall preserves per-user settings and credentials; it does not claim to clean every user's registry.

## Build and test

Use the exact reviewed Windows x64 runtime and hash-locked dependencies described in [RELEASE.md](RELEASE.md). A workspace-only official runtime can be prepared without registering Python:

```powershell
python tools/prepare_workspace_python.py
.\build-installer.ps1 -Clean -PythonExecutable "$PWD\.tools\python-3.14.7\python.exe"
```

The build script runs tests, checks dependency consistency, and produces an unsigned development artifact unless the release signing arguments and production runtime policy are satisfied. The prepared Python 3.14.7 runtime contains OpenSSL 3.5.7; production mode requires the newer security patch level documented in RELEASE.md. Runtime metadata and package versions are recorded in `build-inventory.json`.

```text
dist\
  SLS_Mass_Notify\
    SLS_Mass_Notify.exe
    _internal\
    build-inventory.json
  SLS_Mass_Notify_Installer.exe
  packages\
    SLS_Mass_Notify_Installer.zip
    SLS_Mass_Notify_Installer.zip.sha256
```

The `-Clean` build command above removes previous generated `build` and `dist` output before rebuilding. Output names stay fixed instead of accumulating versioned EXEs. The build also needs Windows .NET Framework 4.x and its C# compiler. Packaging verification rejects unexpected EXEs in `dist`, checks the current version, verifies that the embedded app matches the runnable app, constructs the actual hidden setup window without installing, and runs the launcher's non-elevating package self-check. Signed release builds additionally produce a `.cat` catalog.

Builds also run `tools/smoke_setup_runtime.py`: real harmless Windows parent/child processes, a hidden setup window, and a temporary installation verify responsive shutdown/replacement and isolation of another same-named executable. They check the host's real shared Start Menu directory permissions without changing them, and create/read real Windows shortcuts in a temporary folder. Installer builds automatically repeat this test with `--full-payload` to exercise the complete packaged payload. Privilege/registry writes and temporary-folder protection are mocked; these tests do not install to Program Files or contact PBXs.

Builds also run native window-layout checks at normal/higher text scaling and an isolated fresh-publication-to-Tk-alert test with real SQLite storage and a queued receipt.

## License

[GNU Affero General Public License v3.0](LICENSE).
