from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from knowvia.answer_grading import DeterministicAnswerGrader
from knowvia.answering import GROUNDING_SYSTEM_PROMPT, build_grounded_user_prompt, citations_from_results
from knowvia.cli import DEFAULT_INDEX, create_embedder, create_evidence_evaluator, create_generator
from knowvia.eval_dataset import EvaluationCase, load_evaluation_dataset
from knowvia.general_knowledge import DeterministicGeneralKnowledgeIntentPolicy, GENERAL_COMPANY_KNOWLEDGE_SKILL_ID, GeneralKnowledgeIntentKind
from knowvia.identity import LocalIdentityProvider
from knowvia.knowledge_normalization import DeterministicKnowledgeRequestNormalizer
from knowvia.models import EvidenceStatus, RetrievalResult
from knowvia.permissions import IdentityKnowledgeSourcePolicy
from knowvia.retrieval import Retriever
from knowvia.skill_orchestration import SourceScopedRetriever
from knowvia.skills import DeterministicSkillSelector, SkillSelectionStatus, create_default_skill_registry
from knowvia.vector_store import JsonVectorStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "evaluation" / "knowvia_knowledge_paraphrase_eval_v1.json"
DEFAULT_DOCUMENTS = PROJECT_ROOT / "knowvia_m1_dataset"
DEFAULT_ARTIFACTS = PROJECT_ROOT / "artifacts" / "evaluation"


def _scope(question: str, identity):
    registry = create_default_skill_registry()
    selection = DeterministicSkillSelector().select(question, registry.discover())
    if selection.status is SkillSelectionStatus.UNSUPPORTED:
        intent = DeterministicGeneralKnowledgeIntentPolicy().classify(question)
        if intent.kind is not GeneralKnowledgeIntentKind.KNOWLEDGE:
            raise ValueError("Evaluation question did not enter a knowledge capability")
        skill_id = GENERAL_COMPANY_KNOWLEDGE_SKILL_ID
    elif selection.status is SkillSelectionStatus.SELECTED and selection.skill_id:
        skill_id = selection.skill_id
    else:
        raise ValueError("Evaluation question selected an ambiguous skill")
    skill = registry.load(skill_id)
    if skill_id != GENERAL_COMPANY_KNOWLEDGE_SKILL_ID:
        return skill_id, skill.knowledge_sources
    authorized = frozenset(IdentityKnowledgeSourcePolicy().authorized_sources(identity))
    return skill_id, tuple(source for source in skill.knowledge_sources if source in authorized)


def _source_set(values) -> set[tuple[str, str]]:
    return {(value.source, value.section) for value in values}


def _case_payload(case: EvaluationCase, retrieved: list[RetrievalResult], decision, diagnostics, supporting, answer, grade, skill_id, sources, query):
    retrieved_citations = citations_from_results(retrieved)
    supporting_citations = citations_from_results(supporting)
    expected = _source_set(case.expected_sources)
    selected = _source_set(supporting_citations)
    return {
        "id": case.id,
        "question": case.question,
        "tags": list(case.tags),
        "expected_behavior": case.expected_behavior,
        "skill": skill_id,
        "source_allowlist": list(sources),
        "retrieval_query": query,
        "retrieved": [
            {"evidence_id": index, "chunk_id": result.chunk.id, "source": result.chunk.source, "section": result.chunk.section, "score": result.score}
            for index, result in enumerate(retrieved, 1)
        ],
        "retrieval_hit": bool(expected & _source_set(retrieved_citations)) if expected else None,
        "actual_status": decision.status.value,
        "reason": decision.reason,
        "supporting_evidence_ids": list(decision.supporting_evidence_ids),
        "supporting_sources": [asdict(source) for source in supporting_citations],
        "supporting_precision": (len(expected & selected) / len(selected)) if selected else (1.0 if not expected else 0.0),
        "complete_support": selected == expected if expected else not selected,
        "answer": answer,
        "answer_grade": asdict(grade) if grade else None,
        "evidence_diagnostics": {
            **asdict(diagnostics),
            "final_decision": diagnostics.final_decision.value,
        },
    }


