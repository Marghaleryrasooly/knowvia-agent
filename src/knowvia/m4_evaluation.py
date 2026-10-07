from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from knowvia.answer_grading import DeterministicAnswerGrader
from knowvia.answering import GROUNDING_SYSTEM_PROMPT, build_grounded_user_prompt, citations_from_results
from knowvia.embeddings import DEFAULT_LOCAL_MODEL, LocalEmbeddingProvider
from knowvia.eval_dataset import EvaluationDataset, load_evaluation_dataset
from knowvia.evidence import LLMEvidenceEvaluator
from knowvia.generation import DEFAULT_OLLAMA_HOST, DEFAULT_OLLAMA_MODEL, OllamaGenerationProvider
from knowvia.models import EvidenceStatus
from knowvia.pipeline_metrics import CaseEvaluation, classify_failures, calculate_pipeline_metrics
from knowvia.retrieval import Retriever
from knowvia.vector_store import JsonVectorStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "evaluation" / "knowvia_m4_eval_v1.json"
DEFAULT_DOCUMENTS = PROJECT_ROOT / "knowvia_m1_dataset"
DEFAULT_INDEX = PROJECT_ROOT / "data" / "knowvia_index.json"
DEFAULT_ARTIFACTS = PROJECT_ROOT / "artifacts" / "evaluation"


def run_live_evaluation(
    dataset: EvaluationDataset,
    index_path: Path = DEFAULT_INDEX,
    top_k: int = 3,
) -> list[CaseEvaluation]:
    embedder = LocalEmbeddingProvider(os.getenv("LOCAL_EMBEDDING_MODEL", DEFAULT_LOCAL_MODEL))
    retriever = Retriever(embedder, JsonVectorStore(index_path))
    model = os.getenv("OLLAMA_GENERATION_MODEL", DEFAULT_OLLAMA_MODEL)
    host = os.getenv("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
    generator = OllamaGenerationProvider(model=model, host=host)
    evaluator = LLMEvidenceEvaluator(
        OllamaGenerationProvider(
            model=os.getenv("OLLAMA_EVIDENCE_MODEL", model),
            host=host,
            temperature=0.0,
            max_output_tokens=220,
        )
    )
    grader = DeterministicAnswerGrader()
    outcomes: list[CaseEvaluation] = []
    for number, case in enumerate(dataset.cases, start=1):
        print(f"[{number:02d}/{len(dataset.cases)}] {case.id}: {case.question}", flush=True)
        retrieved = retriever.search(case.question, top_k)
        citations = tuple(citations_from_results(retrieved))
        decision, diagnostics = evaluator.evaluate_with_diagnostics(
            case.question, retrieved
        )
        answer: str | None = None
        grade = None
        if decision.status is EvidenceStatus.SUFFICIENT:
            answer = generator.generate(
                GROUNDING_SYSTEM_PROMPT,
                build_grounded_user_prompt(case.question, retrieved),
            )
            if case.expected_behavior == "ANSWER":
                grade = grader.grade(case, answer)
        failures = classify_failures(case, decision.status, citations, grade)
        outcomes.append(
            CaseEvaluation(
                case=case,
                actual_status=decision.status,
                reason=decision.reason,
                retrieved_sources=citations,
                answer=answer,
                answer_grade=grade,
                failures=failures,
                primary_failure=failures[0] if failures else None,
                evidence_diagnostics=diagnostics,
            )
        )
    return outcomes


def result_payload(
    dataset: EvaluationDataset,
    outcomes: list[CaseEvaluation],
    top_k: int,
) -> dict[str, Any]:
    metrics = calculate_pipeline_metrics(outcomes)
    return {
        "schema_version": 1,
        "run": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dataset_name": dataset.name,
            "dataset_version": dataset.version,
            "case_ids": [case.id for case in dataset.cases],
            "embedding_model": os.getenv("LOCAL_EMBEDDING_MODEL", DEFAULT_LOCAL_MODEL),
            "generation_model": os.getenv("OLLAMA_GENERATION_MODEL", DEFAULT_OLLAMA_MODEL),
            "evidence_model": os.getenv("OLLAMA_EVIDENCE_MODEL", os.getenv("OLLAMA_GENERATION_MODEL", DEFAULT_OLLAMA_MODEL)),
            "top_k": top_k,
        },
        "metrics": metrics.to_dict(),
        "cases": [
            {
                "id": result.case.id,
                "question": result.case.question,
                "expected_behavior": result.case.expected_behavior,
                "actual_status": result.actual_status.value,
                "reason": result.reason,
                "answer": result.answer,
                "answer_grade": asdict(result.answer_grade) if result.answer_grade else None,
                "expected_sources": [asdict(source) for source in result.case.expected_sources],
                "retrieved_sources": [asdict(source) for source in result.retrieved_sources],
                "failures": [stage.value for stage in result.failures],
                "primary_failure": result.primary_failure.value if result.primary_failure else None,
                "evidence_diagnostics": (
                    {
                        **asdict(result.evidence_diagnostics),
                        "final_decision": result.evidence_diagnostics.final_decision.value,
                    }
                    if result.evidence_diagnostics
                    else None
                ),
            }
            for result in outcomes
        ],
    }


