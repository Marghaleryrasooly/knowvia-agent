# Knowvia — Evidence-Aware Enterprise AI Agent

## Current implementation

Knowvia is a university demonstration using fictional NovaTech company documents.
Groq `openai/gpt-oss-120b` is the default evidence-evaluation and grounded-answer
model. Gemini handles routing in the public demonstration; the deterministic
router remains available for offline tests. Ollama is an optional local alternative,
not a requirement for the public website. Evidence validation, supporting sources,
RBAC and confirmation-gated mock ticket actions remain enforced.

The employee interface includes authenticated chat and persistent history.
The separate `/admin` dashboard supports account provisioning, activation,
deactivation, trusted roles and permissions, sanitized administrative activity,
and Markdown knowledge management with explicit access controls, atomic index
publication and rollback. Administrator bootstrap is private and has no default
password. See [administration setup](docs/admin-foundation.md) and
[knowledge management](docs/admin-knowledge-management.md).

The public university demonstration uses Tailscale Funnel HTTPS at
https://desktop-md6j59m.taild8d8ea.ts.net/. Availability requires the host laptop,
Knowvia process and Funnel to remain running. SQLite and uploaded knowledge stay
on the laptop's persistent private disk. Public self-registration is intentionally
disabled: an authorized administrator provisions demonstration accounts and shares
unique credentials privately. Never use test-fixture passwords publicly.

### Current quick start

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,gemini]"
Copy-Item .env.example .env
```

Copy the template only for a fresh checkout; never overwrite existing private
configuration. Set `GROQ_API_KEY` and `GEMINI_API_KEY` privately in the gitignored
`.env`, and select `KNOWVIA_AGENT_ROUTER=gemini`. The bundled vector index is
included; rebuilding it is unnecessary for normal startup. Local embeddings may
download their model on first use; the existing ONNX provider is also available.

For local HTTP development, use the documented development profile and explicit
`KNOWVIA_AUTH_COOKIE_SECURE=false`, then run
`.venv\Scripts\python.exe -m knowvia.web`. Never disable Secure cookies for the
public HTTPS profile. Follow the [Tailscale deployment checklist](docs/tailscale-demo.md)
for public startup, trusted hosts and persistent data. The launcher does not
activate Funnel itself. Keep public ticket backends mocked and real Jira disabled.

To use the optional local reference model, select both
`KNOWVIA_GENERATION_PROVIDER=ollama` and `KNOWVIA_EVIDENCE_EVALUATOR=ollama`,
and run the configured Ollama model locally. There is no automatic provider fallback.

The milestone sections below retain development history and reference commands;
the current setup above supersedes historical defaults and feature limitations.

## University demo publication and hosting

For Wednesday, use [the persistent Tailscale checklist](docs/tailscale-demo.md).
Its local-only launcher explicitly selects Groq after acceptance and never
activates Funnel itself. The earlier [Render preparation](docs/university-deployment.md)
and `render.yaml` remain inactive alternatives, not the selected deployment:
their ephemeral storage does not satisfy the persistence requirement.
Inference now defaults to accepted Groq; explicit Ollama remains available. Local setup uses
`python -m pip install -e ".[dev,gemini]"`, an ignored `.env` based on
`.env.example`, and `python -m knowvia.web`; the cloud image uses
`python -m knowvia.production`. Never publish credentials, runtime databases or
saved evaluation artifacts.

## Cloudflare Llama provider candidate

The optional `cloudflare` generation/evidence adapter uses the direct Workers AI
REST API with `@cf/meta/llama-3.2-3b-instruct`. Groq is now the default and
deployment examples are not switched automatically. Treat the remote provider
as a candidate until the live knowledge comparison and safety gates pass.

Server-only configuration names: `CLOUDFLARE_ACCOUNT_ID`,
`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_GENERATION_MODEL`,
`CLOUDFLARE_EVIDENCE_MODEL`, and `CLOUDFLARE_TIMEOUT_SECONDS` (default 120).
Provider selection is explicit through `KNOWVIA_GENERATION_PROVIDER` and
`KNOWVIA_EVIDENCE_EVALUATOR`. Store credentials in the ignored local `.env` or
platform secret store; never in browser state, source, examples or logs.

The adapter preserves existing prompts and settings: answer temperature 0.1 /
220 output tokens, evidence temperature 0 / 320 output tokens. It rejects
invalid envelopes, empty text and tool calls, sanitizes transport errors,
disallows redirects and has no automatic retry/fallback. Native REST JSON
objects are serialized back to text before the unchanged evidence validator.
No new runtime dependency is needed. Questions and company evidence are sent
to the remote provider, which introduces an external data-processing boundary.

Explicit authorized live comparison (fictional dataset only; no action tools):

```text
python -m knowvia.cloudflare_comparison --output artifacts/evaluation/cloudflare-comparison.json
```

This uses the unchanged 40-case dataset, identical retrieved candidates and
production settings, records the Ollama reference digest, compares decisions
and shared-evidence generation, and stops on provider failure. Artifacts stay
under ignored `artifacts/`. Automated grader results still require inspection
of changed decisions and answer grounding before deployment acceptance.

## Optional pinned ONNX embedding deployment

The default remains `local` (Sentence Transformers). Install `.[dev,gemini]`
for reference/index-building work, or `.[onnx,gemini]` for a lean web runtime.
`requirements-production.txt` now contains ONNX Runtime/tokenizers, not Torch.
The default Docker build preserves the local provider by explicitly installing
Sentence Transformers; use `--build-arg EMBEDDING_PROVIDER=onnx` for the lean image.
No retrieval/evidence settings or index contents change.

Prepare immutable assets explicitly, outside requests:

```powershell
python -m knowvia.onnx_assets --directory artifacts/onnx-minilm
$env:KNOWVIA_ONNX_MODEL_DIR = (Resolve-Path artifacts/onnx-minilm).Path
python -m pytest tests/test_onnx_embeddings.py -q -s
```

Only after parity succeeds, select `KNOWVIA_EMBEDDING_PROVIDER=onnx`.
The Docker ONNX build prepares `/opt/knowvia/model-cache/onnx-minilm`.
Readiness verifies both asset checksums without loading an inference engine;
health remains lightweight. Missing/corrupt assets fail closed with no runtime
download or provider fallback.

Both assets come from `sentence-transformers/all-MiniLM-L6-v2` revision
`1110a243fdf4706b3f48f1d95db1a4f5529b4d41`: plain FP32 `onnx/model.onnx`
and `tokenizer.json`. SHA-256 pins are in `knowvia.embeddings`. The implementation
uses 256-token truncation, masked mean pooling and L2 normalization. The parity
test reproduces every stored document vector without writing the index and
checks tokenization, finite 384-dimensional vectors, norms, cosine similarity,
all-chunk scores and top-3/5/10 order, including source scopes. Run it explicitly
with prepared assets; a skipped parity test is not deployment acceptance.

Measure an isolated web-process footprint without external provider calls:

```powershell
python -m knowvia.embedding_memory --directory artifacts/onnx-minilm
```

This reports current/peak resident memory at application creation, model loading,
short/256-token embedding, Argon2 hashing/verification and index scoring. It uses
temporary application state, not developer accounts. Repeat inside the actual
Linux container under its memory limit and representative concurrent API traffic
before claiming 512 MB compatibility. This probe does not run remote generation,
evidence evaluation or a complete authenticated HTTP chat request.

Knowvia is a small local RAG learning project. M1 retrieves relevant NovaTech
policy sections. M2 supplies those sections to a local language model, produces
a concise employee-facing answer, and displays the retrieved sources. M3 adds
an explicit evidence-sufficiency gate before normal answer generation. M4 is
the frozen end-to-end reliability benchmark. M5 adds a small agent layer that
chooses whether to answer, execute a supported action, clarify, or decline an
unsupported action.

M13 packages the completed M1–M12 architecture behind a polished local web
application. The browser is a presentation client only: identity, skill
selection, evidence checks, authorization, confirmation, ownership, lifecycle,
MCP validation, and tool execution remain enforced by the Python backend.

## M5 agent architecture

```text
Original employee request
          ↓
  deterministic AgentRouter
    ┌─────┼────────┬───────────┐
    ↓     ↓        ↓           ↓
KNOWLEDGE ACTION  CLARIFY   UNSUPPORTED
    ↓      ↓        ↓           ↓
