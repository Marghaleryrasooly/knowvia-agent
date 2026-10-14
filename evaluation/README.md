# Knowvia evaluation and regression testing

Knowvia tests both model quality and deterministic application boundaries.
Versioned datasets describe expected behavior; offline tests exercise parsing,
permissions, conversation ownership and execution safety without hosted inference.
Explicit live runs measure evidence decisions, grounded responses, sources, latency
and provider failures. Passing offline tests does not prove live model quality.

## Evaluation coverage

- **RAG reliability and grounded answers:** supported facts and procedures,
  paraphrases, insufficient knowledge and safe abstention.
- **Evidence and sources:** answerability versus topical relevance, validated
  supporting IDs, citation precision and preserved conditional requirements.
- **Routing and action safety:** knowledge/action/clarification selection,
  proposal validation and explicit confirmation before execution.
- **Identity and authorization:** permission denials, ownership and identity isolation.
- **Skills and orchestration:** skill-specific sources/tools and bounded multi-step plans.
- **MCP integration:** discovery, schemas, results, failures and confirmation boundaries.
- **Conversation and resilience:** clarification answers, topic changes, contextual
  requests, session isolation and constrained routing failure behavior.
- **Authenticated acceptance:** realistic API conversations with temporary state
  and mock-only actions, using an unchanged 57-turn matrix across 41 scenarios.

Focused tests under [tests/](../tests/) add regressions beyond the dataset cases,
including current evidence scopes, admin security and atomic knowledge publication.

## Dataset catalog

Runner links point to implementations, not instructions to execute live inference.
Some runners require explicit live authorization and configured provider access.

| Existing filename | Descriptive evaluation name | What it tests | Evaluation runner |
| --- | --- | --- | --- |
| [knowvia_m4_eval_v1.json](knowvia_m4_eval_v1.json) | Grounded RAG | 50-case retrieval, answer support, abstention and source evaluation | [m4_evaluation.py](../src/knowvia/m4_evaluation.py) |
| [knowvia_m5_eval_v1.json](knowvia_m5_eval_v1.json) | Agent routing | Knowledge/action/clarification decisions and execution separation | [m5_evaluation.py](../src/knowvia/m5_evaluation.py) |
| [knowvia_m6_eval_v1.json](knowvia_m6_eval_v1.json) | Routing and action safety | Semantic route proposals, arguments and action safeguards | [m6_evaluation.py](../src/knowvia/m6_evaluation.py) |
| [knowvia_m7_eval_v1.json](knowvia_m7_eval_v1.json) | Response presentation | Knowledge, abstention and proposal/terminal action presentations | [m7_evaluation.py](../src/knowvia/m7_evaluation.py) |
| [knowvia_m8_eval_v1.json](knowvia_m8_eval_v1.json) | Skill selection | Selected skills, source allowlists and available tools | [m8_evaluation.py](../src/knowvia/m8_evaluation.py) |
| [knowvia_m8_7_eval_v1.json](knowvia_m8_7_eval_v1.json) | Knowledge normalization | Informal problem requests without invented facts or action intent | [m8_7_evaluation.py](../src/knowvia/m8_7_evaluation.py) |
| [knowvia_m9_eval_v1.json](knowvia_m9_eval_v1.json) | Identity and permissions | Authorized/denied knowledge and actions, trusted identities | [m9_evaluation.py](../src/knowvia/m9_evaluation.py) |
| [knowvia_m10_eval_v1.json](knowvia_m10_eval_v1.json) | Ticket lifecycle | Ticket ownership, status transitions, notifications and permission denial | [m10_evaluation.py](../src/knowvia/m10_evaluation.py) |
| [knowvia_m11_eval_v1.json](knowvia_m11_eval_v1.json) | MCP safety | Discovery, schemas, malformed results, server failures and confirmation | [m11_evaluation.py](../src/knowvia/m11_evaluation.py) |
| [knowvia_m12_eval_v1.json](knowvia_m12_eval_v1.json) | Multi-step orchestration | Ordered bounded plans, grounding, authorization and execution gates | [m12_evaluation.py](../src/knowvia/m12_evaluation.py) |
| [knowvia_knowledge_paraphrase_eval_v1.json](knowvia_knowledge_paraphrase_eval_v1.json) | Knowledge reliability | 40-case policy support, natural wording, distractors and abstention | [knowledge_reliability_evaluation.py](../src/knowvia/knowledge_reliability_evaluation.py) |
| [knowvia_routing_resilience_eval_v1.json](knowvia_routing_resilience_eval_v1.json) | Routing resilience | Primary success, transient/permanent failures and bounded fallback safety | [routing_resilience_evaluation.py](../src/knowvia/routing_resilience_evaluation.py) |
| [knowvia_local_acceptance_v1.json](knowvia_local_acceptance_v1.json) | Authenticated acceptance | 57 turns: evidence, permissions, context, clarification, confirmation and isolation | [local_acceptance.py](local_acceptance.py) |

## Naming and reproducibility

M4, M8, M12 and similar filename labels are historical internal milestone identifiers,
not product capabilities or required reading. The descriptive catalog above provides
the current navigation labels. Original filenames, dataset versions, case IDs and
embedded paths remain unchanged for loader compatibility, benchmark reproducibility
and historical integrity checks. No renamed copies or alternative scoring expectations
are introduced.

The acceptance runner incorporates selected cases from the 40-case paraphrase
benchmark alongside directly declared multi-turn scenarios. Its expanded matrix is
41 scenarios / 57 turns; the number of top-level scenario entries alone is not the
complete acceptance workload.

## Methodology and results

See [LOCAL_ACCEPTANCE.md](LOCAL_ACCEPTANCE.md) for grading, provider selection,
safe pacing, token accounting, checkpoints and explicit interrupted-scenario replay.
The original Ollama reference and subsequent Groq measurements remain distinct.
Measurement completion is not automatic provider-quality acceptance.

The [project README](../README.md#testing-and-evaluation) summarizes verified
historical results and separately identifies later targeted checks. It does not
claim universal accuracy or a full-suite rerun after every implementation change.
Generated reports remain in ignored `artifacts/evaluation/`; private transcripts,
credentials, session tokens and raw sensitive provider responses are not publication data.

## Supporting harnesses

- [groq_safeguards.py](groq_safeguards.py): inference pacing, rolling budgets,
  atomic checkpoints, safe stops and protected-reference checks.
- [groq_smoke.py](groq_smoke.py): an explicitly authorized authenticated VPN smoke test.
- [gemini_smoke.py](gemini_smoke.py): isolated provider diagnostics and sanitized results.

Live calls require separate approval and available provider quota. Do not run them
as part of an ordinary documentation check; do not enable real external actions.
