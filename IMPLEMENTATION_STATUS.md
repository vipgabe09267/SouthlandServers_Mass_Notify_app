# Implementation status - 1.1.6-beta

This document separates implemented code from capabilities that still require server work, product decisions, signing, or deployment validation. It does not claim that every item in the original audit or competitor feature list is complete.

No production deployment, live PBX notification, registry installation, or provider account change was performed. Changes remain local and uncommitted.

## Implemented desktop changes

| Area | Result | Main evidence |
| --- | --- | --- |
| Notification durability | SQLite WAL/FULL transaction commits inbox, deduplication receipt, and observed SSE ID together; history and receipt retries remain durable. | `sls_store.py`, delivery/reliability tests |
| Live-only delivery | Startup expires previous pending/displayed records; SSE reconnect uses the current server tail without Last-Event-ID; live polling fallback discards baseline/recovery snapshots and accepts only new publications. | `test_sls_live_delivery.py`, transport tests |
| Suspend/resume | Before presentation, a UI pause of at least five seconds expires the delivery queue and retires old sockets; workers reject delivery during the pause. | live-delivery tests |
| Receipt ACK | Durable outbox, matching-event confirmation, retry, rate pacing, same-origin HTTPS; no human-response or closure claim. | client reliability tests |
| Profile isolation | Deduplication uses normalized PBX origin plus desktop identity; changed identity resets cursor state. | client reliability tests |
| Credential rotation | New credential references staged before atomic settings replacement; old references retired only after success. Password whitespace preserved. | client reliability tests |
| Configuration safety | Legacy root settings migrate correctly; invalid/corrupt/future-schema settings are rejected without replacement. Writes use flush/fsync/replace. | client reliability tests |
| Transport supervision | Reconnect backoff/jitter, current-worker checks, fresh-heartbeat health, explicit authentication/revocation states; unexpected replay requests cause a fresh connection. | transport/reliability tests |
| SSE cursor | Authenticated baseline stored separately from notification receipt; explicit SSE ID must match notification ID. | client/backend tests |
| Expiry | Announcements expire at publication plus ten minutes or an earlier server deadline, including timeout zero; weather validity remains intact. | live-delivery/store/presentation tests |
| Schema handling | Bounded explicit validation; rejected payloads retained with a fault and bounded quarantine, allowing later valid messages and repaired same-ID payloads. | client reliability tests |
| Lifecycle defenses | Explicit incident IDs and positive revision ordering; stale controls and callbacks cannot revive or cancel newer records. | store/presenter tests |
| Test marking | Structured flags/status/severity, preserved through durable storage; message wording never decides test/live/lifecycle state. | backend/client/presenter tests |
| Instructions | Scrollable selectable full message, standard controls, optional image separate from text, DPI/work-area-aware sizing. | presentation tests and actual Tk smoke |
| Priority handling | Shared inbox/display priority rules; critical alerts displace lower-priority queued items without acknowledging or discarding their durable records. | store/presentation tests |
| Presentation capacity | Bounded queue/windows, guarded retirement of cancelled/superseded records, audio arbitration. | presentation tests |
| User controls | Tray status, Settings, History, Health, diagnostic export, local preview, reconnect, exit. | tray tests and actual Tk smoke |
| Sound | Validate PCM/mu-law structure, size, duration and alignment; duplicate imports do not overwrite; playback failure reported. | audio tests |
| Local control | Session-scoped authenticated loopback IPC, DPAPI-protected token, bounded payload/read time, action throttling. | IPC tests |
| Diagnostics | Rotating logs, credential redaction including quoted JSON, export excludes raw identifiers/messages/errors. | diagnostic tests |
| Retention | Terminal content pruned at startup/hourly; minimal deduplication records retained; expired delivery queues remain available in history. | store tests |
| Update safety | Strict version/channel selection, exact digest/size, redirect allowlist, incomplete-download cleanup, active-critical deferral. | update tests |
| Elevation boundary | Desktop never elevates a downloaded bundle; automatic installer execution fails closed. | update tests |
| Installer ownership | File manifest, path/case/traversal checks, no recursive removal of an arbitrary install directory, unrelated files preserved. | installer safety tests |
| Installer privilege | Self-contained .NET launcher requests UAC and stages embedded setup in protected Program Files; no elevated Python runtime from user-writable folders. | bootstrap package check, installer safety tests |
| Legacy upgrade | Recognized registered 1.0.8-Beta installations can migrate using checked paths, PE identities and shortcut targets; rollback preserves original files and unrelated content. | `test_sls_installer_legacy.py`, read-only installed-product detection |
| Installer transaction | Staging/backup, handled-failure rollback, graceful close followed by handle-verified exact-path termination, locked-file reboot exit code. | installer safety/process tests |
| Installer responsiveness | Install/uninstall run off Tk with queued progress/completion callbacks; closing/repeated clicks cannot interrupt or duplicate a transaction. | setup runtime tests, real hidden-Tk upgrade smoke |
| Deployment options | Actual silent operation, explicit failure codes, machine defaults, startup preference preservation. | installer safety tests |
| Supply chain | Exact runtime policy, hash-locked wheels, dependency advisory audit, isolated builds, single version source, inventories, pinned CI actions. | build scripts/tools |
| Release signing gate | Release builds require a real timestamped publisher signature and a catalog covering the complete bundle. | `RELEASE.md`, build scripts |