existing  validated ask for    decline
M1–M3     AgentTool missing info safely
pipeline
```

`AgentOrchestrator` sits above the unchanged `AnsweringService`. The default
M5 router is deterministic and intentionally narrow: informational questions
use the existing knowledge pipeline; only an explicit request to create an IT
support ticket can execute the registered action tool. A ticket request without
a problem description asks a clarification question. Other recognized action
requests are unsupported and execute nothing.

The router receives only the original employee request. Retrieved chunks are
created later, inside the knowledge branch, and are never routed as employee
instructions. Therefore action-like text in a company document cannot directly
trigger a tool.

### Reusable tool boundary

Every tool implements the provider-neutral `AgentTool` contract: a name,
description, required arguments, deterministic argument validation, and an
`execute` method. All tools return the same `ToolResult` shape with `success`,
`tool_name`, `message`, `data`, and `error`. The orchestrator neither imports
nor depends on an IT-ticket-specific result type.

M5's `LocalITTicketTool` is an in-memory demonstration. It creates IDs such as
`IT-0001` and returns ticket data, but contacts no real service and persists
nothing. In M6, Jira, ServiceNow, HR, or other enterprise implementations can
be added behind the same `AgentTool` interface without changing
`AgentOrchestrator`.

## What this milestone builds

```text
Markdown documents
       ↓
parse titles and sections
       ↓
one section = one chunk
       ↓
local Sentence Transformers embedding for every chunk
       ↓
local JSON vector index
       ↓
embed an employee question
       ↓
cosine similarity
       ↓
top-k chunks
       ↓
strict evidence-sufficiency decision
       ├── INSUFFICIENT → deterministic abstention
       └── SUFFICIENT
              ↓
grounded company context
       ↓
Ollama + llama3.2:latest
       ↓
answer + programmatic source list
```

### Parsing

Parsing turns each Markdown file into structured data. In this dataset, the
first `#` heading is the document title and every `##` heading starts a policy
section. Knowvia preserves the source filename, document title, section
heading, and section body.

### Chunking

A chunk is the unit that retrieval can return. The NovaTech documents already
have short, meaningful sections, so Knowvia uses **one `##` section per
chunk**. It never combines unrelated sections. A small paragraph-based fallback
exists only for a future section longer than 4,000 characters.

The title and section are added to the text sent to the embedding model. This
helps a short passage retain its context. The original section body remains
separate for clean CLI display.

### Embeddings

An embedding is a list of numbers representing the meaning of text. Texts with
similar meanings tend to have vectors pointing in similar directions. By
default, Knowvia runs `sentence-transformers/all-MiniLM-L6-v2` on your own
computer. It is free to run, needs no API key, and produces a 384-number vector
for each chunk and question. The model is downloaded and cached on first use.

The retrieval pipeline depends on the `EmbeddingProvider` interface. OpenAI's
`text-embedding-3-small` remains available as an optional provider, so changing
providers does not affect parsing, chunking, vector storage, or retrieval.

### Vector storage

M1 stores chunk metadata and embeddings in `data/knowvia_index.json`. JSON is
transparent, local, and sufficient for this tiny dataset. Search loads these
vectors and performs an exact scan with NumPy.

The rest of the pipeline depends on the `VectorStore` interface, not directly
on JSON. A later PostgreSQL + pgvector implementation can provide the same
operations—replace the index, report its embedding model, and search—without
changing parsing, chunking, embedding, or retrieval orchestration.

### Semantic search and cosine similarity

Semantic search embeds the employee's question with the same model used for
the documents. It compares the question vector with each stored vector using
cosine similarity. A higher score means the vectors are more closely aligned
and the texts are likely more related.

### Top-k

`k` is the number of matches returned. With `--top-k 3`, Knowvia sorts all
chunks by similarity and displays the best three. It does not yet decide that a
low-scoring result should be rejected; abstention belongs to a later milestone.

### Grounded answer generation

The `ask` command sends the employee question and top-k retrieved chunks to
the selected provider. The current default is Groq `openai/gpt-oss-120b`;
the optional Ollama reference model is `llama3.2:latest`. Its system prompt says to use
only the supplied company context for company-specific claims, avoid invented
policies, keep the answer concise, and report when the supplied information is
clearly insufficient.

This prompt is an initial grounding mechanism, not a guarantee against every
hallucination. M2 intentionally has no confidence score, evidence threshold,
advanced abstention system, or other reliability architecture.

### M3 evidence-sufficiency gate

Relevance is not the same as sufficiency. Before normal answer generation, a
separate `EvidenceEvaluator` receives the exact employee question and retrieved
chunks. The default `LLMEvidenceEvaluator` uses local `llama3.2:latest` and must
return strict JSON with one of two decisions:

```json
{
  "decision": "SUFFICIENT",
  "reason": "The evidence directly states the requested policy.",
  "missing_information": ""
}
```

If the decision is `INSUFFICIENT`, normal answer generation is not called.
Knowvia returns a deterministic abstention instead. Empty retrieval, invalid
JSON, extra fields, ambiguous decisions, empty reasoning, provider failures,
and invalid evaluator return types all fail closed to `INSUFFICIENT`.

The evaluator prompt specifically warns that related facts and information
classification do not establish an unstated approval or permission rule. This
is an explicit, testable routing step—not another instruction in the answer
prompt.

The sufficiency rubric also recognizes direct policy support expressed as a
quantity or bound, permission, prohibition, requirement, procedure, eligibility
rule, entitlement, definition, or condition. It does not demand that evidence
repeat the question's wording or enumerate unrelated exceptions when the
requested information is already explicit.

### Sources and the M2 citation limitation

The language model does not write citations. Knowvia derives each source from
the filename and section metadata on the chunks returned by M1 retrieval. This
prevents the model from inventing filenames or section names.

In M2, the displayed source list represents **all retrieved chunks supplied to
generation**. It does not claim that every source supports every sentence in
the answer. Sentence-level attribution belongs to a later milestone.

## Project layout

```text
src/knowvia/
├── parsing.py       # Markdown → structured sections
├── chunking.py      # sections → chunks
├── embeddings.py    # local and optional OpenAI embedding providers
├── vector_store.py  # vector-store interface and local JSON implementation
├── retrieval.py     # indexing and semantic-search pipeline
├── generation.py    # generation interface and local Ollama provider
├── answering.py     # grounded prompt and retrieval → generation flow
├── evidence.py      # strict sufficiency evaluator and fail-closed boundary
├── evaluation.py    # local 12-case routing evaluation and metrics
├── eval_dataset.py  # versioned M4 dataset loading and validation
├── answer_grading.py # deterministic answer and forbidden-claim grading
├── pipeline_metrics.py # end-to-end metrics and failure stages
├── m4_evaluation.py # live runner, serialization, and comparison
├── agent_models.py  # agent decisions and generic tool/result models
├── agent_routing.py # replaceable router and deterministic M5 implementation
├── agent_tools.py   # AgentTool contract and local mock IT ticket tool
├── orchestrator.py  # safe routing and capability dispatch
├── m5_evaluation.py # deterministic routing/tool-use benchmark
├── gemini_routing.py # optional Gemini function-call proposal adapter
├── jira.py           # Jira Cloud REST v3 AgentTool implementation
├── confirmation.py   # SQLite pending actions and one-time claims
├── authorization.py  # replaceable action-authorization boundary
├── audit.py          # structured, sanitized audit events
├── m6_evaluation.py  # deterministic M6 safety benchmark
├── presentation.py   # deterministic employee-facing response boundary
├── m7_evaluation.py  # deterministic M7 presentation benchmark
├── models.py        # small data classes
└── cli.py           # command-line interface

tests/               # parser, chunker, store, and ground-truth tests
knowvia_m1_dataset/  # fictional NovaTech documents and expected retrievals
evaluation/          # versioned M4 evaluation dataset
data/                # generated local index
```

## Setup

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

No API key or `.env` file is required for the default local provider. Optional
configuration can be copied from the example:

```powershell
Copy-Item .env.example .env
```

The real `.env` and generated vector index are ignored by Git.

### Optional local Ollama reference setup

Ollama is a separate Windows application; it is not installed by `pip`. Install
Ollama, make sure it is running, and confirm that the configured model exists:

```powershell
ollama list
```

The default is the existing `llama3.2:latest` model. If it is not listed, pull
it once with `ollama pull llama3.2:latest`. Local Ollama normally serves its API
at `http://localhost:11434` and does not require an API key.

## Build the index

Indexing reads the policy files, creates chunks, calculates their embeddings
locally, and writes the JSON store:

```powershell
python -m knowvia.cli index
```

Expected output resembles:

```text
Indexed 37 chunks into ...\data\knowvia_index.json
```

Re-run indexing after changing a source document or embedding model.

## Search

```powershell
python -m knowvia.cli search "How many vacation days do I get?"
python -m knowvia.cli search "How often can I work from home?" --top-k 3
python -m knowvia.cli search "What should I do if my VPN doesn't work?"
```

Each result displays its rank, similarity score, source filename, section, and
original chunk text. Searching requires an existing index, but does not require
an API key with the default provider.

## Ask a grounded question

Build the index first, then ask a question:

```powershell
python -m knowvia.cli ask "How many vacation days do I get?"
python -m knowvia.cli ask "What should I do if my VPN fails?" --top-k 3
```

The workflow is unchanged through retrieval. The `ask` command then builds a
grounded prompt, calls the selected provider, prints the answer, and prints sources from
the retrieved chunk metadata:

```text
Answer:
Full-time employees receive 24 paid vacation days per calendar year.

Sources:
- 01_employee_handbook.md -> Annual Leave
```

