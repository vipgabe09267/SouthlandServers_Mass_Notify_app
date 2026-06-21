# SouthlandServers Mass Notification App

Current version: 1.0.1

SouthlandServers Mass Notification App is an open-source Windows desktop client for SIP NOTIFY/EAS-style alert delivery. It runs quietly in the background, polls one or more HTTPS alert endpoints, displays weather alerts in a Yealink T48G-style screen preview, and displays mass announcements in a simplified safe-format notice.

The app is designed for dispatch, weather alerting, emergency notification demos, PBX visual alert workflows, and other environments where a lightweight Windows companion app needs to mirror alert content being pushed to SIP phones.

## Project Status

This app is functional and suitable for controlled testing, demos, and pilot deployments. Before calling it fully production-ready, recommended hardening includes code signing, installer signing, CI builds, automated endpoint tests, crash reporting/log rotation, and a real release/update process.

## Features

- Native Windows `.exe` built with Python and PyInstaller.
- Runs in the background and starts automatically with Windows.
- Adds Start Menu launch/uninstall shortcuts and a Windows Installed Apps uninstall entry.
- Supports up to three independent HTTPS alert endpoints.
- Each endpoint can use its own bearer token/key, or run in `No token` mode.
- Stores local tokens encrypted with Windows DPAPI.
- Optional automatic updates from GitHub, checked once every 24 hours.
- Polls every 10-30 seconds by default, configurable in settings.
- Tracks each endpoint's latest alert ID to avoid duplicate notifications.
- Displays new alerts in an 800x480 Yealink T48G-style screen window.
- Displays `kind: announcement` events as a simplified safe-format notice with a hazard icon, title, and body.
- Shows API-provided alert title, priority/severity, area, effective time, and until/expires time on the alert screen.
- Maps `critical` alerts to red, `urgent` alerts to orange, and `advisory`/`notice` alerts to yellow.
- Shows an API-provided alert image payload full-screen when available.
- Plays the bundled EAS tone once for each new alert.
- Reports unresolved endpoint/token/system faults after five minutes.
- Includes an uninstall flow that removes startup entries, shortcuts, app files, and saved settings.

## How It Works

The app polls configured endpoints with a normal HTTPS `GET` request. When a token/key is configured, it sends:

```text
Authorization: Bearer <token>
```

When `No token` is checked for an endpoint, the app sends no `Authorization` header for that endpoint.

Configured API endpoints must use `https://`; `http://` is accepted only for `localhost`/loopback development testing.

Expected API shape:

```json
{
  "ok": true,
  "latest": {
    "id": "desktop-api-verification-...",
    "event": "Severe Thunderstorm Warning",
    "title": "SVR TSTORM WARNING",
    "priority": "urgent",
    "priority_label": "URGENT",
    "severity": "Severe",
    "area": "Williamson County TX",
    "description": "...",
    "image_url": "https://example.com/nws_visual_push/alert.png",
    "xml": "<YealinkIPPhoneImageScreen ...>",
    "created_at": "..."
  },
  "events": []
}
```

For each endpoint, the app stores the last seen `latest.id`. A notification appears only when that ID changes. If no ID exists, the app falls back to a content fingerprint.

Announcement payloads use `latest.kind`, `latest.title`, `latest.priority`, `latest.priority_label`, `latest.body` or `latest.description`, `latest.image_url`, and `latest.created_at`. Weather alerts continue to use the weather fields such as `latest.severity`, `latest.area`, `latest.effective`, and `latest.expires`.

## Automatic Updates

Automatic updates are optional. When enabled, the app checks the GitHub repository once every 24 hours:

```text
vipgabe09267/SouthlandServers_Mass_Notify_app
```

On first check, the app records the current latest GitHub Release as its baseline. On later checks, if a newer non-draft release exists, the app downloads the `SLS_Mass_Notify_Installer.exe` release asset and runs it in update mode. Windows may request administrator approval because replacing files in Program Files requires elevation.

For update clients to install a new release, that release must include a rebuilt `SLS_Mass_Notify_Installer.exe` asset. Prereleases are eligible as long as they are published and not drafts.

## Build

Build the standalone background app:

```powershell
.\build.ps1 -Clean
```

The finished app is created at:

```text
dist\SLS_Mass_Notify.exe
```

Build the installable setup app:

```powershell
.\build-installer.ps1 -Clean
```

The finished installer is created at:

```text
dist\SLS_Mass_Notify_Installer.exe
```

## Behavior

- The installer requests Administrator permission and installs to `%ProgramFiles%\Southland Servers Group\SLS Mass Notify`.
- The installer can register the app under the current user's Windows startup registry key.
- The installer shows each install step and opens the Settings window after install.
- The installer lets the user opt out of automatic GitHub update checks.
- It adds Start Menu shortcuts under `Southland Servers Group` for launching and uninstalling.
- It adds a Windows Installed Apps uninstall entry.
- First run asks for endpoint settings. Up to three endpoints can be configured.
- Each endpoint can use its own token/key, or can be marked `No token` to call the URL directly.
- The token is stored in `%APPDATA%\SouthlandServers\SLS_Mass_Notify\settings.json` encrypted with Windows DPAPI.
- Opening the app again while it is running brings up settings.
- When a new alert fingerprint is seen, it plays `eas_tone.wav` once and shows the appropriate alert or announcement preview.
- The alert preview is non-modal, drops out of topmost mode, auto-hides, and lowers behind your active window when focus leaves it.
- If the same endpoint/token/system fault remains unresolved for 5 minutes, the app shows a small desktop fault notification.
- The preview resolves missing-host image URLs such as `http:///nws_visual_push/...` against the configured endpoint host and displays the returned image on the simulated T48G screen when possible.

## Uninstall

Use either:

```text
Start Menu > Southland Servers Group > Uninstall SouthlandServers Mass Notification App
```

or Windows Settings > Apps > Installed Apps. Uninstall shows a confirmation/progress window, then removes the startup entry, Start Menu shortcuts, installed app folder, and saved settings if selected.

## Endpoint Details

The endpoint can return JSON, XML, or plain text. For the SIP NOTIFY API shape, it reads `latest.id`, `latest.kind`, `latest.title`, `latest.event`, `latest.severity`, `latest.priority`, `latest.priority_label`, `latest.body`, `latest.image_url`, `latest.xml`, `latest.description`, `latest.area`, `latest.effective`, `latest.expires`, `latest.created_at`, and `events`. It stores the last seen `latest.id` and only shows a new notification when that ID changes. Visible screens are driven by clean API fields; announcement screens show only the hazard icon, title, and body, while exact XML remains available from the raw XML view.

Server-side token storage at `/etc/nws_sipnotify_api.token`, 401 handling, Apache routing, and 256-bit token generation remain server responsibilities. This Windows client keeps its local copy private with DPAPI.

## Open Source

This project is open source under the GNU General Public License v3.0. Contributions, forks, audits, and integrations are welcome under the same copyleft license terms.
