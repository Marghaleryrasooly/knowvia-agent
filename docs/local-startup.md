# Local startup without changing existing accounts

Run commands from the repository root in PowerShell. The helper changes only
its child process environment; it never edits `.env`, initializes main accounts,
resets credentials, or stops another process.

## Existing main installation

```powershell
.\deploy\local.ps1 -CheckOnly
.\deploy\local.ps1
```

Workspace: `http://127.0.0.1:8000/`; dashboard: `http://127.0.0.1:8000/admin`.
The absolute storage path is printed. Selection follows the existing
`KNOWVIA_DATA_DIR` setting (project `.env`, or matching inherited setting),
otherwise repository `data`. Conflicting inherited project settings are rejected.
If the setting exists only in the terminal it remains authoritative: check the
printed path. An incomplete main installation is rejected, never initialized.

**While public deployment runs, use its existing HTTPS URL instead.** Main mode
refuses another recognized Knowvia process, occupied port, or any Tailscale
serving configuration. It never disables Funnel. Switching to local HTTP requires
a separately authorized manual public shutdown and removal of serving exposure.
Do not run `local_start.py` directly: the PowerShell wrapper performs the Windows
process and Funnel guards. Alternate/custom launchers are not coordinated by a
shared lock; do not start them concurrently.

Local mode uses loopback-only HTTP, non-Secure local cookies, no proxy trust,
normal authenticated authorization, disabled registration/identity overrides,
and mock actions. This does not change the HTTPS deployment's Secure cookies.
Provider/model configuration remains unchanged. Existing accounts and knowledge
are reused, so deliberate local admin edits affect that same main installation.

## Explicit disposable review

```powershell
.\deploy\local.ps1 -Mode review
```

Uses port 8002 and prints a new temporary `knowvia-review-*` folder each time.
No live accounts or uploaded knowledge are copied. Only the bundled fictional
reference corpus/index is used. No administrator is automatically created.
For authenticated review, in a **separate disposable PowerShell window**, set
`$env:KNOWVIA_DATA_DIR` to the exact printed review folder, then use the supported
private bootstrap (choose fictional identity values):

```powershell
.venv\Scripts\python.exe -B -m knowvia.admin --employee-id review-admin --email review-admin@novatech.example --first-name Review --last-name Administrator
```

Passwords are entered in hidden prompts. Never bootstrap against main storage
for testing. Close the bootstrap terminal afterward so its selection is not
inherited accidentally. Provision review employees through `/admin`.
Use a separate browser profile: cookies are not isolated by port.

Ctrl+C in the helper's terminal stops only that instance. Review folders remain
for inspection; nothing is automatically deleted or overwritten. A later review
start creates a fresh folder rather than resuming old review credentials.

## Public deployment

Continue using [the existing Funnel procedure](tailscale-demo.md) unchanged.
Do not restart it just to inspect these helpers. `-CheckOnly` performs no model
calls and does not initialize the web app or databases. Normal chat may call the
configured providers and consume quota.