Optional generation settings in `.env` are:

```dotenv
KNOWVIA_GENERATION_PROVIDER=ollama
OLLAMA_GENERATION_MODEL=llama3.2:latest
OLLAMA_HOST=http://localhost:11434
KNOWVIA_EVIDENCE_EVALUATOR=ollama
OLLAMA_EVIDENCE_MODEL=llama3.2:latest
```

For `ask`, the same local model is used through two separate abstractions: once
as an evidence evaluator and, only after `SUFFICIENT`, as the normal answer
generator.

## Run the M5 agent

The existing `index`, `search`, and `ask` commands remain available. The new
command routes the employee's original request before invoking a capability:

```powershell
python -m knowvia.cli agent "How many vacation days do I get?"
python -m knowvia.cli agent "Create an IT ticket because my VPN is still broken."
python -m knowvia.cli agent "Create an IT ticket."
python -m knowvia.cli agent "Approve my expense report."
```

Knowledge requests require the local index and the configured inference provider because they call
the complete M1–M3 pipeline. Ticket actions, clarification, and unsupported
actions use the deterministic router and do not call Ollama. The local mock
ticket store resets on each CLI invocation.

## M5 evaluation

M5 has a separate, versioned 24-case dataset at
`evaluation/knowvia_m5_eval_v1.json`. It does not alter the frozen M4 dataset.
The cases cover knowledge requests, explicit ticket actions, incomplete ticket
requests, unsupported actions, and action-like text quoted inside knowledge
requests.

```powershell
python -m knowvia.m5_evaluation
```

It reports routing accuracy, tool-selection accuracy, argument accuracy,
clarification accuracy, unsupported-action accuracy, tool-execution accuracy,
unintended-tool-execution rate, and the knowledge-to-tool safety rate. This is
a deterministic architecture test, not a natural-language understanding
benchmark for arbitrary employee wording.

## M3 behavior evaluation

Run the local evidence-routing evaluation over all 12 cases in
`knowvia_m1_dataset/m1_ground_truth.json`:

```powershell
python -m knowvia.evaluation
```

The eight `retrieve` cases are expected to route as `SUFFICIENT`. The four
`insufficient_evidence` cases are expected to route as `INSUFFICIENT`.

The report includes:

- Expected answer → system answers
- Expected abstention → system abstains
- Expected abstention → system answers
- Expected answer → system abstains
- Overall routing accuracy
- Answer-routing accuracy
- Abstention correctness
- Unsafe-answer rate
- Over-abstention rate

`Answer-routing accuracy` measures whether expected-answer questions were
classified `SUFFICIENT`. It does **not** measure whether the generated
natural-language answer was factually correct.

## Optional OpenAI embeddings

Install the optional dependency:

```powershell
python -m pip install -e ".[dev,openai]"
```

Copy `.env.example` to `.env`, add your `OPENAI_API_KEY`, and select OpenAI for
both indexing and searching:

```powershell
python -m knowvia.cli --provider openai index
python -m knowvia.cli --provider openai search "How many vacation days do I get?"
```

An index must be searched with the same model that created it. To switch back
to local embeddings, rebuild the index with `--provider local` (or omit the
flag, because local is the default).

## Tests

Run the complete non-live suite:

```powershell
python -m pytest -m "not live and not ollama_live and not m3_live"
```

The tests cover the providers, pipeline, and all eight retrievable cases from
`m1_ground_truth.json`. Fast tests use a deterministic embedding test double;
the local-model tests also load `all-MiniLM-L6-v2` and verify its real retrieval
results. The four `insufficient_evidence` records are intentionally not treated
as abstention tests in M1.

To evaluate retrieval with optional OpenAI embeddings after installing the
extra and configuring the key:

```powershell
python -m pytest -m live
```

Live tests call the OpenAI API and may incur a small charge.

Most M2 tests use fake retrievers, fake generation providers, and an injected
fake Ollama HTTP transport. They verify prompts, integration, answer display,
and metadata-derived citations without requiring a running model.

To run the optional local Ollama test:

```powershell
$env:RUN_OLLAMA_LIVE="1"
python -m pytest -m ollama_live
```

For the clearest end-to-end smoke test, use the real CLI:

```powershell
python -m knowvia.cli ask "How many vacation days do I get?"
```

## Current boundaries

This project intentionally contains no large agent framework, MCP integration,
production identity/RBAC, multi-tenancy, frontend, production database,
advanced reranking, confidence scoring, or advanced abstention system. The 3B
local evidence judge can still misclassify cases; fail-closed handling reduces
unsupported answers at the cost of possible over-abstention. The evaluator's
reason is diagnostic rather than independently verified policy evidence.

The agent also intentionally has no autonomous loop, planning system, memory,
production identity, or RBAC. Its deterministic router remains narrow, while
the optional Gemini router is fail-closed and model output never authorizes an
action. M6 added confirmation/idempotency, audit logging, secrets handling, and
one Jira implementation behind `AgentTool`; production OAuth, identity, and
authorization policy remain later-milestone work.

## M4 end-to-end evaluation

M4 measures the existing M1–M3 pipeline without tuning it. The versioned
dataset at `evaluation/knowvia_m4_eval_v1.json` contains 50 diverse cases across
all eight NovaTech documents: direct and paraphrased facts, numeric bounds,
permissions and prohibitions, procedures, eligibility, distractors,
multi-source questions, and unsupported questions that should abstain.

The validator checks unique IDs, behavior labels, criterion structure, and
every expected filename/section against the Markdown corpus. `ANSWER` cases
require sources, a reference answer, and deterministic criteria. `ABSTAIN`
cases cannot contain an invented reference answer.

### M4 metrics

- **Retrieval Recall@1 / Recall@3:** expected source-section pairs found in the
  first one or three chunks, aggregated across answerable cases.
- **Case Hit@1:** answerable cases with at least one expected source ranked
  first.
- **Case Complete@3:** answerable cases whose complete expected source set is
  present in the top three.
- **Routing metrics:** M3 overall routing accuracy, answer-routing accuracy,
  abstention correctness, unsafe-answer rate, and over-abstention rate.
- **Conditional answer accuracy:** correct generated answers among answerable
  cases that the evidence gate allowed through.
- **End-to-end answer success:** correct generated answers divided by all
  answerable cases, so retrieval and routing failures count.
- **Expected-source recall / source precision:** expected citation coverage and
  the fraction of displayed sources that were expected.
- **Complete citation accuracy:** allowed answer cases whose displayed source
  set exactly matches the expected set. M2 displays all retrieved chunks, so
  extra retrieved sources reduce this intentionally strict metric.

The deterministic answer grader supports required terms, alternatives,
numbers, and ordered procedure terms. A case may also define a small,
source-grounded `forbidden_terms` or `forbidden_claims` list. This catches a
known unsupported statement even when the expected fact is present; it is not
a general keyword blacklist or hallucination detector.

Failures are recorded independently as `RETRIEVAL_FAILURE`,
`ROUTING_FAILURE`, `ANSWER_FAILURE`, and `CITATION_FAILURE`. A case may have
more than one. Its primary stage follows pipeline order: retrieval, routing,
answer, then citation.

### Run and compare M4

With the local index built and Ollama running with `llama3.2:latest`:

```powershell
python -m knowvia.m4_evaluation run --top-k 3
```

The command validates the dataset, executes all 50 cases, prints a summary,
and saves a timestamped JSON artifact under `artifacts/evaluation/`. Artifacts
include configuration, aggregate metrics, answers, sources, deterministic
grades, and per-case failure stages.

To select an artifact path or compare compatible runs:

```powershell
python -m knowvia.m4_evaluation run --top-k 3 --output artifacts/evaluation/m4_baseline.json
python -m knowvia.m4_evaluation compare artifacts/evaluation/before.json artifacts/evaluation/after.json
```

Comparison requires matching dataset names, versions, and ordered case IDs. It
reports metric deltas, new passes, regressions, and primary failure-stage
changes.

M4 is a small synthetic benchmark, not proof of production reliability.
Deterministic criteria can miss valid wording variants and cannot detect every
unsupported extra claim. Case-specific forbidden claims cover only known,
source-grounded risks. Expected-source labels reflect the current corpus.
Local-model output can vary between runs, and M4 adds neither an LLM answer
judge nor sentence-level citation attribution.

The final M4 diagnostic pass also records evidence-evaluator raw output and
validation/provider errors inside evaluation artifacts only. These internal
details are never shown by the employee-facing `ask` command. With the local
3B evaluator, the dominant remaining failure is semantic/schema inconsistency:
the model can return `INSUFFICIENT` with an empty required `missing_fact`, even
when its extracted facts directly answer the question. Knowvia deliberately
fails closed instead of guessing whether to change that decision. Truncated
JSON also remains fail-closed. The preserved final diagnostic baseline is
`artifacts/evaluation/m4_diagnostic.json`; M4 is frozen after this pass so later
work can compare against it without changing the dataset or scoring rules.