Routine notifications have one **Dismiss** action; persistent alerts retain the explicit read action. Color announcements display the server image without duplicate text or headings, with text retained in Details and as an image-failure fallback. A pinned footer keeps the action visible. The native announcement smoke checks image sizing, fallback, drill markers, dismissal and persistent-alert controls at normal/high scaling in restricted work areas. User actions remain local and are not sent as human safety responses.

The user confirmed two consecutive live announcements arrived with the corrected polling fallback. Both application receipt requests received successful PBX responses. The separate [v0.1.4 receipt reporting patch](BACKEND_RECEIPT_HANDOFF.md) connects those receipts to exact announcement jobs; it is prepared and locally tested but has not been deployed to the PBX.

## Backend changes prepared separately

Checkout: `../SouthlandServers_Mass_Notify_server_app_integration`

Branch: `work/desktop-app-integration`, based on published `main` commit `b91a37fe2c298b2f7d78d2876846943c47fb591c` (module 0.1.4-beta). Nothing was pushed or deployed.

- Explicit boolean `is_test` propagated by weather, announcement, saved-channel, and lightning test producers.
- Stable descriptive weather incident identifier derived from the CAP chain key, with `action: "notify"`; no fabricated revision counter or cancellation guarantee.
- SSE/JSON capabilities explicitly state supported receipt/replay/expiry behavior and unsupported human-response/pagination/incident lifecycle behavior.
- ACK response explicitly labels its semantics as application receipt.
- Synthetic fixtures generated by actual producer functions test the desktop consumer against real field shapes, including nullable weather fields.

See that checkout's `DESKTOP_APP_CONTRACT.md` for the exact contract and reconciliation notes for the other backend hardening work.

## Remaining release requirements and known limitations

