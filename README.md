# SouthlandServers Mass Notification App

<p align="center">
  <img src="SLS_Mass_Notif_App.png" alt="SLS Mass Notify desktop alert" width="620">
</p>

<p align="center">
  <a href="#install"><img src="https://img.shields.io/badge/platform-Windows-0A66C2" alt="Windows"></a>
  <a href="#build-and-test"><img src="https://img.shields.io/badge/built%20with-Python-3776AB" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPLv3-blue" alt="AGPL v3"></a>
</p>

SLS Mass Notify is a Windows desktop companion for Southland Servers PBX alerting. It maintains authenticated PBX connections in the background, plays a selected WAV tone, and presents weather alerts and announcements without requiring an open browser.

Current release: **v1.0.8-Beta**

## Highlights

- Up to three independent PBX connections
- Authenticated live event streams with reconnect and `Last-Event-ID` resume
- Deduplicated recovery of announcements omitted by an active stream
- Separate layouts for weather alerts and general announcements
- Bundled and custom WAV notification sounds
- Optional Windows startup and verified GitHub Release updates
- Dark settings and installer interfaces
- Standard Windows install, Start Menu, and uninstall integration

## Requirements

- Windows 10 or Windows 11
- A PBX with the v0.0.7-beta or newer desktop live-handshake API
- A desktop username and password created on that PBX
- HTTPS with a certificate trusted by Windows

Legacy JSON polling, bearer-token profiles, unauthenticated profiles, HTTP endpoints, and certificate-validation bypasses are no longer supported as of v1.0.8-Beta. Existing settings are migrated to the current profile format; PBX address, username, password, reconnect preference, and event history are preserved when available.

## Install

Download `SLS_Mass_Notify_Installer.exe` from [GitHub Releases](https://github.com/vipgabe09267/SouthlandServers_Mass_Notify_app/releases) and run it as Administrator.

The default install folder is:

```text
C:\Program Files\Southland Servers Group\SLS Mass Notify
```

The installer adds Start Menu shortcuts and a Windows Installed Apps entry. It preserves existing settings during upgrades. Windows SmartScreen may warn about unsigned builds; sign both executables before broad distribution.

## Configure

Open Settings and complete one PBX profile:

1. Enter a friendly name and the PBX HTTPS address.
2. Enter the desktop username and password from the PBX.
3. Leave the profile and automatic reconnect enabled.
4. Choose or import a notification sound.
5. Select **Test connections**, then save the changes.

Passwords are stored in Windows Credential Manager. DPAPI is retained only as an encrypted compatibility fallback when Credential Manager is unavailable.

## PBX connection contract

The client opens this authenticated stream:

```http
GET /api/sipnotify/desktop/stream HTTP/1.1
Accept: text/event-stream
Authorization: Basic BASE64_USERNAME_PASSWORD
Last-Event-ID: MOST_RECENT_ACCEPTED_ID
```

The first named event must authenticate the session:

```text
event: authenticated
data: {"ok":true,"transport":"live_sse","session_id":"...","client_id":"..."}
```

Notifications arrive as named `notification` events. A record is shown only when `desktop_all` is `true` or the configured desktop username appears in `desktop_recipients`.

While a stream is authenticated, the app also reconciles recent records from `/api/sipnotify/desktop?limit=25`. This closes a PBX delivery gap where a targeted announcement can be queued but not emitted on the stream. Stream and reconciliation records share the same event-ID history, so a notification is shown and sounded once.

Example announcement payload:

```json
{
  "id": "announcement-20260902153000",
  "kind": "announcement",
  "title": "Building Notice",
  "message": "The west entrance is temporarily closed.",
  "desktop_all": false,
  "desktop_recipients": ["frontdesk"]
}
```

Weather records may also include `priority`, `severity`, `area`, `effective`, `expires`, `description`, and a same-origin HTTPS `image_url`.

## Security

- PBX credentials are sent only over HTTPS.
- Certificate validation is always enabled.
- Redirects on authenticated PBX and media requests are restricted to the original origin.
- Alert images must use the same HTTPS origin as the PBX and are limited to 5 MB.
- JSON reconciliation responses are limited to 1 MB; SSE line and event sizes are bounded.
- XML content is parsed with `defusedxml`.
- Saved passwords use Windows Credential Manager, with DPAPI fallback.
- Automatic updates accept only this repository's expected installer asset and require its published SHA-256 digest to match before launch.
- The installer rejects broad system folders and unrelated non-empty folders so uninstall cannot remove an unmanaged directory.

Application settings are stored at:

```text
%APPDATA%\SouthlandServers\SLS_Mass_Notify\settings.json
```

## Audio

Bundled WAV files live in `audio`; the default is `Announcement.wav`. Custom WAV files must be smaller than 25 MB and are copied to:

```text
%APPDATA%\SouthlandServers\SLS_Mass_Notify\audio
```

## Updates

When enabled, the app checks published releases from `vipgabe09267/SouthlandServers_Mass_Notify_app`. Version ordering understands beta and release-candidate tags. Applying an update requires Windows administrator approval because the app is installed under Program Files.

## Build and test

Python 3.13 is used by the provided PowerShell scripts. Build dependencies are pinned in `requirements-build.txt`.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m unittest -v
```

Build the app and installer:

```powershell
.\build.ps1 -Clean
.\build-installer.ps1
```

Outputs:

```text
dist\SLS_Mass_Notify.exe
dist\SLS_Mass_Notify_Installer.exe
```

Optional local security checks:

```powershell
.\.venv\Scripts\python.exe -m pip_audit -r requirements-build.txt
.\.venv\Scripts\python.exe -m bandit -q -r sls_mass_notify.py sls_installer.py
```

## Uninstall

Use Windows **Settings > Apps > Installed apps**, or the uninstall shortcut under **Start Menu > Southland Servers Group**. Saved settings and credentials can be retained or removed.

## Operational note

Test PBX authentication, targeted delivery, all-desktop delivery, reconnect/resume, audio, and alert presentation in the deployment environment before relying on the app. This client complements PBX alerting and does not replace official emergency-warning systems.

## License

Licensed under the [GNU Affero General Public License v3.0](LICENSE).
