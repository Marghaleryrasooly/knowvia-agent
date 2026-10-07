"""Explicit live comparison on the unchanged knowledge benchmark; no action tools."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import os
from urllib.request import urlopen

from knowvia.answering import GROUNDING_SYSTEM_PROMPT, build_grounded_user_prompt
from knowvia.cli import create_embedder, create_evidence_evaluator, create_generator, DEFAULT_INDEX
from knowvia.eval_dataset import load_evaluation_dataset
from knowvia.answer_grading import DeterministicAnswerGrader
from knowvia.identity import LocalIdentityProvider
from knowvia.knowledge_reliability_evaluation import (
    DEFAULT_DATASET, DEFAULT_DOCUMENTS, _scope, _case_payload, calculate_metrics)
from knowvia.knowledge_normalization import DeterministicKnowledgeRequestNormalizer
from knowvia.retrieval import Retriever
from knowvia.skill_orchestration import SourceScopedRetriever
from knowvia.vector_store import JsonVectorStore
from knowvia.models import EvidenceStatus


class CountingGenerator:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = 0
    @property
    def model_name(self):
        return self.delegate.model_name
    def generate(self, system, user):
        self.calls += 1
        return self.delegate.generate(system, user)


def run(output: Path, limit: int = 40):
    if not 1 <= limit <= 40:
        raise ValueError("Comparison limit must be between 1 and 40")
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    try:
        with urlopen(host + "/api/tags", timeout=3) as response:
            tags = json.load(response)
    except Exception:
        raise RuntimeError("Ollama reference unavailable; comparison not performed") from None
    model = os.getenv("OLLAMA_GENERATION_MODEL", "llama3.2:latest")
    reference_metadata = [{key: item.get(key) for key in ("name", "digest", "details")}
                          for item in tags.get("models", []) if item.get("name") == model]
    dataset = load_evaluation_dataset(DEFAULT_DATASET, DEFAULT_DOCUMENTS)
    identity = LocalIdentityProvider().resolve("employee-001")
    retriever = Retriever(create_embedder("local"), JsonVectorStore(DEFAULT_INDEX))
    normalizer = DeterministicKnowledgeRequestNormalizer()
    grader = DeterministicAnswerGrader()
    evaluators = {name: create_evidence_evaluator(name) for name in ("ollama", "cloudflare")}
    generators = {name: create_generator(name) for name in evaluators}
    for evaluator in evaluators.values():
        evaluator.generator = CountingGenerator(evaluator.generator)
    outcomes = {name: [] for name in evaluators}
    payload = {"reference_metadata": reference_metadata, "cases": outcomes,
               "shared_generation": [], "metrics": {}, "completed": False}
    output.parent.mkdir(parents=True, exist_ok=True)
    for number, case in enumerate(dataset.cases[:limit], 1):
        print(f"Comparison {number}/{limit}: {case.id}", flush=True)
        skill, sources = _scope(case.question, identity)
        query = normalizer.normalize(case.question).normalized_request
        retrieved = SourceScopedRetriever(retriever, sources).search(query, 3)
        decisions = {}
        answers = {}
        for name, evaluator in evaluators.items():
            start_calls = evaluator.generator.calls
            decision, diagnostics = evaluator.evaluate_with_diagnostics(query, retrieved)
            decisions[name] = decision
            supporting = [retrieved[i - 1] for i in decision.supporting_evidence_ids]
            answer, grade = None, None
            if decision.status is EvidenceStatus.SUFFICIENT:
                try:
                    answer = generators[name].generate(GROUNDING_SYSTEM_PROMPT, build_grounded_user_prompt(query, supporting))
                    if case.expected_behavior == "ANSWER":
                        grade = grader.grade(case, answer)
                except Exception:
                    answer = None  # Never persist provider error details.
            answers[name] = answer
            item = _case_payload(case, retrieved, decision, diagnostics, supporting, answer, grade, skill, sources, query)
            # Offline classifier output contains only fictional benchmark evidence.
            item["evidence_calls"] = evaluator.generator.calls - start_calls
            item["repair_attempted"] = item["evidence_calls"] > 1
            outcomes[name].append(item)
        if decisions["ollama"].status is EvidenceStatus.SUFFICIENT:
            shared = [retrieved[i - 1] for i in decisions["ollama"].supporting_evidence_ids]
            if decisions["cloudflare"].supporting_evidence_ids == decisions["ollama"].supporting_evidence_ids:
                candidate_answer = answers["cloudflare"]
            else:
                try:
                    candidate_answer = generators["cloudflare"].generate(GROUNDING_SYSTEM_PROMPT, build_grounded_user_prompt(query, shared))
                except Exception:
                    candidate_answer = None
            payload["shared_generation"].append({"id": case.id,
                "supporting_chunk_ids": [r.chunk.id for r in shared],
                "reference": answers["ollama"], "candidate": candidate_answer,
                "candidate_grade": asdict(grader.grade(case, candidate_answer)) if candidate_answer and case.expected_behavior == "ANSWER" else None})
        output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if any(items[-1]["evidence_diagnostics"]["provider_request_failed"] for items in outcomes.values()):
            print("Provider failure; stopped bounded comparison", flush=True)
            return payload
    payload["completed"] = limit == 40
    payload["metrics"] = {name: calculate_metrics(items) for name, items in outcomes.items()}
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()
    payload = run(args.output, args.limit)
    print(json.dumps({"completed": payload["completed"], "metrics": payload["metrics"]}))


if __name__ == "__main__":
    main()