| Item | Remaining work |
| --- | --- |
| Native runtime patches | The reviewed official Python 3.14.7 development runtime bundles OpenSSL 3.5.7. [OpenSSL 3.5.8](https://openssl-library.org/news/openssl-3.5-notes/) includes newer security fixes. Production release mode requires a reviewed runtime with OpenSSL 3.5.8 or newer; no arbitrary DLL substitution was performed. |
| Windows publisher identity | Provision the real Authenticode certificate/service and independent certificate pin, then create and verify a signed release. No backend Ed25519/GPG key was repurposed. |
| Protected automatic updater | Add or select an administrator-controlled deployment service that stages and verifies the entire signed catalog. Current client downloads for administrator review only. |
| Clean-machine validation | Exercise signed install/upgrade/rollback/uninstall on supported Windows images, alternate administrator elevation, RDP/shared sessions, application control and endpoint protection. |
| Installer power loss | Interrupted transactions preserve journal/backup and fail closed for administrator recovery; automatic recovery after arbitrary power loss is not implemented. |
| Legacy migration coverage | Only registered, protected 1.0.8-Beta installations and matching shortcut targets are supported. Other legacy layouts require review; filename presence never establishes ownership. |
| Multi-user removal | Machine uninstall retains each user's settings, secrets and existing per-user startup entries. A separate authorized user-context cleanup process is needed for fleet-wide removal. |
| Offline delivery policy | By explicit request, missed broadcasts are skipped. SSE replay remains disabled; live polling discards each initial/recovery snapshot and accepts only newly observed publications. |
| Immutable revisions | Published weather records may change under the same ID. A new immutable server journal/revision contract is needed for reliable corrections, cancel and all-clear. |
| Server delivery reporting | Published ACK storage holds the latest client receipt, not a per-event ledger. Fleet delivery/read/response reports require backend persistence and APIs. |
| Throttling | The published account/IP limits also count ACKs; many devices behind one NAT can still share a bottleneck. Coordinate rate-limit changes with backend hardening. |
| Linux backend validation | Focused PHP tests passed locally; broader announcement tests require Debian/POSIX because their real storage security checks reject Windows paths. |
| Physical operation | Audible output, focus/assistive-technology behavior, monitor arrangements, proxy/TLS policies, suspend/resume and long soak/load testing need deployment-environment validation. |
| Platform scope | This implementation targets Windows. macOS/Linux desktop agents and mobile clients are separate deliverables. |

## Platform and competitor feature work still pending

These are not represented by placeholder buttons or successful-looking mock APIs.

| Capability | Needed to implement it correctly |
| --- | --- |
| Device enrollment and inventory | Device identity, enrollment approval/revocation, registration API, administrative UI and migration from manually provisioned desktop accounts. |
| Central policy and branding | Signed/versioned policy, admin roles, policy precedence and device application status. |
| Panic activation | Explicit initiate-alert permission, allowed templates/locations, confirmation/cancel workflow, audit trail, replay/rate protection and activation API. |
| Human responses | Separate response endpoint, response options and retention, user/device identity, idempotency, reporting and escalation rules. |
| Incident lifecycle | Immutable revisions, stable incident identity, explicit cancel/all-clear events, incident ownership and close policy. |
| Escalation and accountability | Durable recipient-level delivery/response ledger, retries/fallbacks, overdue timers and operator views. |
| High availability/offline guarantees | Server journal replication, cursor pagination, backpressure, retention/capacity planning, failover and tested recovery objectives. |
| Organization sign-in | Selected SSO/directory provider, role mapping, session/device token policy and administrative integration. |
| SMS | Selected provider, recipient consent/opt-out policy, delivery callback verification, routing/quotas and billing ownership. |
| External calls | Approved provider/trunk and dialing scope, call progress/results, acknowledgement mechanism and abuse controls. Existing PBX paging is not evidence of external calling readiness. |
| Mobile push and geofencing | Target mobile platforms, push provider, managed app distribution, permissions/location policy and mobile clients. |
| Teams/Slack/Webex/Zoom | Selected destination/workspace and credentials, supported payload/action contract and callback authorization. |
| Signage, speakers and beacons | Selected hardware/protocols, inventory, delivery adapters and device health monitoring. |
| CAP/IPAWS or public-warning origination | Appropriate authorization, selected gateway and conformance/operational validation; weather consumption alone does not supply public origination. |
| Rich templates, schedules and approval workflows | Coordinate existing backend UI/templates/scheduler with new roles, versioning, approval and drill workflows rather than duplicating them in the desktop agent. |
| Multilingual/TTS delivery | Language policy and selected voices, server generation/delivery contracts, accessibility review and language-specific acceptance tests. |
| Fleet reporting and audit export | Server audit schema, retention/export controls, dashboard and evidence of display/response rather than transport receipt alone. |
| Managed kiosk/full-screen policies | Explicit administrator policy, accessible exit/ack controls, workstation roles and multi-monitor behavior; current client uses bounded alert windows. |

Provider names and permission rules have been requested; no secrets are needed in chat. The desktop's receiving credentials must not silently gain PBX administrative or panic-initiation authority.

## Validation evidence

Local validation on 2026-09-20: **256 tests passed** under CPython 3.14.7, including process identity/PID fencing, asynchronous setup callbacks, bounded app shutdown, legacy migration rollback and live-only delivery. Windows integration smokes passed with both a small fixture and the complete packaged application/setup runtime. Real harmless parent/child processes and a hidden setup window verify replacement while UI timers keep running, preserving an unrelated same-named process and user file. Both checks run automatically during installer builds. The shortcut checks now validate the host's actual shared Start Menu folder ACLs read-only and create/read native Windows shell links in the temporary installation. The directory helper consistently allows deletion-only shell rights while rejecting untrusted writes; application and staging directories retain their strict policy. Native layout checks passed at normal/high text scaling. An isolated end-to-end fallback test displayed a fresh publication in a real Tk alert and queued its receipt while skipping the old baseline. Build verification checks source bytecode, inventories and the embedded bootstrap package. Elevated installation/UAC completion has not been exercised automatically. The delivery investigation separately used read-only authenticated PBX probes; no announcements were published or acknowledged by those probes. See STREAM_DIAGNOSTICS.md.

Unsigned development artifact: `dist/packages/SLS_Mass_Notify_Installer.zip`. Its generated SHA-256 is in `dist/packages/SLS_Mass_Notify_Installer.zip.sha256`.

Both executables are intentionally unsigned. Production mode rejects the current OpenSSL patch level. No production-readiness claim follows from the passing tests.

Focused evidence includes:

- Client unit, integration and synthetic backend-contract tests under CPython 3.14.7 / OpenSSL 3.5.7.
- Actual Tk 9.0.4 application/settings/alert/history smoke test with temporary state, hidden windows, and external effects mocked.
- Native tray start/update/stop smoke test, without notifications or PBX access.
- Backend: nine producer/CLI tests; four focused PHP test scripts and changed-file lint.
- Dependency advisory scan, static analysis, and diff whitespace checks.
- Onedir application and self-contained installer packaging, with inventories/checksum; artifacts are **unsigned development builds**.

Live validation: the user confirmed receiving an announcement sent at 16:55:18 local time; the app recorded display and a successful server receipt response by 16:55:19. The backend dashboard still needs event-to-job receipt reconciliation; see BACKEND_RECEIPT_HANDOFF.md. Follow-up messages sent during the version handoff/initial stream timeout were skipped under the live-only policy; the updated client now remembers a failed stream route across restarts.
