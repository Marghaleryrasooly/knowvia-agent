# Knowvia technical documentation

Start with the [project README](../README.md) for the current system and quick start.
The implementation is the authority for behavior; development-history notes are
not a substitute for current configuration or security checks.

## Current operational guides

| Guide | Contents |
| --- | --- |
| [Administration](administration.md) | Private bootstrap, trusted roles, account APIs, origin checks and session revocation |
| [Knowledge management](admin-knowledge-management.md) | Markdown staging, source access, publication validation, versions and rollback |
| [Tailscale HTTPS demonstration](tailscale-demo.md) | Persistent Windows storage, secure process profile, startup and acceptance checklist |
| [Evaluation catalog](../evaluation/README.md) | Dataset purposes, runners, reproducibility and offline versus live coverage |
| [Acceptance methodology](../evaluation/LOCAL_ACCEPTANCE.md) | Immutable scenarios, historical references, provider diagnostics and safe pacing/resume |
| [Fictional corpus](../knowvia_m1_dataset/README.md) | NovaTech source documents and retrieval reference questions |

Current operational guides describe Groq evidence/generation, Gemini routing,
implemented knowledge management and disabled public registration/real Jira mutations.
Historical investigation notes are labeled separately; no historical evaluation
report is silently relabeled or replaced.

## Architecture reference

| Concern | Source entry points |
| --- | --- |
| Web/API and UI | [web.py](../src/knowvia/web.py), [static assets](../src/knowvia/static/) |
| Trusted authority | [admin.py](../src/knowvia/admin.py), [authentication.py](../src/knowvia/authentication.py), [authorization.py](../src/knowvia/authorization.py) |
| Conversation and skills | [conversation.py](../src/knowvia/conversation.py), [skill_orchestration.py](../src/knowvia/skill_orchestration.py) |
| Evidence and conditions | [evidence.py](../src/knowvia/evidence.py), [condition_preservation.py](../src/knowvia/condition_preservation.py) |
| Retrieval and inference | [retrieval.py](../src/knowvia/retrieval.py), [embeddings.py](../src/knowvia/embeddings.py), [generation.py](../src/knowvia/generation.py) |
| Action safety | [confirmation.py](../src/knowvia/confirmation.py), [mcp_integration.py](../src/knowvia/mcp_integration.py) |
| Knowledge publication | [knowledge_management.py](../src/knowvia/knowledge_management.py) |
| HTTPS launcher | [funnel_start.py](../deploy/funnel_start.py) |

## Detailed development reference

[Development history](development-history.md) preserves the previous long README,
including design decisions, CLI examples, evidence contracts, regression work and
internal milestone numbering. Historical relative links assume the repository root.
Use it for rationale and evolution, not to infer current defaults or missing features.

The [ONNX deployment reference](development-history.md#optional-pinned-onnx-embedding-deployment)
contains asset preparation and parity instructions for the optional FP32 provider.
The [earlier hosting assessment](hosting-options-history.md) and inactive Render recipe
remain references, not the selected persistent deployment. The secure public demo
uses Tailscale Funnel and the host's existing private storage.

The [historical local demo reference](local-demo-reference.md) preserves example
scenarios, not current login or deployment instructions.

Generated evaluation artifacts, accounts, uploads, session data and secrets stay
outside Git. Published test fixtures contain synthetic data, not public credentials.
