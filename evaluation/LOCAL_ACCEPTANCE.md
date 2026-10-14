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

The live runner defaults to Ollama and supports explicitly selected candidates;
it verifies native structured output is off. It keeps the configured embedding and
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
# Optional Gemini candidate (separate authorization required)

The original matrix and grading remain unchanged. Keep the saved Ollama baseline.
After deterministic implementation review and explicit live authorization, select
both inference providers as `gemini`, set both Gemini inference model variables
to `gemini-3.8-flash`, and set `GEMINI_INFERENCE_TIMEOUT_SECONDS=120`.
Reuse `GEMINI_API_KEY` server-side; do not enable billing. Keep routing and
embeddings unchanged. Inference thinking is fixed at **high** for reasoning
reliability; temperatures remain 0.0/0.1 and output limits remain 320/220.
No schema mode, tools, retries, or provider fallback are enabled.

Authorized future command (DO NOT run during implementation review):

```powershell
.venv\Scripts\python.exe -m evaluation.local_acceptance --live --candidate gemini --output artifacts/evaluation/knowvia-gemini-acceptance-v1.json
```

Check the project's actual free-tier quota before authorization. This command
does not pace requests; if available quota cannot accommodate the run, stop and
plan bounded pacing/resumption rather than paying or hiding quota failures.
Transport failures invalidate the measurement. Inspect all changed decisions
and answer claims, especially VPN, lost laptop and forgotten-password cases.
The Gemini candidate cannot write to the canonical Ollama baseline path.
# Optional Groq candidate (not accepted as default)

Ollama remains the reference/default; Gemini routing and optional Gemini inference
remain separate and unchanged. Groq uses the standard HTTPS chat-completions API
without a new dependency, tools, transport retries, or fallback. The existing
one semantic evidence repair is preserved. No provider is called on import.

Configure `GROQ_API_KEY` only in the gitignored local `.env` or platform secret
store, never in an artifact or chat. Select both inference providers explicitly:

```dotenv
KNOWVIA_GENERATION_PROVIDER=groq
KNOWVIA_EVIDENCE_EVALUATOR=groq
GROQ_GENERATION_MODEL=openai/gpt-oss-120b
GROQ_EVIDENCE_MODEL=openai/gpt-oss-120b
GROQ_TIMEOUT_SECONDS=120
GROQ_EVIDENCE_REASONING_EFFORT=medium
GROQ_GENERATION_REASONING_EFFORT=low
GROQ_EVIDENCE_MAX_COMPLETION_TOKENS=2048
GROQ_GENERATION_MAX_COMPLETION_TOKENS=1024
```

Temperatures stay evidence `0.0` and answer `0.1`. Reasoning is not returned;
GPT-OSS uses `include_reasoning=false`, not `reasoning_format`. Completion limits
provide reasoning headroom, not permission to invent more facts. Incomplete
responses fail closed; semantic validation remains authoritative.

**Do not run these commands without separate live-test authorization.** First
verify the account is on Free in the Groq console and check its actual model
access/limits. Neither API selection nor this harness can establish billing tier.
Never upgrade billing to complete an evaluation.

```powershell
# Exactly one existing natural-vpn turn, isolated authenticated state/mock tools.
.venv\Scripts\python.exe -B -m evaluation.groq_smoke --live
# Only after separately authorizing all 57 turns; same matrix and grading.
.venv\Scripts\python.exe -B -m evaluation.local_acceptance --live --candidate groq --output artifacts/evaluation/knowvia-groq-acceptance-candidate.json
```

Groq outputs cannot overwrite the Ollama reference or an existing candidate file.
The smoke output is `artifacts/evaluation/knowvia-groq-vpn-smoke.json`, contains
allowlisted diagnostics, final answer/citations, and no reasoning or raw provider
bodies. Both runners stop after a provider failure; no quota/transport retry occurs.
The Groq full runner now applies inference-level pacing and checkpoints described
below. If interrupted, report an incomplete run rather than hiding failed turns
or automatically retrying them.
Inference counts exclude unchanged routing calls. Compare strict passes, supported
success, unsupported abstention, unsafe answers, source precision, repairs, provider
errors and latency separately from deterministic test results. Inspect every
changed evidence decision and generated claim. Zero unsafe answers is required,
even though the saved Ollama reference contains one unsafe answer.

## Safe Groq batches (offline implementation; live authorization required)

No live authorization is implied by these commands. The runner selects Groq only
inside its process, preserving .env, normal Ollama defaults, optional Gemini
generation, and the configured Gemini router. Ensure both model configurations
remain GPT-OSS 120B, evidence medium/2048/temperature 0, answer low/1024/temperature
0.1, timeout 120. Keep the reference Sentence Transformers embedding provider
for a like-for-like comparison. Inspect actual Free-plan limits/remaining allowance
in the dashboard beforehand; the harness cannot verify billing or other usage.

