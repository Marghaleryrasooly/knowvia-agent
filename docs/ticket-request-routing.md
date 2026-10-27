# IT ticket request boundaries

Ticket detail extraction is deterministic and shared by conversation resolution
and the local routing fallback. A clear current-turn problem always takes
precedence over previous context. Problem-first wording and action-first
`because`, `for`, `about`, `regarding` and colon forms are supported. Supplied
but unclear details never authorize substitution of an older problem.

Explicit ticket requests still pass trusted permission checks and the normal
proposal/confirmation boundary. Gemini routing, Groq generation, evidence rules,
MCP transport and atomic confirmation are unchanged. Troubleshooting alone does
not request a ticket; quoted, hypothetical, negated and competing instructions
cannot establish action intent through the deterministic guard.

## One bounded details answer

When an authenticated, ticket-authorized employee explicitly requests a ticket
without details, the server records a pending-details marker in the existing
conversation turn. It is not a proposal or executable action, and needs no new
database table. The marker is bound by the existing user/conversation storage.

Only the immediately following clear IT problem report can complete it, within
five minutes and with permissions checked again. The result is a normal ticket
preview, never execution. Cancel/never mind stops preparation. An unrelated,
ambiguous, affirmative-only or stale answer consumes the opportunity; the user
must restate an explicit ticket request. A new explicit request is processed on
its own merits. Ordinary “yes” does not confirm a proposal.

The extractor is intentionally conservative, not a general natural-language
workflow engine. Uncertain phrasing should clarify; difficult requests can be
restated as an explicit ticket request with one complete problem description.

## Offline verification and integration

`tests/test_ticket_request_context.py` covers extraction, conversation isolation,
fake Gemini routing, unavailable-provider fallback, role enforcement, previews,
confirmation, cancellation and duplicate-execution prevention. Other existing
routing, knowledge/evidence and action-security regressions remain authoritative.
No changes to frozen acceptance datasets are needed.

Develop this change in its isolated Git worktree with disposable SQLite state,
dotenv loading disabled, fake transports and mock tools. Do not point worktree
startup or tests at the public data directory. Before any later integration,
review the worktree diff, rerun offline regressions and obtain separate approval.
Integration/restarting the public deployment is not part of this verification.
