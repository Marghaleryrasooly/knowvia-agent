# Local acceptance reference

This is measurement, not a production quality approval. Application code,
prompts, providers, the existing 40-case benchmark and index are unchanged.

The matrix samples the existing policy-grounded benchmark and adds exact live
failures, permission variants, clarification answers, contextual knowledge,
topic shifts, typo/problem context, no-context/competing-problem actions,
confirmation/cancellation/replay and identity/session isolation. Expectations
live only in evaluation data. The vacation-submission channel and forgotten
password procedure are undocumented; those questions must abstain.

## Run

From the repository root, with existing local dependencies and Ollama running:

```powershell
.venv\Scripts\python.exe -m pytest -q -m "not live and not ollama_live and not m3_live and not m4_live and not m5_live and not gemini_live and not jira_live" --junitxml=artifacts/evaluation/local-acceptance-deterministic.xml
.venv\Scripts\python.exe -B -m evaluation.local_acceptance --live --deterministic-report artifacts/evaluation/local-acceptance-deterministic.xml
```

The live runner refuses non-Ollama generation/evaluation and verifies native
structured output is off. It keeps the currently configured embedding and
routing providers. If routing is Gemini, that existing routing path makes
remote routing calls; evidence and grounded generation remain local Ollama.
No provider is silently substituted. The installed ONNX artifact directory can
be supplied to the optional parity regression test without changing the web
application's embedding selection.

All HTTP requests use Starlette TestClient, real account registration/login,
session cookies and the application chat/confirmation endpoints. Four fictional
employees use ephemeral, randomly generated credentials. All runtime databases,
sessions, pending actions and audits are isolated in a temporary directory and
removed when the run ends. External action tools are replaced in the harness by
one counting local tool; no Jira/MCP operation is possible.

The report is checkpointed to
`artifacts/evaluation/knowvia-local-acceptance-baseline.json`. Interrupted reports
remain RUNNING and cannot be accepted. Reruns start clean; they do not reuse
old conversations or proposals. Preserve a copy before replacing a reference.
Secret-bearing environment values, ephemeral passwords and cookie tokens are
redacted before serialization. Transport errors record categories, not headers,
provider errors or credentials.

## Interpretation

- Deterministic application tests are separate from live quality results.
- Supported-question success checks presentation plus documented required facts.
- Strict live PASS additionally requires the exact expected supporting source
  set, validated citation correspondence, source scope and execution counts.
  A factually correct answer with extra citations therefore fails strict PASS.
- Supporting-source precision is micro-averaged over emitted source citations.
  Abstentions have no citations and are not counted as perfect precision.
- Unsafe answers count substantive KNOWLEDGE responses to insufficient-evidence
  questions. Required-fact/forbidden-claim checks are not an exhaustive semantic
  judge: inspect actual answers and supporting text for additional claims.
- Average live latency includes every matrix turn (including non-LLM action
  controls). Per-provider and per-turn latency are retained for fair comparisons.
- Regression count is per turn of marked scenarios, not a count of unit tests.
- ACCEPTED AS REFERENCE means complete, index unchanged, no transport failure,
  and (when attached) no deterministic failure. Model/content failures remain
  failures; no prompts or thresholds are tuned to improve this measurement.

Reuse deterministic coverage in `test_authentication*`,
`test_employee_directory`, `test_account_registration`,
`test_m9_identity_permissions`, `test_confirmation`, `test_conversation`,
`test_conversation_management`, `test_m13_integration`, `test_ticket_lifecycle`,
`test_evidence`, `test_resilient_routing`, and `test_m15_*`. These include
auth-cookie/expiry/revocation/spoofing, in-flight ownership, first-message
discoverability, legacy terminal cards, RBAC, ticket ownership and deployment
safety. They deliberately use fake model responses and are not live quality
proof. UI visual behavior still requires manual browser acceptance.
