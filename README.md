# SLS Mass Notify

<p align="center">
  <img src="SLS_Mass_Notif_App.png" width="380" alt="SLS Mass Notify desktop alert preview">
</p>

<p align="center">
  <strong>Windows desktop notifications for Southland Servers PBX</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/release-v1.0.8--beta-2f81f7" alt="v1.0.8-beta">
  <img src="https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0A66C2" alt="Windows 10 and 11">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPLv3-blue" alt="AGPL v3"></a>
</p>

<p align="center">
  <a href="https://github.com/vipgabe09267/SouthlandServers_Mass_Notify_app/releases/latest">Download</a> ·
  <a href="#setup">Setup</a> ·
  <a href="#build-and-test">Build and test</a>
</p>

SLS Mass Notify runs quietly in the Windows background and displays PBX announcements and weather alerts as native desktop notification windows. It supports three PBX profiles, custom alert sounds, Windows startup, automatic reconnect, and verified application updates.

## Current release

| Component | Supported configuration |
| --- | --- |
| Desktop app | v1.0.8-Beta |
| PBX plugin | v0.0.7-beta or newer |
| Delivery | Authenticated live event stream |
| Authentication | PBX desktop username and password |
| Network | HTTPS with a Windows-trusted certificate |
| Operating system | Windows 10 or Windows 11, 64-bit |

The legacy standalone polling transport and its bearer-token, no-auth, HTTP, and certificate-bypass options were retired in v1.0.8-Beta. Existing profiles are migrated automatically; usable addresses, usernames, passwords, reconnect settings, and event history are retained.

## What’s new in v1.0.8-Beta

- Simplified PBX setup around the current live connection flow.
- Fixed targeted announcements that could be queued by the PBX but omitted from the stream.
- Fixed connection tests that could remain stuck in progress.
- Improved reconnect, resume, routing, and duplicate-event handling.
- Refined the dark-mode application and installer interfaces.
- Reworked alert spacing and typography to prevent overlapping text.
- Strengthened HTTPS, redirect, media-download, response-size, and installer safeguards.
- Added proper Windows product and version information to both executables.
- Pinned and audited the build dependencies.

## Features

- Three independently configurable PBX connections
- Weather-alert and announcement presentation modes
- Targeted-user and all-desktop notification routing
- `Last-Event-ID` resume after connection interruptions
- Bundled WAV tones, sound preview, and custom WAV import
- Optional launch at Windows sign-in
- Optional verified updates from GitHub Releases
- Windows Credential Manager integration
- Standard Start Menu and Installed Apps entries

## Install

Download `SLS_Mass_Notify_Installer.exe` from the [latest GitHub release](https://github.com/vipgabe09267/SouthlandServers_Mass_Notify_app/releases/latest), then run it as Administrator.

The default installation folder is:

```text
C:\Program Files\Southland Servers Group\SLS Mass Notify
```

The installer preserves existing app settings during upgrades. The current public executables are not code-signed, so Windows SmartScreen may show a warning.

## Setup

1. Open **SLS Mass Notify** from the Start Menu.
2. Enter a name for the PBX connection.
3. Enter the PBX hostname or HTTPS origin, such as `https://pbx.example.com`.
4. Enter the desktop username and password configured in the PBX plugin.
5. Select **Test connections**.
6. Choose a notification sound and select **Save changes**.

The app can monitor up to three PBXs. Each connection has its own address, credentials, enabled state, reconnect preference, and event history.

## Delivery behavior

The app opens the PBX desktop event stream using HTTP Basic authentication:

```http
GET /api/sipnotify/desktop/stream HTTP/1.1
Accept: text/event-stream
Authorization: Basic BASE64_USERNAME_PASSWORD
Last-Event-ID: MOST_RECENT_ACCEPTED_ID
```

The PBX must begin the stream with an authenticated event:

```text
event: authenticated
data: {"ok":true,"transport":"live_sse","session_id":"...","client_id":"..."}
```

Notifications are accepted when either:

- `desktop_all` is `true`; or
- the configured desktop username appears in `desktop_recipients`.

Once the live session is authenticated, the client also checks the PBX’s recent-event view for messages omitted from the stream. This recovery check is tied to the authenticated live session and is not a selectable legacy transport. Both paths use the same event-ID history, preventing duplicate popups and sounds.

Supported notification fields include:

- `id`, `kind`, `title`, `message`, and `description`
- `priority`, `severity`, `area`, `effective`, and `expires`
- `desktop_all` and `desktop_recipients`
- same-origin HTTPS `image_url` values
- presentation colors supplied by the PBX

## Security

- PBX credentials are transmitted only over HTTPS.
- TLS certificate validation is always enabled.
- Authenticated redirects cannot leave the original PBX origin.
- Alert images must use the same HTTPS origin as the PBX and are limited to 5 MB.
- Recent-event JSON responses are limited to 1 MB.
- SSE line and event sizes are bounded.
- XML payloads are processed with `defusedxml`.
- Passwords are stored in Windows Credential Manager, with encrypted DPAPI fallback.
- Update installers must match the SHA-256 digest published by GitHub.
- The installer rejects system roots and unrelated non-empty installation folders.

Settings are stored under the current Windows profile:

```text
%APPDATA%\SouthlandServers\SLS_Mass_Notify\settings.json
```

## Alert sounds

The default sound is `audio\Announcement.wav`. Additional bundled sounds are available from Settings. Imported WAV files must be smaller than 25 MB and are copied to:

```text
%APPDATA%\SouthlandServers\SLS_Mass_Notify\audio
```

## Build and test

The build scripts target Python 3.13 and produce 64-bit Windows GUI executables.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m unittest -v
.\build-installer.ps1 -Clean
```

Build outputs:

```text
dist\SLS_Mass_Notify.exe
dist\SLS_Mass_Notify_Installer.exe
```

Optional security checks:

```powershell
.\.venv\Scripts\python.exe -m pip_audit -r requirements-build.txt
.\.venv\Scripts\python.exe -m bandit -q -ll -r sls_mass_notify.py sls_installer.py
```

The v1.0.8-Beta release passed 26 automated tests, dependency auditing, static checks, packaged launch testing, and live authentication against configured PBXs.

## Uninstall

Use **Windows Settings > Apps > Installed apps**, or the uninstall shortcut under **Start Menu > Southland Servers Group**. The uninstaller can retain or remove saved settings and credentials.

## Operational notice

Validate PBX authentication, targeted delivery, all-desktop delivery, reconnect/resume behavior, audio, and alert presentation in the deployment environment. This application complements PBX alerting and does not replace official emergency-warning systems.

## License

SLS Mass Notify is licensed under the [GNU Affero General Public License v3.0](LICENSE).
