# SouthlandServers Mass Notification App

**Current version: V1.0.7-Beta**

[![Windows](https://img.shields.io/badge/platform-Windows-0A66C2)](#install)
[![Python](https://img.shields.io/badge/built%20with-Python-3776AB)](#build-from-source)
[![License: AGPL v3](https://img.shields.io/badge/license-AGPLv3-blue)](LICENSE)
[![Southland Servers](https://img.shields.io/badge/Southland%20Servers-Projects-111827)](https://southlandservers.xyz/projects)

SouthlandServers Mass Notification App is an open-source Windows desktop companion for SIP NOTIFY, emergency alert, PBX announcement, and EAS-style visual notification workflows.

It runs quietly in the background, starts with Windows if enabled, maintains authenticated live PBX streams, plays the selected WAV alert tone for new events, and displays clean desktop alert windows without requiring users to keep a browser open. Legacy JSON polling remains available as an explicit compatibility fallback.

[View Southland Servers Projects](https://southlandservers.xyz/projects)

![SouthlandServers Mass Notification App preview](SLS_Mass_Notif_App.png)

## What It Does

| Area | Behavior |
| --- | --- |
| Weather alerts | Shows NWS/SIP NOTIFY-style alert screens with title, priority, severity, area, effective time, and until/expires time. |
| Announcements | Shows a simplified safe-format notice with a hazard icon, title, and body only. |
| PBX profiles | Supports up to three independent PBX connections with normalized server addresses. |
| Live delivery | Uses an authenticated SSE handshake, plus deduplicated catch-up checks so PBX stream omissions cannot silently lose targeted messages. |
| Authentication | Uses desktop-specific HTTP Basic credentials for live mode; legacy modes remain available for migrated polling profiles. |
| Startup | Can register itself to run automatically when Windows starts. |
| Audio | Select bundled WAV tones from the `audio` folder, preview them, or import a custom WAV. |
| Updates | Optional automatic update checks from GitHub Releases on startup and about every 15 minutes. |
| Faults | Shows a desktop fault notification if an endpoint, token, or system issue remains unresolved for five minutes. |
| Uninstall | Adds a normal Windows uninstall entry and Start Menu uninstall shortcut. |

## Screens And Event Types

### Weather / NWS Alerts

Weather alerts use the structured weather fields returned by the API:

- `latest.title`
- `latest.priority`
- `latest.priority_label`
- `latest.severity`
- `latest.area`
- `latest.effective`
- `latest.expires`
- `latest.description`
- `latest.image_url`

Priority colors:

| Priority | Display Color |
| --- | --- |
| `critical` | Red |
| `urgent` | Orange |
| `advisory` / `notice` | Yellow |

### Mass Notify Announcements

Announcements use `latest.kind: "announcement"` and display only:

- hazard triangle/exclamation symbol
- title
- body text

The announcement window intentionally does not display endpoint internals, XML internals, IP addresses, recipients, or fake phone controls.

## Install

Download the latest V1.0.7-Beta installer from the [GitHub Releases page](https://github.com/vipgabe09267/SouthlandServers_Mass_Notify_app/releases):

```text
SLS_Mass_Notify_Installer.exe
```

Run the installer as Administrator. The installer will:

1. Install the app into:

   ```text
   C:\Program Files\Southland Servers Group\SLS Mass Notify
   ```

2. Add Start Menu shortcuts under:

   ```text
   Southland Servers Group
   ```

3. Add a Windows Installed Apps uninstall entry.
4. Require acceptance of the [Terms of Service](TERMS_OF_SERVICE.md).
5. Ask whether the app should run at Windows startup.
6. Ask whether automatic GitHub update checks should be enabled.
7. Launch the Settings window after install.

Windows SmartScreen may warn on unsigned builds. Code signing is recommended before broad public deployment.

## First Run Setup

When Settings opens, configure at least one PBX profile.

1. Enable the PBX profile you want to use.
2. Enter the HTTPS PBX hostname or origin URL.
3. Select `Live handshake (v0.0.7-beta or newer only)` for current PBX releases, or `Legacy polling fallback (v0.0.6-beta or older only)` for older PBXs.
4. For live handshake, enter the desktop-specific username and password configured in FreePBX. Authentication is fixed to this supported method.
5. Keep automatic reconnect enabled and certificate validation enabled.
6. For a legacy profile, choose username/password or bearer-token authentication and set that profile's polling interval.
7. Choose the alert audio tone or import your own `.wav`.
8. Click `Test connections`; live tests require the named `authenticated` SSE event.
9. Click `Save changes`.

The app can monitor up to three PBXs at the same time. Each profile has its own address, credentials, delivery mode, legacy polling interval, reconnect policy, and enabled state.

## How It Works

The background app gives each enabled PBX its own cancellable transport worker.

```mermaid
flowchart LR
    A[Windows Startup] --> B[SLS Mass Notify Background App]
    B --> C[Load Settings]
    C --> D[Open authenticated SSE stream]
    D --> E{Authenticated event received?}
    E -- No --> D
    E -- Yes --> F[Wait for notification event]
    F --> G{New event ID?}
    G -- No --> F
    G -- Yes --> H[Play selected WAV once]
    H --> I{kind}
    I -- alert --> J[Show Weather Alert Screen]
    I -- announcement --> K[Show Safe Announcement Notice]
    F --> L{Disconnect or bounded reconnect?}
    L --> D
    D --> M{Fault unresolved 5 min?}
    M -- Yes --> N[Desktop Fault Notification]
```

Live profiles send an authenticated streaming request:

```http
GET /api/sipnotify/desktop/stream HTTP/1.1
Host: example.com
Accept: text/event-stream
Authorization: Basic BASE64_USERNAME_PASSWORD
Last-Event-ID: MOST_RECENT_ACCEPTED_ID
```

Legacy-polling profiles may use either bearer-token or username/password authentication. Live delivery always requires the desktop-specific username/password pair and therefore does not show an authentication selector.

For username/password endpoints, the app sends HTTP Basic authentication:

```http
Authorization: Basic BASE64_USERNAME_PASSWORD
```

The app persists the most recent accepted event ID plus a bounded recent-ID history. Reconnects send `Last-Event-ID`; authenticated live sessions also perform a short catch-up check because some PBX streams acknowledge the handshake without pushing queued targeted announcements. Streaming, catch-up, and legacy polling paths all deduplicate before sound or display. Legacy records without an ID use a content fingerprint.

## Expected API Format

### Weather Alert Example

```json
{
  "ok": true,
  "latest": {
    "kind": "alert",
    "id": "urn:oid:...",
    "event": "Tornado Warning",
    "title": "TORNADO WARNING",
    "priority": "critical",
    "priority_label": "CRITICAL",
    "severity": "Extreme",
    "message_type": "Alert",
    "area": "Williamson County TX",
    "effective": "2026-06-21T08:27:32-05:00",
    "expires": "2026-06-21T09:12:32-05:00",
    "description": "The National Weather Service has issued...",
    "image_url": "https://example.com/nws_visual_push/alert_xxx.png",
    "xml": "<YealinkIPPhoneImageScreen ...>",
    "recipients": ["1000"],
    "created_at": "2026-06-21T08:27:32-05:00"
  },
  "events": []
}
```

### Announcement Example

```json
{
  "ok": true,
  "latest": {
    "kind": "announcement",
    "id": "announcement-20260621182234",
    "event": "Announcement",
    "title": "Announcement",
    "priority": "notice",
    "priority_label": "ADVISORY",
    "beep": "yes",
    "body": "Mass notify body verification test 2",
    "text": "Mass notify body verification test 2",
    "description": "Mass notify body verification test 2",
    "message": "Mass notify body verification test 2",
    "image_url": "",
    "xml": "<YealinkIPPhoneTextScreen Beep='yes'>...</YealinkIPPhoneTextScreen>",
    "recipients": [],
    "created_at": "2026-06-21T13:22:34.632063-05:00"
  },
  "events": []
}
```

## Custom Audio

V1.0.7-Beta keeps alert sounds in the `audio` folder. The default tone is:

```text
audio\Announcement.wav
```

Bundled WAV files are packaged with the app and copied into Program Files during install. Settings includes:

- a dropdown of available `.wav` files
- a `Play` button to preview the selected tone
- an `Import WAV` button for custom alert audio

Imported audio is copied to:

```text
%APPDATA%\SouthlandServers\SLS_Mass_Notify\audio
```

Only `.wav` files under 25 MB are accepted for custom imports.

## Security Notes

- Live PBX profiles require `https://`.
- Invalid-certificate and legacy HTTP-media switches are disabled by default and visibly marked unsafe.
- No-auth endpoints show a yellow caution because requests may not be fully authenticated.
- PBX passwords and legacy tokens are stored in Windows Credential Manager. DPAPI remains a migration/fallback path.
- Settings are stored under:

  ```text
  %APPDATA%\SouthlandServers\SLS_Mass_Notify\settings.json
  ```

- The exact XML payload remains available from the raw XML view, but the normal visible alert screens are driven by clean API fields.
- Server-side token storage at `/etc/nws_sipnotify_api.token`, 401 handling, Apache routing, and 256-bit token generation remain server responsibilities.

## Automatic Updates

Automatic updates are optional.

When enabled, the app checks this GitHub repository shortly after startup and about every 15 minutes while running:

```text
vipgabe09267/SouthlandServers_Mass_Notify_app
```

The updater watches GitHub Releases. When a newer published, non-draft release is available, the app downloads the release asset named:

```text
SLS_Mass_Notify_Installer.exe
```

Release tags are compared with the running app version, including beta/RC ordering, so an older install updates even if it has never recorded a previous release ID. The download must come from this repository and match the SHA-256 digest published by GitHub before it can run.

The installer is launched in update mode through the Windows elevation flow. Because the app installs into Program Files, Windows requests administrator approval before applying the update. Existing endpoint, audio, startup, and automatic-update preferences are preserved.

## Uninstall

Use either method:

```text
Start Menu > Southland Servers Group > Uninstall SouthlandServers Mass Notification App
```

or:

```text
Windows Settings > Apps > Installed Apps
```

The uninstaller removes startup entries, Start Menu shortcuts, installed app files, and optionally saved endpoint settings/tokens.

## Build From Source

Install build requirements:

```powershell
py -m pip install -r requirements-build.txt
```

Build the standalone background app:

```powershell
.\build.ps1 -Clean
```

Output:

```text
dist\SLS_Mass_Notify.exe
```

Build the installable setup app:

```powershell
.\build-installer.ps1 -Clean
```

Output:

```text
dist\SLS_Mass_Notify_Installer.exe
```

## Release Checklist

Before publishing a release:

1. Confirm `APP_VERSION` is still `1.0.7-Beta` for this V1.0.7-Beta release.
2. Rebuild with `.\build-installer.ps1 -Clean`.
3. Test install, Terms acceptance, settings save, live authenticated handshake, targeted and all-desktop delivery, reconnect/resume, explicit JSON fallback, supplied colors, TEST ONLY banner, audio, uninstall, and update preference.
4. Attach `dist\SLS_Mass_Notify_Installer.exe` to the GitHub Release.
5. Code sign the app and installer when a signing certificate is available.

## Project Status

V1.0.7-Beta includes production transport and security hardening. Broad emergency-use deployment still requires environment-specific PBX integration testing and signed release artifacts.

Recommended hardening before broad public production rollout:

- code signing and installer signing
- CI-based release builds
- automated endpoint integration tests
- crash reporting or stronger log rotation
- signed update verification

## License

This project is open source under the [GNU Affero General Public License v3.0](LICENSE).

Contributions, forks, audits, and integrations are welcome under the same copyleft license terms.