First five **whole scenarios**, unchanged original order:

```powershell
.venv\Scripts\python.exe -B -m evaluation.local_acceptance --live --candidate groq --output artifacts/evaluation/groq-acceptance-v1/results.json --batch-scenarios 5 --min-call-interval-seconds 65 --minute-token-budget 6000 --daily-token-budget 150000
```

After separately authorizing the next batch:

```powershell
.venv\Scripts\python.exe -B -m evaluation.local_acceptance --live --candidate groq --output artifacts/evaluation/groq-acceptance-v1/results.json --batch-scenarios 5 --min-call-interval-seconds 65 --minute-token-budget 6000 --daily-token-budget 150000 --resume
```

- Pacing applies to **every** Groq call start, including answers and the existing
  one semantic repair. No transport retry/fallback is added. Gemini routing is
  unchanged and has a separate quota; Groq budgets do not cover its calls.
- A conservative prompt estimate (UTF-8 bytes / 3 + 512 tokens) plus maximum
  completion tokens is reserved and atomically saved **before** sending. This is
  not an exact GPT-OSS tokenizer or a guarantee of provider accounting. Actual
  usage replaces the estimate; unknown/failed calls retain their reservation.
  Token windows conservatively retain charges for 60 seconds/24 hours after
  completion, rather than ageing long-running completion tokens out too early.
  An actual reservation overrun stops further inference rather than being hidden.
  Overlarge requests fail locally; do not lower limits to force them through.
- Reasoning tokens are a diagnostic subset of completion tokens, **not added
  again** to total usage. Minute budgeting uses a rolling 60-second window; daily
  budgeting uses a conservative rolling 24 hours, persisted across batches.
  Other key/account usage is unknown; leave headroom and do not run concurrently.
- Stop on any Groq provider failure, including authentication/access/billing,
  quota, timeout, transport, invalid or incomplete output. An ordinary Exception
  stop latch prevents subsequent calls even if the application catches the error.
  A valid but semantically negative decision remains a measured quality failure;
  it does not trigger a retry. Invalid evidence still follows existing bounded
  semantic repair when transport succeeded.
- Atomic replace preserves the previous checkpoint if writing fails. A lock
  prevents two runners using the same checkpoint. If a crash leaves a .lock file,
  verify no runner is active before manually removing it. Never auto-break locks.
- Temporary auth/conversation/proposal databases are removed when the process
  ends. Resume skips only completed scenarios and recreates clean isolated state
  for remaining independent scenarios. Multi-turn scenarios are never split into
  batches. An active/interrupted scenario blocks automatic resume, including a
  crash during unchanged Gemini routing. No uncertain call is automatically replayed.
- Only after **explicit human approval to replay that incomplete scenario**, add
  `--approve-replay-incomplete` to the resume command. The entire scenario is
  rerun, not just missing turns; previous completed turns/in-flight metadata are
  preserved under interrupted_attempts. Reservations from old attempts remain
  charged. This can repeat earlier calls/actions inside **isolated mock state**;
  it is not exactly-once network recovery. Do not use this flag casually.
- Protected matrix, referenced benchmark, original Llama artifact, prior Groq
  root artifacts, knowledge documents and index hashes are verified at checkpoints
  and resume. Changed runtime/pacing or scenario order also refuses resume.
- Candidate status is RUNNING/BATCH_COMPLETE/COMPLETE/STOPPED. Quality acceptance
  always remains NOT_EVALUATED: COMPLETE does not mean the model passed.
- Visible evidence fields are stored only after the existing parser accepts them:
  requested_claim, directly_supported_facts, supporting_evidence_ids, missing_fact.
  No hidden reasoning, prompts, raw headers or complete provider envelopes are
  saved. Final employee-facing text/citations and sanitized provider metadata remain.
- Existing strict grading is unchanged. Aggregate Groq tokens/calls, mean/median
  inference latency, median HTTP latency, errors, supported-question false negatives
  and paired reference comparisons supplement—not replace—the original metrics.
  False negatives require a parsed INSUFFICIENT decision and exclude provider-error
  abstentions; invalid evidence is tracked by validation diagnostics. All-attempt budget accounting
  includes interrupted/unknown reservations; normal quality metrics use current
  candidate rows, with interrupted attempts retained separately.
- HTTP latency subtracts harness pacing waits; elapsed-with-pacing is also saved.
  Inference latency excludes pacing; final full-run mean/median uses all 57 turns,
  including controls, as in the reference. No exhaustive semantic judge is added:
  inspect changed answers/citations against policy text, especially extra claims.

Offline verification (no providers called):

```powershell
.venv\Scripts\python.exe -B -m pytest tests/test_groq_acceptance_safeguards.py tests/test_groq_smoke_diagnostics.py tests/test_local_acceptance.py -q
.venv\Scripts\python.exe -B -m knowvia.repository_safety
```
