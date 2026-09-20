# Server v0.1.4 desktop receipt fix

The desktop receives consecutive announcements and successfully posts their receipt ACKs. The released server saves only the last receipt per desktop, while announcement jobs retain a static `published` row with no journal event ID. The dashboard also stops refreshing once submission completes.

The implementation is in [backend-fixes/server-v0.1.4-desktop-receipts.patch](backend-fixes/server-v0.1.4-desktop-receipts.patch). It is based on published server commit `b91a37fe2c298b2f7d78d2876846943c47fb591c` (module `0.1.4-beta`). No changes were made to the other session's backend checkout. This patch has **not been deployed** to either PBX.

## Implemented changes

- API-only announcement publication prints the exact persisted event ID as structured output. The job records that ID with each desktop destination.
- The existing authenticated ACK route still checks the event's desktop routing before writing a receipt. It returns HTTP 503 with Retry-After if receipt storage fails, allowing the desktop outbox to retry.
- An atomic receipt ledger stores event-and-username hashes and timestamps. Duplicate ACKs preserve the first timestamp; later announcements do not replace earlier receipts. Storage uses a separate lock, checked file identities, restricted permissions and atomic replacement. PHP versions with `fsync` also flush the file and Linux directory.
- Job status reconciles the final result rows against those exact receipt keys, including ACKs arriving before the job finishes. Confirmed rows become `received` with a timestamp. This confirms application receipt, not a person's read action.
- The dashboard continues checking receipts for up to ten minutes after creation, without blocking another announcement. Manual refresh remains available. Responses from an older job cannot overwrite a newly submitted job's view.
- Runtime verification includes the new API helper; the release build includes the PHP and Python receipt regressions.

The ledger is under `/var/lib/asterisk/SLS_Mass_Notifications_Plugin/desktop-receipts`, bounded to 20,000 receipts and seven days. It stores no message bodies or credentials. Existing jobs without an exact event ID cannot be retroactively matched reliably. Test with new announcements after deployment.

## Apply in the backend session

The patch is a source change, not an official signed server release or a FreePBX module upload. In a clean checkout based on the commit above:

```bash
git apply --check /path/to/server-v0.1.4-desktop-receipts.patch
git apply /path/to/server-v0.1.4-desktop-receipts.patch
php tools/test_desktop_receipts.php
python3 tools/test_desktop_receipt_contract.py
php tools/test_desktop_reliability.php
php tools/test_desktop_announcement_expiry.php
node tools/test_desktop_receipt_ui.js
```

If incorporating this into the other session's newer server version, review conflicts and retain that version's other changes. Run its normal Linux release checks and signing/package process before deployment. Deploy the module, synchronized desktop API directory (including `desktop_receipts.php`), and runtime `sls_notify.py` together using the server's normal installation process. Do not deploy only the dashboard or only the ACK endpoint.

After deployment, send two fresh announcements to the connected desktop and check that both exact jobs show `received`. Selecting the popup's read button is not required for an application receipt. Older `published` rows without IDs remain historical publication records.

## Validation performed here

- 254 desktop unit tests passed; native notification, layout and full installer upgrade smokes passed. App and installer were rebuilt as `1.1.6-beta` and their embedded payloads verified.
- PHP receipt-store tests: multiple events, duplicate ACKs, user separation, absent/expired evidence, bounded size, lock contention, corrupt storage and failed writes.
- Five Python/PHP contract tests: exact producer ID after successful publication only; authorized ACK; unauthorized/unknown IDs; retryable storage failure; final job-result reconciliation and polling expiry.
- Node tests of the actual dashboard polling functions: post-completion refresh, confirmed receipt, second send availability, late-response isolation, manual refresh and ten-minute limit.
- Existing PHP desktop routing and announcement expiry checks passed. Changed PHP files pass syntax checks.

These tests ran on Windows with isolated fixtures. Linux ownership/symlink cases and the complete server release suite still need to run in the backend's Linux build environment. No live PBX server files or administration settings were changed from this session.
