# Desktop delivery investigation — 2026-09-20

Observed on the configured primary PBX:

- The announcement was present in the authenticated journal and targeted the configured desktop account.
- The desktop logged successful SSE authentication followed by a 60-second heartbeat timeout.
- Read-only stream probes returned HTTP 429, `stream_capacity_reached`, with `Retry-After: 15`. This is a stream-slot limit, not evidence that the credentials are wrong.
- The authenticated JSON endpoint returned the journal and a valid HTTP Date header successfully.
- A bounded read-only stream on the second configured PBX delivered a keepalive after about 15 seconds and the requested reconnect event after about 22 seconds.

No announcement was published, acknowledged, or replayed during these probes.

## Desktop change

Version 1.1.6-beta falls back to five-second authenticated snapshot checks after a stalled authenticated stream or stream throttling. It honors Retry-After before switching. The initial snapshot is a discard baseline. Only new IDs/revisions with publication times in the subsequent observation interval are eligible. A network failure, long observation gap, restart, or suspend establishes a new baseline and skips missed records. The server's HTTP Date supplies the observation clock. Existing schema, recipient, deduplication, expiry, display and receipt checks still apply.

Fallback is remembered per profile for 24 hours, including reconnects and restarts, avoiding repeated consumption of server stream slots. Changing the profile identity clears the hint. Health/status identifies live polling. The connection test reuses the current worker, requires actual delivery activity, and explicitly distinguishes connection verification from Local preview and a server-sent announcement.

## Backend follow-up

The primary server/proxy stream problem still needs operational investigation. Check concurrent connections for this desktop account, slot release after disconnect, and buffering/compression on the actual HTTPS route. Verify that **every** notification and keepalive reaches the client promptly, not just the initial padded authenticated event. Compare the primary route with the working second route. Do not increase connection limits or lengthen client timeouts as a substitute for observing live delivery. No backend deployment was performed as part of this desktop fix.