def save_result(payload: dict[str, Any], output: Path | None = None) -> Path:
    if output is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = DEFAULT_ARTIFACTS / f"m4_baseline_{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return output


def compare_results(before_path: Path, after_path: Path) -> dict[str, Any]:
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    for payload in (before, after):
        if payload.get("schema_version") != 1:
            raise ValueError("Unsupported result schema version")
    before_run, after_run = before["run"], after["run"]
    if (before_run["dataset_name"], before_run["dataset_version"], before_run["case_ids"]) != (
        after_run["dataset_name"], after_run["dataset_version"], after_run["case_ids"]
    ):
        raise ValueError("Results use different datasets or case IDs")

    metric_names = [
        "retrieval_recall_at_1", "retrieval_recall_at_3", "case_hit_at_1", "case_complete_at_3",
        "conditional_answer_accuracy", "end_to_end_answer_success", "expected_source_recall",
        "source_precision", "complete_citation_accuracy",
    ]
    deltas = {name: after["metrics"][name] - before["metrics"][name] for name in metric_names}
    for name in ("overall_accuracy", "answer_routing_accuracy", "abstention_correctness", "unsafe_answer_rate", "over_abstention_rate"):
        deltas[name] = after["metrics"]["routing"][name] - before["metrics"]["routing"][name]
    before_cases = {case["id"]: case for case in before["cases"]}
    after_cases = {case["id"]: case for case in after["cases"]}
    new_passes = [case_id for case_id in before_run["case_ids"] if before_cases[case_id]["failures"] and not after_cases[case_id]["failures"]]
    regressions = [case_id for case_id in before_run["case_ids"] if not before_cases[case_id]["failures"] and after_cases[case_id]["failures"]]
    stage_changes = {
        case_id: {"before": before_cases[case_id]["primary_failure"], "after": after_cases[case_id]["primary_failure"]}
        for case_id in before_run["case_ids"]
        if before_cases[case_id]["primary_failure"] != after_cases[case_id]["primary_failure"]
    }
    return {"metric_deltas": deltas, "new_passes": new_passes, "regressions": regressions, "failure_stage_changes": stage_changes}


def print_summary(payload: dict[str, Any]) -> None:
    metrics = payload["metrics"]
    routing = metrics["routing"]
    print("\nM4 Evaluation Summary")
    print("=" * 30)
    for label, key in (
        ("Retrieval Recall@1", "retrieval_recall_at_1"), ("Retrieval Recall@3", "retrieval_recall_at_3"),
        ("Case Hit@1", "case_hit_at_1"), ("Case Complete@3", "case_complete_at_3"),
    ):
        print(f"{label}: {metrics[key]:.1%}")
    for label, key in (
        ("Overall routing accuracy", "overall_accuracy"), ("Answer-routing accuracy", "answer_routing_accuracy"),
        ("Abstention correctness", "abstention_correctness"), ("Unsafe-answer rate", "unsafe_answer_rate"),
        ("Over-abstention rate", "over_abstention_rate"),
    ):
        print(f"{label}: {routing[key]:.1%}")
    for label, key in (
        ("Conditional answer accuracy", "conditional_answer_accuracy"), ("End-to-end answer success", "end_to_end_answer_success"),
        ("Expected-source recall", "expected_source_recall"), ("Source precision", "source_precision"),
        ("Complete citation accuracy", "complete_citation_accuracy"),
    ):
        print(f"{label}: {metrics[key]:.1%}")
    print("Failure counts:")
    for stage, count in metrics["failure_counts"].items():
        print(f"- {stage}: {count}")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Knowvia M4 end-to-end evaluation")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run the live 50-case local evaluation")
    run.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    run.add_argument("--documents", type=Path, default=DEFAULT_DOCUMENTS)
    run.add_argument("--index-file", type=Path, default=DEFAULT_INDEX)
    run.add_argument("--top-k", type=int, default=3)
    run.add_argument("--output", type=Path)
    compare = commands.add_parser("compare", help="Compare two saved M4 results")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    return parser


def main() -> None:
    load_dotenv()
    args = create_parser().parse_args()
    if args.command == "compare":
        print(json.dumps(compare_results(args.before, args.after), indent=2))
        return
    dataset = load_evaluation_dataset(args.dataset, args.documents)
    outcomes = run_live_evaluation(dataset, args.index_file, args.top_k)
    payload = result_payload(dataset, outcomes, args.top_k)
    path = save_result(payload, args.output)
    print_summary(payload)
    print(f"Saved result: {path}")


if __name__ == "__main__":
    main()