def calculate_metrics(cases: list[dict]) -> dict:
    answerable = [case for case in cases if case["expected_behavior"] == "ANSWER"]
    abstain = [case for case in cases if case["expected_behavior"] == "ABSTAIN"]
    answered_expected = [case for case in answerable if case["actual_status"] == "SUFFICIENT"]
    answered_all = [case for case in cases if case["actual_status"] == "SUFFICIENT"]
    support_values = [case["supporting_precision"] for case in answered_expected]
    grade_values = [case["answer_grade"]["passed"] for case in answered_expected if case["answer_grade"]]
    validation_failures = [
        case for case in cases
        if case["evidence_diagnostics"]["validation_error_message"] is not None
    ]
    return {
        "case_count": len(cases),
        "answerable_count": len(answerable),
        "abstention_count": len(abstain),
        "answerable_paraphrase_consistency": len(answered_expected) / len(answerable),
        "abstention_precision": sum(case["actual_status"] == "INSUFFICIENT" for case in abstain) / len(abstain),
        "unsafe_answer_rate": sum(case["actual_status"] == "SUFFICIENT" for case in abstain) / len(abstain),
        "over_abstention_rate": sum(case["actual_status"] == "INSUFFICIENT" for case in answerable) / len(answerable),
        "retrieval_recall_at_3": sum(bool(case["retrieval_hit"]) for case in answerable) / len(answerable),
        "supporting_source_precision": sum(support_values) / len(support_values) if support_values else 0.0,
        "complete_support_accuracy": sum(case["complete_support"] for case in answered_expected) / len(answered_expected) if answered_expected else 0.0,
        "conditional_answer_accuracy": sum(grade_values) / len(grade_values) if grade_values else 0.0,
        "end_to_end_answer_success": sum(bool(case["answer_grade"] and case["answer_grade"]["passed"] and case["complete_support"]) for case in answerable) / len(answerable),
        "grounded_answer_rate": sum(bool(case["answer_grade"] and case["answer_grade"]["passed"] and case["supporting_precision"] == 1.0) for case in answered_all) / len(answered_all) if answered_all else 0.0,
        "validation_failure_count": len(validation_failures),
    }


def run(dataset_path: Path, output: Path, top_k: int = 3) -> dict:
    dataset = load_evaluation_dataset(dataset_path, DEFAULT_DOCUMENTS)
    identity = LocalIdentityProvider().resolve("employee-001")
    retriever = Retriever(create_embedder("local"), JsonVectorStore(DEFAULT_INDEX))
    evaluator = create_evidence_evaluator("ollama")
    generator = create_generator("ollama")
    normalizer = DeterministicKnowledgeRequestNormalizer()
    grader = DeterministicAnswerGrader()
    outcomes = []
    for index, case in enumerate(dataset.cases, 1):
        print(f"[{index:02d}/{len(dataset.cases)}] {case.id}: {case.question}", flush=True)
        skill_id, sources = _scope(case.question, identity)
        normalized = normalizer.normalize(case.question)
        query = normalized.normalized_request
        retrieved = SourceScopedRetriever(retriever, sources).search(query, top_k)
        decision, diagnostics = evaluator.evaluate_with_diagnostics(query, retrieved)
        supporting = [retrieved[evidence_id - 1] for evidence_id in decision.supporting_evidence_ids]
        answer = None
        grade = None
        if decision.status is EvidenceStatus.SUFFICIENT:
            answer = generator.generate(GROUNDING_SYSTEM_PROMPT, build_grounded_user_prompt(query, supporting))
            if case.expected_behavior == "ANSWER":
                grade = grader.grade(case, answer)
        outcomes.append(_case_payload(case, retrieved, decision, diagnostics, supporting, answer, grade, skill_id, sources, query))
    payload = {
        "schema_version": 1,
        "run": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dataset_name": dataset.name,
            "dataset_version": dataset.version,
            "top_k": top_k,
            "embedding_model": os.getenv("LOCAL_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
            "generation_model": os.getenv("OLLAMA_GENERATION_MODEL", "llama3.2:latest"),
            "evidence_model": os.getenv("OLLAMA_EVIDENCE_MODEL", os.getenv("OLLAMA_GENERATION_MODEL", "llama3.2:latest")),
        },
        "metrics": calculate_metrics(outcomes),
        "cases": outcomes,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def compare(before: Path, after: Path) -> dict:
    old = json.loads(before.read_text(encoding="utf-8"))
    new = json.loads(after.read_text(encoding="utf-8"))
    if (old["run"]["dataset_name"], old["run"]["dataset_version"]) != (new["run"]["dataset_name"], new["run"]["dataset_version"]):
        raise ValueError("Evaluation artifacts use different datasets")
    return {key: {"before": old["metrics"][key], "after": new["metrics"][key], "delta": new["metrics"][key] - old["metrics"][key]} for key in old["metrics"] if isinstance(old["metrics"][key], (int, float))}


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Evaluate knowledge paraphrase reliability")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--top-k", type=int, default=3)
    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("before", type=Path)
    compare_parser.add_argument("after", type=Path)
    args = parser.parse_args()
    if args.command == "compare":
        print(json.dumps(compare(args.before, args.after), indent=2))
        return
    payload = run(args.dataset, args.output, args.top_k)
    print(json.dumps(payload["metrics"], indent=2))


if __name__ == "__main__":
    main()