## M6: optional Gemini routing and confirmed Jira actions

M6 preserves M5 and adds interchangeable implementations:

```text
AgentRouter                     AgentTool
├── DeterministicAgentRouter   ├── LocalITTicketTool
└── GeminiAgentRouter          └── JiraTicketTool
```

The deterministic router remains an offline/local option; public routing uses Gemini
and ticket tools remain mocked. Gemini only
proposes a decision. It receives the original employee request and public
function schemas—never RAG chunks, Jira configuration, credentials,
environment variables, authentication headers, pending-action state, or audit
data. Knowvia deterministically validates its decision and arguments.

Every Jira action becomes an `ActionProposal` in
`data/pending_actions.db`. SQLite provides an atomic one-time claim. Jira is
not called until the proposal ID is explicitly confirmed; the tool, arguments,
and authorization are rechecked immediately before execution. Repeated
confirmation cannot execute a proposal twice.

Jira creation uses REST API v3 and Atlassian Document Format. Project key and
issue type come from configuration, never Gemini. Knowvia does not
automatically retry after a network failure because Jira might already have
accepted the issue. Sanitized structured events are appended to
`data/agent_audit.jsonl` without credentials or authentication headers.

### Install optional Gemini support

```powershell
python -m pip install -e ".[dev,gemini]"
```

### Gemini setup

```dotenv
KNOWVIA_AGENT_ROUTER=gemini
GEMINI_API_KEY=your-gemini-api-key-here
GEMINI_ROUTER_MODEL=gemini-3.7-flash
```

