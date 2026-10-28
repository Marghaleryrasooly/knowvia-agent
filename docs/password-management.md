# Invitations and password management

Administrator-provisioned identities and trusted roles remain authoritative.
Public self-registration stays disabled. Existing initial-password accounts and
the private administrator recovery command remain available; nothing is migrated
or verified merely by importing the application.

## Initial deployment — separate approval required

1. Review/integrate the isolated change and take a consistent private backup.
2. During an approved maintenance window, stop the backend and initialize the
   additive schema against its **existing absolute** data directory:

   ```powershell
   .venv\Scripts\python.exe -B -m knowvia.password_management --data-dir "<absolute-existing-data-directory>" --initialize --maintenance-confirmed
   ```

   This creates `credential_state` and `credential_tokens` in `authority.db` only.
   Existing identities, roles, passwords, sessions and documents are unchanged.
   Running it again is safe. Missing databases and unsupported journal modes are
   rejected. Attached-database transactions require local durable SQLite storage
   and supported rollback journals; do not switch journal modes casually.
3. Configure mail privately using the variables below. Do not copy credentials
   into Git, tests, terminal arguments, screenshots or logs.
4. Restart only after separate authorization, then perform a controlled real
   delivery test. No real delivery or production initialization was performed
   during implementation.

## Email configuration

`KNOWVIA_MAIL_TRANSPORT=disabled` is the default. No local file captures real
emails or reset tokens. Offline tests inject a memory-only fake transport.

To opt into SMTP, set `KNOWVIA_MAIL_TRANSPORT=smtp`,
`KNOWVIA_PUBLIC_BASE_URL` to the exact public HTTPS origin (no path/query),
`KNOWVIA_MAIL_FROM`, `KNOWVIA_SMTP_HOST`, `KNOWVIA_SMTP_PORT`,
`KNOWVIA_SMTP_USERNAME` and `KNOWVIA_SMTP_PASSWORD`. The callback hostname must
appear explicitly in `KNOWVIA_ALLOWED_HOSTS`. Port 465 uses TLS immediately;
587 requires STARTTLS. Certificate verification cannot be disabled.

Verify sender ownership/domain with the selected SMTP provider before setting
`KNOWVIA_MAIL_SENDER_VERIFIED=true`. This flag is an operator acknowledgement,
not automatic DNS verification. For a custom domain, complete the provider's
SPF/DKIM requirements and review DMARC. Use a free SMTP allowance if available;
Knowvia does not enable billing or select a paid service.

