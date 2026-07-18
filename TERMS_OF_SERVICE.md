# SouthlandServers Mass Notification App Terms of Service

By installing or using this app, you acknowledge that it is a desktop notification client that receives alert and announcement content from user-configured PBX connections using an authenticated live stream or an explicit legacy polling fallback.

You are responsible for configuring endpoints, tokens, recipient systems, and server-side alert data accurately. The app does not create weather alerts, verify emergency content, or replace official emergency alerting systems.

Use HTTPS PBX connections. HTTP endpoints may expose traffic to interception or modification. Certificate validation must remain enabled in production. Username/password and legacy bearer-token credentials should be kept private.

The app stores local settings under the current Windows user profile and stores saved tokens/passwords in Windows Credential Manager, with DPAPI as a compatibility fallback. The app may check GitHub Releases for updates if automatic updates are enabled during install or in Settings.

This software is provided under the GNU Affero General Public License v3.0 without warranty. Test deployments before operational use and comply with all applicable laws, policies, and emergency communication requirements.
