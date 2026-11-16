# Private administrator recovery

This operator-only command resets one **existing active trusted administrator**.
It is not employee Forgot Password, initial bootstrap, or an HTTP endpoint.
Local filesystem access is its trust boundary. Do not expose it through a web
route, automation service, or an untrusted terminal.

Before real execution, obtain explicit owner approval for credential changes and
a brief maintenance window. Drain requests and stop the backend using its normal
approved procedure. The command does not stop services or enforce maintenance:
`--maintenance-confirmed` is an operator acknowledgement. Concurrent old-password
login can otherwise issue a session after revocation. Do not run a second server
against the same data. Keep the current public HTTPS security configuration.

Take a consistent private backup of the data directory while stopped; protect
backups because they contain password hashes and sessions. Do not place them in Git.

In a private interactive terminal, use the existing absolute root and exact stored
email. Replace `<EXISTING_ABSOLUTE_DATA_DIRECTORY>` with the already verified
private data directory; do not create a new directory for recovery. This example
is **for separately authorized future execution only**:

```powershell
.venv\Scripts\python.exe -B -m knowvia.admin_recovery --data-dir "<EXISTING_ABSOLUTE_DATA_DIRECTORY>" --email admin@novatech.example --acknowledge-local-recovery --maintenance-confirmed
```

The command displays the target and resolved directory. Type both exactly when
asked, then enter the new password twice in hidden prompts (12–128 characters).
No password argument/environment setting is supported. Visible-input fallback
is an error. Store the new password in your private password manager.

The command does not load `.env`, create schemas/accounts, promote roles or alter
activation. It validates existing databases and rechecks the target inside the
write transaction. Password update, target-session revocation and sanitized audit
record commit together, or roll back together. Cross-file atomic recovery requires
supported rollback-journal mode on local durable storage; WAL is rejected rather
than changed. Do not use network filesystems. Other data remains untouched.

After success, restart through the existing approved public launcher, sign in on
HTTPS with the new password and verify administration. Old sessions require a
new login. On failure, stop and investigate; do not edit SQL manually, rerun
bootstrap, or blindly retry after an uncertain process interruption. Restoring a
backup can resurrect old credentials/sessions, so it also requires explicit review.
