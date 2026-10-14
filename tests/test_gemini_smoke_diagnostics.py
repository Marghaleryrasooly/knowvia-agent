from types import SimpleNamespace as NS
import json
import pytest
from evaluation.gemini_smoke import DiagnosticModels, sanitized_report
from knowvia.generation import GeminiGenerationProvider


@pytest.mark.parametrize("code,category", [(429,"quota"),(401,"authentication"),(403,"authentication"),
    (402,"billing"),(400,"invalid_request"),(404,"api_access"),(503,"transport")])
def test_transport_categories_are_preserved_without_private_content(code,category):
    error=RuntimeError("private credential header prompt body")
    error.code=code
    def call(**kw): raise error
    records=[]
    models=DiagnosticModels(NS(generate_content=call),records,"evidence")
    with pytest.raises(RuntimeError): models.generate_content(contents="private prompt")
    assert records[0]["error_category"]==category
    assert records[0]["http_status"]==code
    assert "private" not in json.dumps(records)
    with pytest.raises(RuntimeError,match="stopped"): models.generate_content()
    assert len(records)==1


@pytest.mark.parametrize("error,category",[(TimeoutError("private"),"timeout"),(ConnectionError("private"),"transport")])
def test_timeout_and_connection(error,category):
    def call(**kw): raise error
    records=[]
    with pytest.raises(Exception): DiagnosticModels(NS(generate_content=call),records,"evidence").generate_content()
    assert records[0]["error_category"]==category


@pytest.mark.parametrize("finish,text,category", [("MAX_TOKENS","private","incomplete_output"),
    ("SAFETY","private","blocked"),("STOP","","empty_response"),("OTHER","private","invalid_response"),
    ("STOP","answer",None)])
def test_response_metadata_only(finish,text,category):
    response=NS(candidates=[NS(finish_reason=finish,content=NS(parts=[NS(text=text)]))],
        usage_metadata=NS(thoughts_token_count=320,candidates_token_count=0,total_token_count=900),
        private_body="private",headers="private")
    records=[]
    DiagnosticModels(NS(generate_content=lambda **k:response),records,"evidence").generate_content()
    assert records[0]["error_category"]==category
    assert records[0]["finish_reason"]==finish
    assert records[0]["token_usage"]["thoughts_token_count"]==320
    assert "private" not in json.dumps(records)


def test_worker_failure_returns_normal_http_and_retains_diagnostics():
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient
    from starlette.concurrency import run_in_threadpool
    from knowvia.evidence import LLMEvidenceEvaluator,evaluate_evidence_safely
    from knowvia.models import Chunk,RetrievalResult
    def call(**kw):
        error=RuntimeError("private")
        error.code=429
        raise error
    records=[]
    provider=GeminiGenerationProvider("fictional",client=NS(models=DiagnosticModels(NS(generate_content=call),records,"evidence")))
    def evaluate():
        return evaluate_evidence_safely(LLMEvidenceEvaluator(provider),"question",[
            RetrievalResult(Chunk("id","source","title","section","private evidence"),.9)])
    async def endpoint(request):
        decision=await run_in_threadpool(evaluate)
        return JSONResponse({"decision":decision.status.value})
    with TestClient(Starlette(routes=[Route("/",endpoint)])) as client:
        result=client.get("/")
        assert result.status_code==200
        assert result.json()["decision"]=="INSUFFICIENT"
    assert records[0]["error_category"]=="quota"
    assert len(records)==1


def test_report_strips_raw_outputs_prompts_and_evidence():
    payload={"cases":[{"route":"KNOWLEDGE","kind":"ABSTENTION","text":"safe abstention",
        "provider_calls":[{"raw_output":"private"}],"supporting_results":[{"text":"private"}],
        "retrieved":[{"text":"private"}],"user_prompt":"private"}]}
    report=sanitized_report(payload,[])
    assert report["request_completed"]
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize("evidence", [True,False])
def test_experimental_profile_changes_only_thinking_and_output_allowance(evidence):
    from evaluation.gemini_smoke import configure_profile
    captured=[]
    response=NS(candidates=[NS(finish_reason="STOP",content=NS(parts=[NS(text="answer")]))])
    def call(**kwargs):
        captured.append(kwargs)
        return response
    p=GeminiGenerationProvider("fictional",temperature=0.0 if evidence else .1,
        max_output_tokens=320 if evidence else 220,client=NS(models=NS(generate_content=call)))
    configure_profile(p,evidence=evidence,profile="experimental-low-1024")
    assert p.generate("unchanged system","unchanged user")=="answer"
    request=captured[0]
    assert request["model"]=="gemini-3.8-flash"
    assert request["contents"]=="unchanged user"
    assert request["config"]["system_instruction"]=="unchanged system"
    assert request["config"]["thinking_config"]=={"thinking_level":"low","include_thoughts":False}
    assert request["config"]["max_output_tokens"]==1024
    assert request["config"]["temperature"]==(0.0 if evidence else .1)
    # A separate production instance retains its original defaults.
    untouched=GeminiGenerationProvider("fictional",client=NS(models=NS(generate_content=call)))
    assert untouched.thinking_level=="high" and untouched._max_output_tokens==220


def test_experiment_report_labels_actual_configuration_and_uses_separate_output():
    from evaluation.gemini_smoke import OUTPUT,EXPERIMENT_OUTPUT
    assert OUTPUT!=EXPERIMENT_OUTPUT
    report=sanitized_report({},[],"experimental-low-1024")
    assert report["profile"]=="experimental-low-1024"
    assert report["thinking_level"]=="low"
    assert report["evidence_output_limit"]==report["answer_output_limit"]==1024
