# Administration foundation

The `/admin` dashboard supplies account administration using existing APIs. Existing
employee login, password hashing and cookies are reused. No public bootstrap,
default administrator password, or registration role assignment exists.

## Private first administrator

On the server, with `KNOWVIA_DATA_DIR` pointing at the mounted private data root:

```text
python -m knowvia.admin --employee-id <id> --email <work-email> --first-name <name> --last-name <name>
```

Passwords are entered through hidden `getpass` prompts, not command arguments,
environment examples or generated files. The operation is one-time and atomic;
an existing identifier/email causes rollback rather than adopting an existing
account. Do not expose this command as an HTTP endpoint. Back up all databases
together. Do not execute it against a developer data directory to test it.

`authority.db` holds role assignments, a bootstrap marker and sanitized
administration events. Existing account/directory/session databases are reused.
Administrative changes use an attached-database SQLite transaction. Use local
durable storage and one web process/replica; network filesystems and distributed
administration are not supported. Keep SQLite rollback journals intact.

## API boundaries

- GET `/api/admin/me`: authenticated administrator check.
- GET `/api/admin/accounts`: authorized directory/account-status/trusted-role listing, without hashes or sessions.
- GET `/api/admin/roles`: allowlisted trusted roles and their permission catalog.
- GET `/api/admin/audit`: last 100 safe administrative events.
- POST `/api/admin/accounts`: provision directory record, account and trusted roles.
- POST `/api/admin/accounts/{employee_id}/activation`: `{"active": true/false}`.
- POST `/api/admin/accounts/{employee_id}/roles`: `{"roles": ["employee", ...]}`.

All mutations require an authenticated session, a matching `Origin` (scheme and
host) and JSON content type. Development identity overrides do not grant admin
access. A display/job `role` field is never authorization. Trusted assignment
names are allowlisted in `ROLE_PERMISSIONS`; dynamic permission-catalog editing
is deferred. Dashboard forms must send the exact allowed fields; extra privilege
fields are rejected. Administrative audit responses contain metadata only.

Deactivation updates account/directory state and revokes every current session
atomically. Reactivation does not resurrect revoked sessions. The last active
administrator cannot be demoted or disabled. New role resolution is performed on
every request, rather than embedded in cookies. Initial fictional demo seeding
does not overwrite records or role changes on later starts.
Existing directory records for the four previously trusted identities receive
their original permission mapping once, even with demo seeding disabled. This
migration creates no employee records and never assigns administrator authority.

Login attempts are bounded by both client and account digests, 10 attempts per
five minutes, with bounded in-memory storage. The limiter resets on service
restart and is not a distributed/edge abuse-control replacement. Configure
trusted reverse proxies correctly before public use. Filesystem/database owners
remain privileged; this layer does not defend against direct server-file edits.

## Evidence scope compatibility

Production evaluator factories require the extended nine-field contract.
Five-field historical records remain readable only through the legacy-compatible
parser/evaluator mode, with GENERAL scope and UNKNOWN eligibility. They cannot
verify eligibility. Invalid scoped outputs get at most the existing one repair;
they cannot use legacy permission recovery to bypass required scope. Existing
valid INSUFFICIENT decisions remain insufficient.

Live model quality remains unaccepted until authorized scoped regression tests
pass. Do not switch Ollama defaults or enable Groq globally based on offline tests.

## Dashboard

Open `/admin` and sign in through the existing session system. The public page
contains only a static login shell; every data read and mutation rechecks trusted
server authority. The employee workspace link returns to `/` without changing
the session. Employee chat assets are unchanged. Sign out revokes the current
session using the existing logout endpoint. No token is stored in browser storage.

Administrators can list employee records, provision new accounts, assign catalog
roles and activate/deactivate accounts. Existing directory-only records are marked
NOT_REGISTERED; their account lifecycle buttons are withheld until registration.
Provisioning never adopts or overwrites an existing record. Job titles do not
grant privileges. Confirmation dialogs precede role and activation changes;
server-side last-admin and session-revocation rules remain authoritative.

Administrative activity displays only the newest 100 sanitized administration
events, not unrestricted application audit logs or employee conversations. Responses
are marked no-store. User-controlled metadata is rendered using textContent,
not HTML. Mutation requests use same-origin JSON fetch; the browser supplies
Origin and the existing server check rejects missing or mismatched origins.

Knowledge uploads, password reset/invitation delivery, arbitrary permission-catalog
editing and an employee-side admin navigation button are not added in this milestone.
Privately distribute initial credentials through an appropriate channel; they are
never returned in account listings or administrative activity.
