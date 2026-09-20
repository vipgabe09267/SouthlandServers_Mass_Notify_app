# Changelog

## 1.1.6-beta — 2026-09-20

Changes since the published 1.0.8-beta desktop release.

### Notification delivery

- Added authenticated polling fallback when the live stream stalls or reaches its connection limit. The app remembers the fallback for 24 hours so reconnecting does not repeat the initial stream timeout.
- Fixed delivery of successive announcements on affected PBX connections.
- Stopped replaying announcements missed while the computer was asleep, shut down, or disconnected. Restarting or reconnecting establishes a new delivery baseline.
- Limited queued and visible announcements to ten minutes after publication. Earlier server expiry still applies.
- Added durable local receipt retries, duplicate detection, local notification history, and connection health details.
- Updated the connection test to check actual delivery activity. The test does not send an announcement; Local preview checks presentation separately.

### Announcement display

- Color announcements display the server's design without a duplicate white text box or repeated heading.
- Fixed blank color announcements when an image finishes loading after the dark-themed window has opened. Text stays available until the image renders and remains the fallback if rendering fails.
- Added one visible Dismiss button for routine notifications. Persistent alerts retain an explicit read action.
- Kept controls within the monitor's usable area, resized images to fit, and improved Settings and installer layouts on smaller screens and at larger text sizes.
- Applied the SLS icon explicitly to app windows instead of relying on Tk's default icon.

### Installation and maintenance

- Replaced the installer with one self-contained EXE that includes the app and its runtime.
- Fixed installation failures involving untrusted ownership and shared Start Menu permissions while retaining checks against untrusted content writes.
- Moved installation work off the UI thread and improved shutdown of verified app processes during upgrades, including stuck background instances.
- Added installation ownership manifests, rollback for handled failures, protected staging, and migration checks for registered 1.0.8-Beta installations.
- Organized local build output around one installer EXE and one runnable app folder. The GitHub release attaches only the installer EXE.
- Improved credential handling and added bounded media handling, redacted diagnostics, pinned build dependencies, and checks for update download size and digest. The updater recognizes installer EXE releases and retains ZIP compatibility. Downloads are not automatically executed.

### Validation and known limitations

- 256 unit tests pass. Native checks cover the actual packaged app's delayed image rendering and window icons, layout at enlarged text sizes, live-only delivery, and full installer payload replacement in an isolated temporary installation.
- This is an unsigned beta. Publisher signing, the production OpenSSL runtime update, and clean-machine deployment acceptance testing remain outstanding; see [release requirements](RELEASE.md).
- Server v0.1.4-beta can continue showing `published — Awaiting optional client acknowledgement` after a successful desktop receipt. The [separate server patch](BACKEND_RECEIPT_HANDOFF.md) has not been deployed by this desktop release.
- Device receipts do not mean a person read the announcement. Dismiss/read actions remain local. Enrollment, panic activation, human responses, and reliable incident cancellation/all-clear require coordinated backend APIs.