Gmail/Outlook **recipients** need no special integration. A possible low-volume
sender is an eligible Gmail account using `smtp.gmail.com`, TLS 465/587 and an
app password; Google requires 2-Step Verification and may restrict app-password
availability. Do not use the regular Google password. This adapter does not
implement OAuth-only SMTP providers. See Google's
[app-password requirements](https://support.google.com/mail/answer/185833?hl=en)
and [SMTP documentation](https://developers.google.com/workspace/gmail/imap/imap-smtp).
Account eligibility, free quotas, outbound-port availability and deliverability
must be checked before activation; none is guaranteed by the application.

## Employee workflows

- **Invite:** choose Email invitation under Add employee and assign trusted
  employee roles. No password is emailed. The link expires after 24 hours; its
  successful use verifies mailbox ownership and sets the initial password.
  Invitation-based creation of administrators is intentionally unavailable.
- **Delivery failure:** account creation and delivery are distinct. A saved
  pending account remains visible; use Resend invitation rather than duplicate
  provisioning. Resend invalidates earlier links. Uncertain/failed delivery
  invalidates that link and is never automatically retried.
- **Existing accounts:** Password & security at `/account` supports Change
  Password after checking the current password. All sessions are revoked and a
  fresh sign-in is required. Existing emails start **unverified**. Use Verify
  recovery email with the current password and confirm the emailed link before
  relying on Forgot Password.
- **Forgot Password:** `/password` always returns a generic response. Only an
  active, fully onboarded account with a verified, matching recovery address can
  receive a reset link. Reset/verification links expire after 30 minutes. If an
  existing user has already lost their password before verifying email, this
  self-service flow intentionally cannot recover it; the existing private admin
  recovery remains available for administrators, not arbitrary employee resets.

## Security and operational limits

Tokens use 256 bits of randomness and are stored as SHA-256 digests only. They
are bound to purpose, account, current email and credential version. Links use
URL fragments, removed from browser history immediately; tokens are not placed
in queries, browser storage or API responses. GET requests never redeem links.
All mutations require same-origin JSON, including public redemption. Passwords
use the existing 12–128-character policy and Argon2id hashing.

Redemption, credential changes, token invalidation, session revocation and audit
events are transactional. Login rechecks the verified credential under the same
transaction that inserts its session, preventing an old-password login from
issuing a live session after a completed reset. This guard covers the existing
SQLite repository; any future persistence adapter must implement equivalent
atomic issuance. Previously authorized in-flight requests may finish normally.
Deactivation invalidates outstanding links. New roles are never taken from links.

Five attempts per five minutes are bounded by client and email/account/token
digests. Limits are process-local, reset on restart and do not replace an edge
abuse-control service. SMTP has a ten-second socket timeout and no retries.
Reset delivery runs after the generic response; there is no durable mail queue.
A process interruption can lose delivery, requiring a new user request or an
admin resend. Sanitized audit events record operations, not passwords or links.
Use one worker and monitor provider quotas without enabling automatic upgrades.

No external email provider has been accepted or enabled by the offline tests.

## Approved rollout and rollback checklist

Do not integrate into a running checkout: static HTML/JavaScript is read from
disk and would expose new controls before the loaded Python backend has new
routes. A separate integration/maintenance approval is required.

1. First exercise the committed version with disposable databases and the
   injected fake mail transport, including invitation, verification, reset,
   password change, replay rejection and unrelated-session preservation.
2. Record the current commit, data directory and launcher settings. During the
   approved window, withdraw public forwarding, drain requests and stop only
   the identified backend. Confirm port 8000 has no listener.
3. Make SQLite online-backup-API copies of **all seven** application databases
   into a new private directory outside Git. Include published knowledge,
   document/index versions and other persistent supporting files. Check each
   backup with `PRAGMA integrity_check`, verify completeness, and retain the
   pre-rollout commit and private configuration. Never overwrite a good backup.
4. With a clean main checkout, incorporate only the reviewed Phase 3 commit
   using `git cherry-pick <phase-3-commit>`. Do not merge unrelated work.
5. Run the explicit schema-initialization command above against the verified
   main data directory. Existing accounts and roles are retained; existing
   recovery addresses remain unverified. Leave mail disabled initially.
6. Start using the existing [Funnel procedure](tailscale-demo.md). Verify login,
   `/admin`, account/role listings, knowledge/version availability and safe
   agent previews/cancellation. Any live inference requires its own approval;
   never execute real external ticket actions for this rollout.
7. Only after sender ownership, TLS, quotas and the exact callback/trusted host
   are verified, separately approve SMTP activation and a controlled delivery
   to an owner-controlled mailbox. Prove invitation and reset single-use
   behavior, then allow intended recipients. Legacy users must sign in, enter
   their current password and follow the verification email before recovery is
   enabled. An operator flag never verifies a recipient's mailbox.

On failure, stop accepting writes before rollback. Prefer reverting the code
with mail disabled while **retaining the updated databases**: the additive
tables do not change old required schemas, and prior code ignores them. Keep
newly changed password hashes and session revocations; do not drop tables or
reactivate sessions. New invitation/reset pages will be unavailable on old
code; outstanding links must be invalidated through an explicitly approved
maintenance operation before later re-enablement. Pending invitees remain
unable to sign in until supported onboarding is restored.

Restore a full pre-rollout backup only if database recovery is necessary and
separately authorized. It discards post-backup changes and can resurrect old
passwords and sessions; never restore individual attached databases piecemeal
or overwrite a live database. Revoke restored sessions and review recovery
state before making the restored site public.
