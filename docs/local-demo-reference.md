# Historical local demonstration reference

This guide preserves an early development walkthrough across internal milestones
M1–M13. Its identity-selector workflow is not authentication and must not be used
as current public setup. Current demonstrations sign in with privately provisioned
accounts. Follow the [README](../README.md), [administration guide](administration.md)
and [secure HTTPS guide](tailscale-demo.md) instead.

The bundled index does not need rebuilding for normal startup. The historical
commands below are reference material, not instructions to run against a working
deployment. Never enable development identity overrides on the public website.

## Historical local setup (not the current authenticated workflow)

```powershell
.venv\Scripts\python.exe -m knowvia.cli index
$env:KNOWVIA_AGENT_ROUTER = "deterministic"
$env:KNOWVIA_AGENT_TOOL = "mock"
$env:KNOWVIA_IDENTITY = "employee-001"
.venv\Scripts\python.exe -m knowvia.web
```

Open `http://127.0.0.1:8000` and start a new conversation.

## Example demonstration scenarios

1. **Grounded HR answer** — Ask `How many vacation days do I get?` Show the
   concise answer and the Annual Leave source card.
2. **Conversational follow-up** — Ask `What about sick leave?` Explain that a
   fresh, HR-scoped retrieval and evidence decision is performed for the new
   turn.
3. **Safe troubleshooting normalization** — Start a new conversation and ask
   `My VPN isn't working.` Show that a declarative problem becomes a knowledge
   request, never an implicit ticket action.
4. **Explicit ticket action** — Ask `Create an IT ticket for my VPN problem.`
   Show the proposal card. Point out that no ticket exists before confirmation.
5. **Cancel boundary** — Cancel the proposal once to demonstrate that canceling
   executes nothing. Repeat the explicit request, then select **Create ticket**
   to show the exact returned ticket ID and status.
6. **Ownership and status** — Ask for the created ticket's status. Switch to a
   different demo identity and repeat the lookup to demonstrate safe denial.
7. **Permission denial** — Select `it-employee-001` and ask
   `What is the meal allowance?` Show that protected Finance knowledge is not
   returned.
8. **Compound request** — As `employee-001`, ask `My VPN isn't working. Give me
   the troubleshooting guidance and help me create a ticket.` Show the grounded
   knowledge response and independent confirmation card.
9. **Abstention** — Ask `Can I paste confidential source code into ChatGPT?`
   Show that related documents do not become an unsupported company approval or
   prohibition.
10. **Developer details** — Enable the optional details switch and show only the
    safe high-level skill/capability/grounding/authorization fields. Disable it
    to return to the employee view.

## Optional integrations

The same UI can use `KNOWVIA_AGENT_TOOL=mcp` to demonstrate the protocol adapter
over the mock backend. Jira should be enabled only in a sandbox with explicit
credentials and deliberate confirmation. Do not perform a live Jira creation in
a thesis rehearsal unless that side effect is intentional.

## Talking points

- Models propose; deterministic application boundaries authorize and execute.
- Retrieved candidates and validated supporting evidence are separate.
- Identity, permissions, ownership, confirmation, and lifecycle state never come
  from model claims.
- Skills expose only the relevant knowledge scope and allowed tools.
- The web application is replaceable presentation code over reusable structured
  services, not a rewrite of the agent.
