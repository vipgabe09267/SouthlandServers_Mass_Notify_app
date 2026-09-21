# Changelog

## 1.0.9-beta - 2026-09-21

Changes since 1.0.8-beta:

- Fixed successive announcements failing to arrive when live streaming stalls. Added polling fallback and automatic receipt retries.
- Skipped announcements missed while asleep or disconnected. Queued and visible announcements now expire within ten minutes of publication.
- Fixed blank colored announcements and duplicate plain text. Kept Dismiss visible, resized images to fit, and restored the SLS window icon.
- Fixed Settings and installer windows extending below the taskbar, including at larger text sizes.
- Replaced setup with one self-contained installer EXE. Fixed ownership and Start Menu permission errors, unresponsive installation, and upgrades blocked by lingering app processes.
- Added local history, connection health, diagnostic export, and separate connection-test and local-preview controls.
- Improved credential storage, media validation, upgrade rollback, and update download verification. Update downloads support installer EXEs and are not automatically run.

This is an unsigned beta. The server's pending-receipt display requires a separate backend fix. See the [README](README.md) for setup and remaining limitations.