1. Open [Google AI Studio API Keys](https://aistudio.google.com/app/apikey).
2. Accept the terms and create or select a Google Cloud project.
3. Select **Create API key** and copy the new authorization key.
4. Copy `.env.example` to `.env` and replace the placeholder.
5. Never commit `.env` or paste the key into a prompt.

The unpaid Gemini Developer API tier may use submitted inputs and outputs for
product improvement and human review. Use it only for fictional or
non-sensitive development requests, not confidential production company data.

### Jira setup

```dotenv
KNOWVIA_AGENT_TOOL=jira
JIRA_BASE_URL=https://your-company.atlassian.net
JIRA_USER_EMAIL=developer@example.com
JIRA_API_TOKEN=your-jira-api-token-here
JIRA_PROJECT_KEY=IT
JIRA_ISSUE_TYPE=Task
JIRA_TIMEOUT_SECONDS=15
```

Create an API token:

1. Sign in to [Atlassian API token management](https://id.atlassian.com/manage-profile/security/api-tokens).
2. Select **Create API token**. Use a dedicated, low-privilege development
   account and the shortest practical expiration.
3. Copy the token once and store it only in `.env` or a password manager.
4. Grant that account **Browse Projects** and **Create Issues** only in the
   intended test project.

M6 uses Jira's simple-script Basic authentication method with email and API
token. Production should replace it with OAuth 2.0 authorization-code
authentication behind the same provider boundary.

Create a test target:

1. Prefer an Atlassian organization sandbox: open Atlassian Administration and
   select **Apps → Sandboxes → Create sandbox**, then add Jira. Formal
   sandboxes generally require Premium or Enterprise.
2. Otherwise create a separate Jira Cloud test site or dedicated project with
   no production data.
3. Create **Knowvia Integration Test**, for example with project key `KIT`.
4. Choose an issue type available on its create screen, such as `Task`.
5. Grant the development account Browse Projects and Create Issues there.
6. Point the Jira environment variables at that test environment.

Propose an action without executing it:

```powershell
python -m knowvia.cli agent "Create a Jira ticket because my VPN is broken." --tool jira
```

Add `--router gemini` to use Gemini. Review the displayed fields, then execute
or reject explicitly:

```powershell
python -m knowvia.cli confirm <proposal-id>
python -m knowvia.cli cancel <proposal-id>
```

Do not confirm a proposal against production during development.

### M6 evaluation and optional Gemini test

The separate `evaluation/knowvia_m6_eval_v1.json` dataset covers malformed
function calls, provider failures, confirmation, rejection, duplicate
confirmation, tool failures, auditing, and document-text injection. M4 and M5
remain frozen.

```powershell
python -m knowvia.m6_evaluation
```

The primary gate is an unintended real-tool-execution rate of 0%.

The optional Gemini live test sends fictional requests and has no Jira
execution path:

```powershell
$env:RUN_GEMINI_LIVE="1"
python -m pytest -m gemini_live
```

There is deliberately no automatically enabled Jira creation test. A real
issue requires credentials, a generated proposal, and explicit confirmation.

## M7: employee-facing presentation

M7 separates internal agent state from what an employee sees:

```text
AgentOrchestrator / AnsweringService
                 ↓
    AgentResult / AnswerResult
                 ↓
 DeterministicEmployeeResponsePresenter
                 ↓
        EmployeeResponse
                 ↓
        CLI today, UI later
```

`AgentResult` remains the authoritative structured record for tests, auditing,
debugging, and future interfaces. The presenter is strictly downstream: it
cannot route, select tools, validate or change arguments, confirm proposals,
execute actions, alter evidence decisions, or create citations.

Employee responses use deterministic templates for action state and external
identifiers. The presenter reads only allowlisted fields such as a validated
ticket title, description, and Jira issue key. It never exposes raw router
reasons, diagnostic categories, stack traces, provider responses, credentials,
headers, or arbitrary tool-result data.

Knowledge answer text is preserved exactly from the existing grounded pipeline;
M7 does not add another language model. Source metadata is also preserved and
displayed under **Sources consulted**. This wording is intentional because the
current citations identify retrieved chunks supplied to the pipeline, not
sentence-level attribution.

Successful action wording confirms only the fact represented by the successful
`ToolResult`, for example:

```text
Done — I created IT ticket KIT-123 for “VPN problem.”
```

It does not promise a response time, resolution time, IT follow-up, review, or
service level. An ambiguous Jira network outcome remains explicit and advises
checking Jira before retrying, which avoids implying success or encouraging a
duplicate issue.

### Employee mode and debug mode

Employee-facing output is now the default for `ask`, `agent`, `confirm`, and
`cancel`:

```powershell
python -m knowvia.cli agent "Create a Jira ticket because my VPN is broken." --tool jira
```

A pending real action is still not executed. The response includes an opaque
confirmation identifier and CLI commands to confirm or cancel it. Only the
existing `confirm` command can claim and execute that proposal, after repeating
the M6 validation and authorization checks.

Use `--debug` when structured developer output is needed:

```powershell
python -m knowvia.cli agent "How many vacation days do I get?" --debug
python -m knowvia.cli confirm <proposal-id> --debug
```

Debug mode retains decision labels, raw tool names, proposal fields, and exact
source filenames. It should not be used as the normal employee interface.

### M7 evaluation

M7 has a separate versioned dataset at
`evaluation/knowvia_m7_eval_v1.json`. It does not modify the frozen M4, M5, or
M6 datasets. The evaluation constructs deterministic internal results and
measures only the presentation boundary:

```powershell
python -m knowvia.m7_evaluation
```

Metrics cover factual preservation, action-state preservation, identifier
preservation, citation/source preservation, confirmation preservation,
internal-detail leakage, and unsafe presentation. No Gemini, Ollama, Jira, or
other external provider is required.

M7 remains intentionally limited to deterministic English templates and the IT
ticket action policy. It does not add localization, Skills, RBAC, Jira lifecycle
tracking or webhooks, MCP, autonomous planning, or a frontend.

## M8: skills and progressive disclosure

M8 adds a capability-selection layer above the existing agent orchestrator:

```text
Employee request
      ↓
SkillRegistry.discover() — name and description only
      ↓
DeterministicSkillSelector
      ↓
SkillRegistry.load() — exactly one selected full skill
      ↓
Selected instructions + source scope + allowed tools
      ↓
Existing AgentOrchestrator and M1–M7 safety pipeline
```

The registry deliberately exposes two different representations. A
`SkillSummary` contains only a stable identifier, display name, and short
description. The selector cannot see instructions, tools, or document scope.
Only after a single skill is selected does the registry return its complete
`SkillDefinition`. Ambiguous and unsupported requests load no complete skill.

The initial deterministic skills are:

- **HR** — Employee Handbook, Remote Work Policy, and Employee Benefits Guide;
- **IT Support** — IT access/security, setup, engineering, and information
  classification documents, with `create_it_ticket` available;
- **Finance** — Business Expense Policy, with no action tool.

Tool filtering happens before either the router factory or `AgentOrchestrator`
is created. Consequently, an HR or Finance request cannot reference the ticket
tool through the selected runtime. Selecting IT Support still does not execute
a ticket action: the existing authorization, argument validation, pending
proposal, explicit confirmation, atomic claim, revalidation, execution, and
audit lifecycle remains authoritative.

Knowledge requests use `SourceScopedRetriever`, a small wrapper around the
unchanged M1 retriever. It requests the current small index's candidate set,
filters results by the selected skill's exact source allowlist, preserves
semantic order, and supplies the scoped results to the unchanged
`AnsweringService`. The ordinary `ask` and `search` commands remain unscoped.
For a production-scale vector database, source filtering should be performed
inside the vector query rather than retrieving a broad candidate set locally.

Skill instructions are isolated at the selected-skill runtime boundary in M8.
They are not injected into the frozen grounded-generation prompt. Their current
enforceable effects are the selected tool allowlist and document scope; a later
model-assisted runtime may consume the selected instructions without exposing
other skills.

Normal employee output still receives only `AgentResult` through the M7
presenter, so skill internals are not displayed. Agent `--debug` output adds
only the selected skill identifier.

Run the separate deterministic M8 evaluation with:

```powershell
python -m knowvia.m8_evaluation
```

It measures skill selection, relevant and irrelevant tool exposure, knowledge
scope, progressive disclosure, unintended execution, and preservation of the
confirmation safety boundary. It does not call Gemini, Ollama, Jira, or any
external service and does not modify the frozen M4–M7 evaluation datasets.

### M8 status and accepted limitations

M8 is complete and frozen. Its deterministic evaluation reached 100% skill
selection, relevant-tool exposure, knowledge-scope, progressive-disclosure,
and safety-preservation accuracy, with 0% irrelevant-tool exposure and 0%
unintended tool execution. The complete non-live M1–M8 regression suite passed
with 163 tests.

The following limitations are accepted for this milestone:

- source scoping uses local post-retrieval filtering rather than native vector
  store metadata filtering;
- selected skill instructions are isolated but are not injected into the
  frozen grounded-generation prompt;
- pending confirmation records do not include a skill identifier or skill
  version, so dynamic skill-policy versioning and revocation are not handled;
- cross-domain requests clarify instead of loading or coordinating multiple
  skills;
- the specialized HR, IT Support, and Finance skills remain a deliberately
  simplified enterprise taxonomy, with engineering and information security
  currently grouped under IT Support.

These limitations should be revisited only in a future explicitly approved
milestone. They are not reasons to expand or retune frozen M8 behavior.

### General company knowledge

Clear company-information questions that do not match a specialized HR, IT, or
Finance term use the knowledge-only `general_company_knowledge` capability.
Specialized matching still runs first. The general capability exposes no tools
and uses a fixed knowledge-only router, so it cannot propose or execute an
action.

Its retrieval scope is not permission to every document. Before retrieval,
Knowvia constructs an explicit source allowlist from the trusted identity's
existing `knowledge.hr.read`, `knowledge.it.read`, and
`knowledge.finance.read` permissions. Only sources belonging to authorized
domains are passed to `SourceScopedRetriever`. If no authorized source exists,
the request is denied before retrieval. The normal evidence-sufficiency and
supporting-evidence boundaries then decide whether to answer or abstain.

Explicit actions, model-like commands, and context-only phrases do not enter
this general knowledge path. Its current intent policy is deliberately
conservative and requires both a clear informational question form and company
or workplace context.

## Supporting evidence and source precision

Retrieval candidates and answer-supporting evidence are intentionally separate.
The retriever still returns the requested `top_k` candidates so retrieval recall
and diagnostics are preserved. The evidence evaluator makes the existing strict
`SUFFICIENT` or `INSUFFICIENT` decision and, for a sufficient decision, must also
identify the one-based retrieved evidence IDs that directly support the answer.
Those IDs are validated for type, uniqueness, range, and consistency with the
decision. Invalid selections fail closed.

Only the validated supporting chunks are sent to answer generation, and only
their programmatic filename/section metadata becomes `AnswerResult.sources`.
The complete candidate set remains available separately as
`AnswerResult.retrieval_results`; selected evidence is available as
`AnswerResult.supporting_results`. This preserves multi-source answers without
presenting unrelated top-k candidates as support.

For an insufficient decision, generation remains blocked and supporting sources
are empty. Retrieved candidates remain available internally for diagnostics but
are not presented as supporting citations. The M7 presenter remains a pure
presentation boundary and continues to label non-empty metadata as "Sources
consulted." M8 source scoping still occurs before evidence selection.

Historical M4 artifacts retain their original meaning and are not rewritten:
their source metrics describe the top-three retrieved candidate set. Supporting
source precision is evaluated separately in the newer deterministic answering
tests so historical retrieval baselines remain comparable.

## M8.6: conversational foundation

M8.6 adds bounded multi-turn context for knowledge conversations without making
conversation history authoritative evidence. Use the same explicit session ID
across CLI invocations:

```powershell
python -m knowvia.cli agent "How many vacation days do I get?" --session employee-demo
python -m knowvia.cli agent "What about sick leave?" --session employee-demo
```

`ConversationCoordinator` sits above `SkillAwareAgent`. It loads recent
structured state from the local SQLite `ConversationStore`, asks the
deterministic `FollowUpResolver` to produce a `ResolvedRequest`, and then sends a
standalone knowledge question through the existing Skills, scoped retrieval,
evidence-sufficiency, supporting-evidence, generation, and presentation
pipeline. Retrieval and evidence evaluation run again on every knowledge turn;
old answers and sources are never reused as evidence.

Only genuinely referential knowledge turns may inherit the most recent active
knowledge skill. Explicit domain terms in the current message take precedence,
so `What about sick leave?` switches from Finance to HR. Ambiguous references
clarify instead of inventing context. Conversation state is bounded by a recent
turn limit, a character budget, and inactivity expiry. Oversized turns are not
eligible for skill inheritance.

M8.6 intentionally does not implement conversational action follow-ups or
natural-language confirmation. Phrases such as `Yes, please`, `Do it`, or
`Create a ticket for that` are intercepted and clarified before routing; they
cannot create, confirm, or execute an action. Direct complete action requests
still use the unchanged M6 proposal flow, and the explicit `confirm <proposal>`
and `cancel <proposal>` commands remain authoritative. Conversation records are
not bound to Jira execution, employee identity, or authorization in this
milestone.

The M7 presenter remains deterministic and history-free. Normal responses do
not expose session internals; `--debug` may display the standalone resolved
request for development. The local SQLite implementation is suitable for this
learning milestone, not shared production session infrastructure.

## M8.7: knowledge request normalization

M8.7 adds a deterministic knowledge-only boundary after routing and before the
existing retrieval/evidence pipeline. Clear first-person problem reports such
as `My VPN isn't working` are framed as requests for documented company
guidance or troubleshooting. The original employee wording is preserved in the
normalized request; the normalizer does not add a device type, operating
system, error, location, credential, policy fact, or ticket detail.

Already-clear questions pass through unchanged. Explicit ticket requests also
pass through unchanged and remain on the existing ACTION path. If a model
incorrectly proposes an action for a mere problem statement, the M8.6
current-turn intent gate first changes the route to KNOWLEDGE and M8.7 then
creates only an informational request. Neither conversation history nor the
normalizer can supply a missing action verb or confirm an action.

The normalized request is sent through the selected skill's existing
`SourceScopedRetriever`, followed by fresh evidence evaluation, strict
supporting-evidence validation, grounded generation, and programmatic sources.
Contradictory or malformed evaluator output still fails closed. Direct `ask`
and `search` behavior is unchanged.

Debug mode displays the bounded normalization result as `Normalization` and
`Knowledge request`; normal employee responses remain controlled by the
deterministic M7 presenter. Run the separate deterministic evaluation with:

```powershell
python -m knowvia.m8_7_evaluation
```

The deterministic normalizer is intentionally conservative. It recognizes a
small family of explicit first-person failure phrases and leaves uncertain
statements unchanged. It is not a general semantic rewriting model, does not
diagnose technical faults, and cannot make undocumented troubleshooting
guidance sufficient.

## M9: local identity and permissions

M9 adds a trusted deterministic identity boundary above conversations, Skills,
knowledge retrieval, and actions. `IdentityContext` contains a stable subject,
display name, roles, explicit permissions, authentication state, and identity
source. Roles are descriptive; authorization ultimately checks permission
strings.

The current permissions are:

- `knowledge.hr.read`
- `knowledge.it.read`
- `knowledge.finance.read`
- `action.it_ticket.create_own`
- `action.it_ticket.read_own`

`LocalIdentityProvider` supplies fixed fictional identities for development.
The CLI option `--identity` selects one of those records; it is **not** proof of
identity and must not be treated as authentication, SSO, OAuth, or an enterprise
directory integration. Unknown identities fail closed, and there is no admin
fallback.

Knowledge permission is checked after lightweight skill selection but before
the complete skill is loaded or protected retrieval/generation runs. Action
tools are removed before router construction when the identity lacks their
permission. An explicit action is also denied deterministically before routing,
and the identity-aware action authorizer rechecks permission after routing.
Gemini receives only the employee request and already-filtered public tool
schemas; it never receives roles or permissions and cannot grant access.

Pending real-action proposals store their owning `subject_id`. Confirmation and
cancellation require the same subject. Confirmation checks ownership, current
permission, stored arguments, existing authorization, and then performs the
unchanged atomic one-time claim followed by immediate revalidation and
execution. Revoked permission leaves the proposal unexecuted. Action audit
events now include safe subject, identity-source, authentication, and
authorization-outcome metadata; secrets remain excluded.

Conversation rows are partitioned by both session ID and subject ID. Reusing a
session name as a different local identity therefore does not expose or inherit
the other identity's conversational context. Conversation text is never parsed
as identity or permission evidence, so claims such as `I am an admin` have no
authorization effect.

Development identities include:

- `employee-001`: HR, IT, and Finance knowledge plus own IT-ticket creation;
- `employee-002`: knowledge access only, with no ticket action permission;
- `it-employee-001`: IT knowledge and own IT-ticket creation;
- `hr-employee-001`: HR knowledge only.

Set the local default with `KNOWVIA_IDENTITY=employee-001` or select explicitly:

```powershell
python -m knowvia.cli agent "How many vacation days do I get?" --identity employee-001
```

Run the deterministic M9 evaluation with:

```powershell
python -m knowvia.m9_evaluation
```

M9 intentionally does not implement real authentication, SSO/OIDC, directory
group synchronization, resource-level ACLs, delegated administration, or a
production policy engine. The local identity selector is suitable only for
development and deterministic testing.

## M14.1: trusted employee-directory foundation

M14.1 adds a server-side SQLite employee directory without adding registration,
login, passwords, or authentication sessions. Each typed directory record has a
unique employee ID and case-normalized unique work email plus the employee's
name, department, organizational role, and active state. Verification succeeds
only when the supplied ID and work email identify the same active record; all
unknown, mismatched, inactive, and malformed inputs fail closed with a generic
verification error.

The directory deliberately stores no permissions. After verification,
`EmployeeDirectoryIdentityService` resolves the employee ID through the existing
M9 `IdentityProvider`; M9 remains the only source of roles used by authorization
and of explicit permissions. Directory department/role fields therefore cannot
grant access or elevate a caller. The four fictional NovaTech records returned
by `demo_employee_records()` are development/test seed data only and correspond
to the existing `employee-001`, `employee-002`, `it-employee-001`, and
`hr-employee-001` subjects.

M14.1 is only the directory foundation. It is not exposed as a registration API,
does not change the development identity selector, and is not production SSO or
identity proofing.

## M14.2: verified employee account registration

M14.2 introduced the registration service and SQLite account repository.
The current application exposes login and profile-controlled registration. A typed
registration request contains only employee ID, work email, password, and
password confirmation. The service first verifies the same active employee
through the M14.1 directory boundary and resolves the existing M9 identity; only
then can it create an account. Department, organizational role, permissions,
authorization identity, and admin state are not registration inputs.

The `accounts` table stores a stable account ID, unique employee ID,
case-insensitive unique normalized work email, Argon2id password hash,
timezone-aware creation time, and `ACTIVE`/`DISABLED` status. Plaintext passwords
are neither persisted nor included in safe `Account`/`RegistrationResult`
objects. Passwords use `argon2-cffi` and a bounded 12–128 character policy;
arbitrary composition rules are intentionally avoided. Duplicate employees or
emails fail atomically through database uniqueness constraints.

Later milestones added login sessions, cookies, authentication pages and account
administration. Password reset, email verification and SSO/OIDC remain unimplemented.

## M14.3: login authentication and server-side sessions

M14.3 authenticates registered accounts with their normalized work email and
Argon2id password hash, then revalidates the active account, active M14.1
employee record, and current M9 identity mapping. Successful login issues a
256-bit URL-safe bearer token in an HttpOnly, SameSite=Lax cookie. SQLite stores
only its SHA-256 digest, a separate session ID, account ID, creation/expiry
timestamps, and optional revocation timestamp. Sessions expire after eight
hours by default and can never outlive seven days.

`POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/logout`, and
`GET /api/auth/me` expose only safe account/employee data. Authorization
permissions are not copied into session records; every request resolves the
account back through the current directory and M9 provider. Unknown, malformed,
expired, revoked, disabled-account, inactive-employee, and unmapped-identity
sessions fail closed.

Production-style configuration defaults to Secure cookies and rejects browser-
supplied identities. Local HTTP development may explicitly set
`KNOWVIA_AUTH_COOKIE_SECURE=false` and
`KNOWVIA_ALLOW_DEVELOPMENT_IDENTITY_OVERRIDE=true`; the latter is never a
fallback for an invalid authentication cookie. Demo directory seeding is also
explicit through `KNOWVIA_SEED_DEMO_EMPLOYEE_DIRECTORY=true`.

The current application includes login/registration UI, a separate admin dashboard,
trusted account administration and login abuse controls. Public registration is
disabled in the secure Funnel profile. Password reset/change, MFA, email verification
and SSO/OIDC remain unimplemented.

## M10: ticket lifecycle and notification events

M10 records successfully created IT tickets in a local SQLite lifecycle store.
Each record contains the external ticket identifier, owning `subject_id`,
status, external system/reference, and creation/update timestamps. Supported
normalized states are `OPEN`, `IN_PROGRESS`, `RESOLVED`, `CLOSED`, and
`UNKNOWN`.

Ticket-status questions take a deterministic path before normal agent routing.
Knowvia extracts the ticket identifier, checks authentication and
`action.it_ticket.read_own`, verifies ownership from trusted local records, and
only then asks the configured status provider. Mock development mode reads the
stored state; Jira mode performs a read-only Jira issue lookup. The provider's
result is validated and ownership/permission are checked again before protected
status is returned. Gemini may recognize other requests, but it never supplies
ticket status or decides ownership.

Status changes are recorded atomically. A structured notification event is
created once for each observed transition, for example `OPEN` to
`IN_PROGRESS`. Reobserving the same state creates no duplicate notification.
M10 does not send email, chat, push, or background notifications; a future UI
or integration can consume these records.

Development examples:

```powershell
# Create and locally track a mock ticket.
python -m knowvia.cli agent "Create an IT ticket for my VPN problem." --identity employee-001 --router deterministic --tool mock

# Read its trusted stored status (replace the ID with the returned ID).
python -m knowvia.cli agent "What is the status of my ticket IT-0001?" --identity employee-001 --router deterministic --tool mock

# Show structured notification events owned by this identity.
python -m knowvia.cli notifications --identity employee-001

# A different identity is denied even if it knows the ticket ID.
python -m knowvia.cli agent "What is the status of my ticket IT-0001?" --identity it-employee-001 --router deterministic --tool mock
```

Run the deterministic M10 evaluation with:

```powershell
python -m knowvia.m10_evaluation
```

Current limitations are deliberate: lifecycle state is local single-machine
SQLite state; Jira status names use a small normalization map; Jira webhooks,
background polling, delivery channels, comments, assignees, and SLAs are not
implemented. Only tickets already tracked and bound to an owner can be queried.
The mock ticket counter is process-local, so its identifiers are demonstration
values rather than durable globally unique IDs. The local identity selector is
still a development aid, not real authentication.

## M11: MCP integration boundary

M11 adds the official Python MCP SDK as a standardized protocol boundary while
preserving Knowvia's existing `AgentTool` interface. A local MCP server exposes
two structured tools:

- `create_it_ticket(title, description)`
- `get_ticket_status(ticket_id)`

The server delegates to the existing mock or Jira implementation; MCP does not
replace Jira. `OfficialMCPClientTransport` performs discovery and calls through
the SDK, while `MCPAgentTool` maps discovered schemas and structured results
back into Knowvia's provider-neutral contracts. Schemas, required arguments,
tool names, result fields, result types, and success/error consistency are all
validated. Malformed results and unavailable servers fail closed.

Discovery is capability metadata, never authorization. Skill allowlists and
trusted identity permissions filter creation tools before router construction.
The orchestrator still enforces explicit current-turn action intent,
authorization, proposal ownership, confirmation, atomic claim, immediate
permission/argument revalidation, execution, lifecycle recording, and audit.
Ticket status remains on M10's deterministic path: local read-own permission and
ownership are checked before the MCP status call and revalidated afterward.
Gemini never decides identity, permission, ownership, confirmation, or status.

Use the local mock MCP backend:

```powershell
$env:KNOWVIA_MCP_TICKET_BACKEND = "mock"
python -m knowvia.cli agent "Create an IT ticket because my VPN is broken." --identity employee-001 --router deterministic --tool mcp
python -m knowvia.cli agent "What is the status of my ticket IT-0001?" --identity employee-001 --router deterministic --tool mcp
```

To place MCP over Jira, configure the existing Jira environment variables and:

```powershell
$env:KNOWVIA_MCP_TICKET_BACKEND = "jira"
python -m knowvia.cli agent "Create an IT ticket because my VPN is broken." --identity employee-001 --router deterministic --tool mcp
# The first command only creates a proposal. Use its existing confirmation command
# when you intentionally want the Jira side effect.
python -m knowvia.cli confirm <proposal-id> --identity employee-001 --tool mcp
```

Run the deterministic M11 evaluation with:

```powershell
python -m knowvia.m11_evaluation
```

M11 intentionally uses a local stdio MCP server and one ticket integration
domain. It does not add remote MCP authentication, Streamable HTTP deployment,
server trust registries, dynamic third-party server installation, multiple-tool
planning, or automatic retries. The stdio server is launched by Knowvia with a
trusted local identity binding; it is not intended as a separately exposed
authorization service.

## M12: bounded multi-step orchestration

M12 adds a small planning boundary above `SkillAwareAgent`. The default
`DeterministicMultiStepPlanner` recognizes single knowledge, single ticket
creation, trusted ticket-status lookup, and the explicit compound pattern where
an employee requests both troubleshooting guidance and ticket creation.

Plans contain typed, ordered steps and are validated before any step runs. A
plan may contain at most three steps, may use only `KNOWLEDGE`,
`CREATE_IT_TICKET`, and `GET_TICKET_STATUS`, and may not repeat a capability.
Unknown, malformed, looping, over-limit, or model-injected action plans fail
closed. Ticket creation is valid only when both the original employee request
and the action step contain explicit current-turn ticket intent.

Every step re-enters the existing runtime independently:

```text
validated plan step
→ Skill selection and source/tool filtering
→ trusted identity and permission checks
→ existing RAG, ticket creation, or status path
→ existing evidence/ownership/confirmation/audit boundaries
```

Knowledge uses fresh retrieval, evidence evaluation, supporting-evidence
selection, generation, and citations. Generated answers are never converted
into tool arguments. For a compound VPN request, the ticket step is rebuilt
only from the problem stated in the original employee request. Jira-backed
creation still stops at a proposal and requires the unchanged explicit
confirmation command. MCP and direct tools remain interchangeable beneath the
same step contract.

Example with the local mock tool:

```powershell
python -m knowvia.cli agent "My VPN isn't working. Give me the troubleshooting guidance and help me create a ticket." --identity employee-001 --router deterministic --tool mock --debug
```

Example that must stop at Jira confirmation:

```powershell
python -m knowvia.cli agent "My VPN isn't working. Give me the troubleshooting guidance and help me create a ticket." --identity employee-001 --router deterministic --tool jira
```

The same plan can use MCP:

```powershell
$env:KNOWVIA_MCP_TICKET_BACKEND = "mock"
python -m knowvia.cli agent "My VPN isn't working. Give me the troubleshooting guidance and help me create a ticket." --identity employee-001 --router deterministic --tool mcp
```

Run the deterministic evaluation with:

```powershell
python -m knowvia.m12_evaluation
```

M12 deliberately does not add open-ended model planning, loops, parallel
execution, new capabilities, cross-step generated-data injection, or automatic
confirmation. The deterministic planner recognizes a narrow compound-request
grammar; ambiguous combinations fail safely or continue through the existing
single-step clarification path. Multi-step requests are based on the current
utterance and do not yet participate in conversational follow-up resolution.

## M13: employee web application

M13 adds a small Starlette web boundary and a modular HTML/CSS/JavaScript
client. It deliberately does not duplicate agent decisions in the browser.

```text
Employee browser
  → bounded HTTP request
  → KnowviaWebService
  → ConversationCoordinator / MultiStepOrchestrator
  → existing M1–M12 safety and capability layers
  → structured result
  → deterministic employee presentation
  → safe UI response model
```

The UI includes conversation navigation, responsive chat, source cards,
proposal cards with explicit Create/Cancel controls, ticket results,
notifications, and an opt-in developer-details view. Normal responses never
expose skill definitions, permission names, evidence IDs, prompts, credentials,
or provider diagnostics. Developer details are deliberately limited to safe
high-level labels such as selected skill, capability, grounded/abstained state,
transport, and authorization outcome.

The browser stores display history locally for navigation. Authoritative
multi-turn state remains in the existing SQLite conversation store and is
bound to the trusted development identity. Clearing browser storage does not
alter tickets, proposals, audits, or server-side conversation state.

### Install and index

Python 3.11 or newer is required. From PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,gemini]"
Copy-Item .env.example .env
.venv\Scripts\python.exe -m knowvia.cli index
```

Local embeddings download once on first use. Groq is the default inference provider
and requires its server-side API key; Ollama is needed only when explicitly selected.
The public demonstration uses Gemini routing; the deterministic router remains available.

### Start the employee application

For a side-effect-free local demonstration, use the deterministic router and
mock ticket tool in `.env`:

```text
KNOWVIA_AGENT_ROUTER=deterministic
KNOWVIA_AGENT_TOOL=mock
KNOWVIA_IDENTITY=employee-001
```

Then start the application:

```powershell
.venv\Scripts\python.exe -m knowvia.web
```

Open `http://127.0.0.1:8000`. The host and port can be changed with
`KNOWVIA_WEB_HOST` and `KNOWVIA_WEB_PORT`.

### Tool configurations

- `KNOWVIA_AGENT_TOOL=mock` uses the in-memory demonstration ticket tool.
- `KNOWVIA_AGENT_TOOL=jira` uses the existing Jira integration. Creation still
  stops at a proposal and requires an explicit UI confirmation.
- `KNOWVIA_AGENT_TOOL=mcp` uses the local validated MCP adapter. Select its
  underlying implementation with `KNOWVIA_MCP_TICKET_BACKEND=mock` or `jira`.

Jira values (`JIRA_BASE_URL`, `JIRA_USER_EMAIL`, `JIRA_API_TOKEN`,
`JIRA_PROJECT_KEY`, and `JIRA_ISSUE_TYPE`) belong only in `.env`. Never commit
real credentials. No credential is sent to Gemini or returned to the browser.
Ambiguous network failures are not automatically retried for ticket creation.

Normal web requests use the M14 server-backed authenticated session and derive
their identity through the trusted employee-directory/M9 boundary. The old
identity menu remains available only when the explicit development override is
enabled; it is not authentication and is forbidden in demo/production modes.

### Security and safety boundaries

- The browser cannot choose or execute an arbitrary tool; it submits employee
  text or confirms/cancels an existing proposal ID.
- All tools are filtered by selected skill and trusted permissions, then
  validated again at execution.
- Ticket creation preserves proposal, confirmation, atomic claim, permission
  revalidation, execution, and audit ordering.
- Ticket status comes from trusted lifecycle/tool data and enforces ownership.
- Knowledge answers use fresh scoped retrieval, evidence sufficiency, validated
  supporting evidence, and programmatic source metadata.
- Conversation history cannot grant identity, permissions, action intent, or
  confirmation.
- MCP discovery does not grant authorization, and malformed MCP results fail
  closed.

### Testing

Run the focused M13 integration tests:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_web.py tests/test_m13_integration.py -v
```

Run the complete deterministic suite without live providers:

```powershell
.venv\Scripts\python.exe -m pytest -m "not live and not ollama_live and not gemini_live and not jira_live and not m3_live and not m4_live and not m5_live"
```

Live Gemini and Jira checks remain explicit, separate operations and are never
part of the deterministic UI suite.

## M15 deployment safety configuration

Knowvia has four explicit application environments through
`KNOWVIA_ENVIRONMENT`: `development`, `test`, `demo`, and `production`. A
missing value defaults to `production`, not development. The environment name
selects a validation profile; it does not grant authorization or enable an
external capability by itself.

### Local development

Local HTTP development must be explicit. A typical private workstation uses:

```dotenv
KNOWVIA_ENVIRONMENT=development
KNOWVIA_ALLOWED_HOSTS=localhost,127.0.0.1,testserver
KNOWVIA_AUTH_COOKIE_SECURE=false
KNOWVIA_ALLOW_DEVELOPMENT_IDENTITY_OVERRIDE=true
KNOWVIA_SEED_DEMO_EMPLOYEE_DIRECTORY=true
KNOWVIA_REGISTRATION_ENABLED=true
KNOWVIA_ALLOW_DEVELOPER_DETAILS=true
KNOWVIA_AGENT_TOOL=mock
KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS=false
```

The identity override and developer details are unsafe conveniences and must
not be carried into a public deployment. Real Jira remains available for a
private development or future enterprise environment only when both the Jira
backend and `KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS=true` are configured. Jira
credentials alone never activate it. An unapproved Jira selection in local
development is safely downgraded to mock ticketing; demo/production fail fast.

### Public portfolio demo

A portfolio deployment uses fictional NovaTech records and local ticketing:

```dotenv
KNOWVIA_ENVIRONMENT=demo
KNOWVIA_ALLOWED_HOSTS=knowvia-demo.example.com
KNOWVIA_AUTH_COOKIE_SECURE=true
KNOWVIA_ALLOW_DEVELOPMENT_IDENTITY_OVERRIDE=false
KNOWVIA_SEED_DEMO_EMPLOYEE_DIRECTORY=true
KNOWVIA_REGISTRATION_ENABLED=true
KNOWVIA_ALLOW_DEVELOPER_DETAILS=false
KNOWVIA_AGENT_TOOL=mock
KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS=false
KNOWVIA_ENABLE_HSTS=true
```

This registration-enabled example is for an explicitly opted-in fictional sandbox,
not the secure Funnel profile, which disables registration and automatic seeding.
Sandbox registration verifies only the seeded fictional directory; visitors do
not choose roles or permissions. Demo ticket creation retains proposal and
confirmation, produces `DEMO_IT-*` local identifiers, and never contacts
Atlassian. A visitor creates their own password (12–128 characters) using one
of these explicitly fictional ID/email pairs:

| Demo employee ID | Fictional work email |
| --- | --- |
| `employee-001` | `amina.khan@novatech.example` |
| `employee-002` | `daniel.reed@novatech.example` |
| `it-employee-001` | `priya.shah@novatech.example` |
| `hr-employee-001` | `maya.chen@novatech.example` |

These `.example` addresses do not receive mail and represent no real employee.
Do not expose the development identity switcher in a portfolio deployment.

### Production and future enterprise use

`production` requires secure cookies and rejects development identity override,
demo directory seeding, debug mode, and developer-detail exposure. Registration
is disabled unless explicitly configured. A real enterprise deployment still
needs managed SSO/OIDC, a real employee-directory integration, managed secrets,
TLS termination, rate limiting, operational monitoring, database backups, and
deployment-specific origin/host configuration.

Set `KNOWVIA_ENABLE_HSTS=true` only when the public host is permanently served
over HTTPS; it is intentionally invalid for local development. Every response
receives a conservative CSP, frame protection, `nosniff`, and no-referrer
policy. `GET /health` returns only `{"status":"ok"}` and does not initialize
the RAG, Gemini, Jira, MCP, employee, or account services.

### Secrets

Gemini/OpenAI keys, Jira tokens and account details remain server-side in the
deployment secret store or an ignored local `.env`. Never place secrets in
HTML, JavaScript, command-line arguments, committed configuration, screenshots,
or support logs. `.env.example` contains placeholders only. `/api/config` is a
small UI-capability response and never returns provider credentials, session
tokens, password data, or permission internals.

## M15.2 production packaging and hosting readiness

Knowvia supports Python 3.11 or newer; the production container and current
acceptance environment use Python 3.12. The packaged ASGI application is
`knowvia.web:app`. A conventional hosting platform should start exactly one
process with:

```text
python -m knowvia.production
```

That entrypoint binds Uvicorn to `0.0.0.0`, uses the platform-provided `PORT`
(default `8000`), disables reload, fixes the worker count at one, validates the
M15.1 configuration, initializes the persistent data root, idempotently copies
the bundled vector index when necessary, and self-checks `/health` before
serving. Multiple workers or replicas must not share these SQLite files; use a
single instance until the persistence layer is replaced by a multi-process
database.

### Runtime resources and persistence

Set `KNOWVIA_DATA_DIR` to a mounted persistent volume. It contains:

- `employee_directory.db`, `accounts.db`, and `authenticated_sessions.db`;
- `conversations.db` and `pending_actions.db`;
- `ticket_lifecycle.db` and `agent_audit.jsonl`;
- the runtime `knowvia_index.json` vector index.

The process fails clearly if this directory cannot be created and written.
The browser cannot influence any filesystem path. Local development defaults
to the repository `data/` directory.

The eight Markdown policy documents in `knowvia_m1_dataset/`, the web assets in
`src/knowvia/static/`, and the validated seed `data/knowvia_index.json` are
immutable build inputs. On an empty volume, the production bootstrap copies the
seed index once. Existing valid runtime indexes are never overwritten or
rebuilt at startup, and `/health` never initializes retrieval or a model. Run
the same checks manually with:

```text
python -m knowvia.deployment_readiness
```

### Model and cache behavior

The Docker image pre-downloads
`sentence-transformers/all-MiniLM-L6-v2` into `/opt/knowvia/model-cache` during
the image build. Model binaries are not committed to Git and are not downloaded
on each service restart. The runtime index records the same embedding model;
the existing retriever continues to reject a model/index mismatch.

Gemini routing and Groq inference keys must be supplied through server-side secrets.
Evidence evaluation and grounded generation default to Groq GPT-OSS 120B, so public
startup does not require Ollama. Provider errors fail closed without automatic fallback.

### Container build and run

The image uses `python:3.12-slim`, exact direct production dependency versions,
a non-root UID, a Python-based `/health` check, and no copied `.env` file. Build
it with:

```text
docker build -t knowvia:demo .
```

At runtime, inject secrets through the hosting platform and mount persistent
storage at `/var/lib/knowvia`. `deploy/demo.env.example` documents non-secret
public-demo settings and secret placeholders. Do not pass that file unchanged:
replace the example hostname and supply Gemini/Groq settings through the
server-side platform configuration.

A public demo must keep `KNOWVIA_AGENT_TOOL=mock`,
`KNOWVIA_MCP_TICKET_BACKEND=mock`, and
`KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS=false`. Demo actions retain confirmation
and produce fictional `DEMO_IT-*` tickets. Never mount or inject Jira
credentials into the public-demo service.

### Hosting constraints

- One web process and one replica while SQLite/local JSON persistence is used.
- A persistent writable volume is required; ephemeral-only filesystems lose
  accounts, sessions, conversations, proposals, tickets, notifications, audit
  records, and the runtime index on restart.
- Container image builds require enough memory/network capacity to install
  Torch/Sentence Transformers and download the embedding model.
- Runtime knowledge answers require access to Groq by default; only explicitly
  selected Ollama requires an Ollama endpoint. Gemini routing is separate.
- TLS termination, domain/DNS setup, managed secrets, backups, abuse controls,
  logging/metrics, and provider-specific deployment manifests remain M15.3+.

### Gemini routing resilience

When Gemini routing is configured, Gemini remains the primary semantic router.
Its existing bounded retry runs first. If routing still fails because of a
transient connection, timeout, rate-limit, or provider 5xx condition, Knowvia
invokes the deterministic router exactly once with the same selected skill and
the same permission-filtered tool schemas. Valid Gemini decisions—including
`UNSUPPORTED`—are never overridden, and malformed responses, authentication
errors, invalid requests, and unknown failures do not trigger fallback.

The fallback only proposes an ordinary validated `AgentDecision`. It cannot
execute tools or access Jira/MCP. Knowledge requests still pass through scoped
retrieval and evidence validation. Action requests still require explicit
current-turn intent, tool validation, authorization, and—where configured—the
existing proposal and confirmation lifecycle. Sanitized routing provenance is
available to tests and diagnostics without retaining request text, prompts,
responses, credentials, or headers.

Run the isolated deterministic resilience evaluation with:

```powershell
.venv\Scripts\python.exe -m knowvia.routing_resilience_evaluation
```

### M13 limitations

- Identity selection is demonstration-only and must not be treated as SSO.
- The UI server is a local development server, not a hardened public deployment.
- Browser conversation navigation is local to that browser; authoritative
  context is still bounded and stored in the existing SQLite session store.
- Notifications are structured local events; there is no email, push, Slack,
  websocket, or background Jira polling service.
- The deterministic multi-step planner supports only the bounded M12 capability
  set; there is no autonomous loop or general-purpose planning.
- Source cards represent validated supporting chunks, not sentence-level
  attribution.
- Real Jira and Gemini behavior depends on separately configured services and
  is not exercised by automated UI tests.
- Routing fallback is per request and does not yet include a circuit breaker or
  a second hosted provider. The configured SDK timeout is not guaranteed to be
  a strict end-to-end wall-clock deadline.

See [docs/DEMO_GUIDE.md](docs/DEMO_GUIDE.md) for the final thesis demo flow.

## M15.3 repository publication safety

Before staging or publishing repository contents, run:

```powershell
.venv\Scripts\python.exe -m knowvia.repository_safety
```

The check uses Git's tracked and non-ignored file set when Git metadata is
available. Before Git is initialized it applies this repository's `.gitignore`
as a conservative filesystem fallback. It reports only file paths and finding
categories; matched credential values are never printed. A non-zero exit code
means unsafe publishable content was found.

Local `.env` files, SQLite runtime state, conversations, accounts, sessions,
audit logs, model caches, test/build output, and generated artifacts are not
publication content. The deterministic `data/knowvia_index.json` seed remains
publishable because M15.2 uses it to bootstrap a fresh runtime data directory.

## Accepted inference verification (October 14, 2026)

Normal CLI/web environment selection now defaults to Groq `openai/gpt-oss-120b`
for evidence evaluation and grounded generation. Set the server-side `GROQ_API_KEY`
in the gitignored `.env`; missing credentials fail closed. Gemini routing is unchanged.
Ollama remains available locally by explicitly setting both
`KNOWVIA_GENERATION_PROVIDER=ollama` and `KNOWVIA_EVIDENCE_EVALUATOR=ollama`.
There is no automatic fallback. The historical 57-turn result remains unchanged;
the final authorized-IT fix and three safety contrasts passed separately.
