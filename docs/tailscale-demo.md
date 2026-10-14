# Persistent Windows HTTPS demonstration

The public deployment was activated and tested on October 14, 2026 at
[https://desktop-md6j59m.taild8d8ea.ts.net/](https://desktop-md6j59m.taild8d8ea.ts.net/).
The project owner verified the website and real Groq-backed responses through HTTPS.
This records successful testing, not a continuous-availability guarantee.

The Windows host runs both Knowvia and Tailscale Funnel. The computer must stay
awake and connected, and both processes must remain running. Local SQLite state,
uploaded knowledge and publication versions remain on persistent private disk.
Public self-registration is intentionally disabled; administrators provision accounts.

The launcher changes only its child-process configuration; the real `.env` is
unchanged. Groq is the default inference provider. `--groq-accepted` acknowledges
the completed acceptance review; it is not itself a model-quality test.

## Host setup and historical investigation

Tailscale 1.102.4 is installed at `C:\Program Files\Tailscale\tailscale.exe`.
The configured hostname is `desktop-md6j59m.taild8d8ea.ts.net`.
The initial preparation inspection found no active Funnel configuration or local
health response; those observations predate successful activation and do not
describe current availability. Do not read/share Tailscale state files: they can contain secrets.

The saved original local acceptance artifact used Gemini routing, Ollama evidence
and answers, local MiniLM and disabled structured output. Its natural VPN case
failed strict JSON parsing followed by schema-placeholder repair; lost-device
output failed missing-fact consistency on both attempts. Vacation succeeded.
These are application/evidence failures documented locally, not evidence that
Funnel broke model connectivity. The artifact cannot prove the exact server
configuration or transport outcome of an earlier public browser request.
The old forgotten-password answer was unsafe; current evidence protections and
Groq acceptance must not be bypassed to reproduce that historical result.

## Configuration and persistence

`deploy/funnel_start.py` explicitly selects Groq GPT-OSS 120B for BOTH inference
providers and Gemini routing. It preserves existing Groq settings, Gemini routing
model, embedding provider and data-root configuration. No Ollama fallback.
It enforces demo/mock-only ticketing, Secure cookies, HSTS, exact hosts, no debug,
no developer identity override, no public registration or directory reseeding.
No account, role, document or conversation is deleted or reset.

The entire existing `KNOWVIA_DATA_DIR` must stay on this laptop's local persistent
disk (default `D:\marghalery\knowvia-agent\data`). It includes accounts, sessions,
authority/roles/admin audit/drafts, directory, conversations, proposals, tickets,
agent audit, runtime index and `knowledge/versions/` manifests/documents/indexes.
Use the same root when privately bootstrapping an administrator. Existing local
admin bootstrap works here; unlike serverless/free ephemeral hosting it needs no
new bootstrap mechanism. Keep consistent private backups while the application
is stopped; never copy databases or private uploads into Git or a public directory.

## Exact commands

Run from PowerShell in the project root. Read-only status may require an elevated
terminal due to Windows Tailscale daemon permissions:

```powershell
Set-Location D:\marghalery\knowvia-agent
& 'C:\Program Files\Tailscale\tailscale.exe' funnel status
& 'C:\Program Files\Tailscale\tailscale.exe' serve status
.venv\Scripts\python.exe -B deploy\funnel_start.py --hostname desktop-md6j59m.taild8d8ea.ts.net --check-only
```

For a future authorized start (do not start a duplicate server while it is running), terminal A:

```powershell
.venv\Scripts\python.exe -B deploy\funnel_start.py --hostname desktop-md6j59m.taild8d8ea.ts.net --port 8000 --groq-accepted
```

Terminal B: check local health first, then activate only when authorized:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
& 'C:\Program Files\Tailscale\tailscale.exe' funnel --https=443 http://127.0.0.1:8000
```

Use foreground Funnel for this initial demonstration; Ctrl+C removes exposure.
Do not serve a filesystem folder, use raw TCP, expose Ollama port 11434, or open
port 8000 on the LAN. Later stop a matching configured rule explicitly with:

```powershell
& 'C:\Program Files\Tailscale\tailscale.exe' funnel --https=443 off
```

Public URL: `https://desktop-md6j59m.taild8d8ea.ts.net/`; administrator UI: `/admin`.
Tailscale terminates TLS and reverse-proxies to loopback. Uvicorn trusts forwarded
headers only from `127.0.0.1,::1`, not `*`. The Host must stay the public hostname
and the forwarded protocol must be HTTPS for existing Origin checks. Verify on
the actual public URL; this preparation cannot certify live proxy behavior.
Log in through HTTPS, not local HTTP, because authentication cookies are Secure.

## End-to-end acceptance checklist (live AI calls require authorization/quota)

1. `/health` returns only status; `/` and assets render; browser API calls use the
   same HTTPS origin. No mixed content, key exposure or backend CORS workaround.
2. Login: Secure/HttpOnly/SameSite cookie; `/api/auth/me` correct identity; logout
   invalidates it. An employee cannot use admin read/mutation APIs.
3. Exact VPN question and lost-device reporting: validated supporting sources,
   grounded answer, required steps/timing preserved. Vacation rule retains its
   conditions. Forgotten-password recovery abstains; no invented company policy.
4. Clarification and contextual follow-up resolve; explicit topic change wins.
   Knowledge never creates a ticket. Distinguish provider error from abstention.
5. First turn appears in Recent while processing. Switch away/back, delete an
   unrelated conversation; result stays attached to its originating conversation.
6. Contextual ticket request proposes, not executes. Confirm once: one mock
   ticket. Repeated approval: no second execution. Cancel: zero executions.
7. `/admin` login/account list/roles/audit work. Same-origin provisioning and
   activation work; foreign/missing Origin is rejected; last admin stays protected.
8. Stage a clearly labeled fictional test Markdown document with explicit roles.
   Publish/rollback; inaccessible roles cannot retrieve it. Do not replace the
   bundled reference corpus or historical index/benchmarks.
9. Stop only after requests finish, restart with the SAME root: accounts, history,
   ticket state, knowledge versions and rollback survive. Terminal action cards
   remain terminal; unavailable proposals cannot acquire authority after refresh.
10. Keep laptop powered/awake and connected; verify from a browser outside the
    tailnet. Teacher needs no Tailscale client for Funnel. Sleep/shutdown ends
    availability but does not erase local persistence. After reboot, manually
    restart the app and foreground Funnel. No scheduled task or sleep setting was
    changed. Background `--bg` Funnel can resume automatically after reboot, so
    do not enable it casually or assume the Python backend also restarts.

Sources: [Funnel CLI](https://tailscale.com/docs/reference/tailscale-cli/funnel),
[Funnel requirements](https://tailscale.com/docs/features/tailscale-funnel).
