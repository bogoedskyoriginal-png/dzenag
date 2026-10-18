import copy
import json
from pathlib import Path

import httpx
import pytest

from dzen_agent.editorial_contracts import DiscoveryV1
from dzen_agent.errors import UserActionRequired
from dzen_agent.http_diagnostics import sanitize
from dzen_agent.pipeline.orchestrator import Pipeline, create_run
from dzen_agent.prompt_service import Prompts
from dzen_agent.provider import AITunnel
from dzen_agent.recovery import authorize_retry
from dzen_agent.services import ContentService
from dzen_agent.store import Store, dumps


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("dzen_agent.errors.PROJECT_ROOT", tmp_path)
    result = Store(tmp_path / "sonar.db")
    result.initialize()
    Prompts(result).seed()
    return result


@pytest.mark.parametrize("model", ["sonar", "perplexity/sonar", "sonar-pro", "sonar-reasoning-pro", "sonar-deep-research", "sonar-pro-search"])
def test_sonar_uses_exact_schema_without_rewriting_prompt_or_parameters(store, model):
    prompt = 'Авторский Discovery: {"contract_version":"1.0","stage":"discovery"}'
    Prompts(store).save("DISCOVERY", "My Discovery", prompt)
    before = store.rows("SELECT * FROM prompt_versions ORDER BY id")
    context = {"run_context": {"run_id": 1}}
    output = json.loads((Path(__file__).parent / "fixtures/editorial_v1.json").read_text("utf-8"))["discovery"][0]
    payloads = []

    class LocalProvider:
        def call(self, **kwargs):
            payloads.append(kwargs)
            return {"choices": [{"message": {"content": dumps(output)}}]}

    settings = {**store.settings(), "research_model": model, "max_output_tokens": 16000,
                "public_catalog": {"chat": {model: {"max_output": 127000}}}}
    result, _ = ContentService(store, LocalProvider(), settings=settings).structured(
        "DISCOVERY", DiscoveryV1, context, "run:1:discovery")
    payload = payloads[0]["payload"]
    assert payloads[0]["model"] == model
    assert payload["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "DiscoveryV1", "strict": True, "schema": DiscoveryV1.model_json_schema()}}
    assert payload["max_tokens"] == 16000
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert payload["messages"][0]["content"].startswith(prompt)
    assert json.loads(payload["messages"][1]["content"]) == context
    assert set(payload) == {"messages", "max_tokens", "response_format"}
    assert result.contract_version == "1.0"
    assert store.rows("SELECT * FROM prompt_versions ORDER BY id") == before


def test_other_model_json_mode_and_sonar_catalog_cap_preserved(store):
    Prompts(store).save("DISCOVERY", "Discovery", "JSON")
    captured = []

    class Capture:
        def call(self, **kwargs):
            captured.append(kwargs["payload"])
            return {"choices": [{"message": {"content": '{}'}}]}

    for model in ("sonar", "other-model"):
        settings = {**store.settings(), "discovery_model": model, "max_output_tokens": 16000,
                    "public_catalog": {"chat": {model: {"max_output": 8000}}}}
        with pytest.raises(UserActionRequired):
            ContentService(store, Capture(), settings=settings).structured("DISCOVERY", DiscoveryV1, {}, model)
        assert captured[-1]["max_tokens"] == 8000
    assert captured[-1]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("plain_text", [False, True])
def test_http400_body_request_id_and_request_saved_without_secrets_and_never_auto_retried(store, tmp_path, plain_text):
    key = "private-provider-key-987654321"
    other = "never-save-this-password"
    captures = []
    body = {"error": {"code": 400, "message": f"Unsupported response_format json_object; api_key={key}",
        "metadata": {"raw": f'password="{other}"', "Authorization": "Bearer abcdefg",
                     "nested": [{"client_secret": other}], "cookie": "session=private"}},
        "usage": {"cost_rub": .05}}

    def handler(request):
        captures.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 500})
        assert request.headers["Authorization"] == "Bearer " + key
        headers = {"x-request-id": "request-400"}
        return httpx.Response(400, text=f'bad response_format; api_key={key}; password="{other}"', headers=headers) if plain_text else httpx.Response(400, json=body, headers=headers)

    api = AITunnel(store, key, httpx.Client(transport=httpx.MockTransport(handler)))
    for _ in range(2):
        with pytest.raises(UserActionRequired) as caught:
            api.call(call_key="run:1:discovery", operation="DISCOVERY", model="sonar", payload={
                "messages": [{"role": "user", "content": "secret echoed: " + key}],
                "response_format": {"type": "json_object"}, "max_tokens": 16000})
        assert "response_format" in str(caught.value)
    assert captures == ["GET", "POST"]  # Includes neither another POST nor a balance check on replay.
    receipt = store.one("SELECT * FROM ai_calls")
    diagnostic = json.loads(receipt["result_json"])["http_error"]
    assert diagnostic["http_status"] == 400 and diagnostic["request_id"] == "request-400"
    assert diagnostic["request"]["max_tokens"] == 16000
    assert diagnostic["request"]["response_format"] == {"type": "json_object"}
    assert "headers" not in diagnostic["request"]
    persisted = dumps(store.rows("SELECT * FROM ai_calls") + store.rows("SELECT * FROM usage_events")
                      + store.rows("SELECT * FROM event_log"))
    persisted += "".join(p.read_text("utf-8") for p in (tmp_path / "logs").glob("*"))
    for secret in (key, other, "abcdefg", "session=private"):
        assert secret not in persisted
    assert store.one("SELECT request_id FROM usage_events")["request_id"] == "request-400"
    assert store.costs()["requests"] == 1
    assert store.costs()["total"] == (0 if plain_text else .05)
    assert store.costs()["unknown"] == (1 if plain_text else 0)


def test_redaction_handles_nested_credentials_and_plain_assignments():
    value = {"headers": {"secret_header": "hidden"}, "metadata": [{"access_token": "token-value", "apiKey": "other"}],
             "raw": 'password="pass-value"; client_secret=secret-value; Cookie: session-value',
             "raw_header": "Authorization: Basic dXNlcjpwYXNz"}
    safe = dumps(sanitize(value))
    for secret in ("hidden", "token-value", "other", "pass-value", "secret-value", "session-value", "dXNlcjpwYXNz"):
        assert secret not in safe


@pytest.mark.parametrize("status", [400, 401, 402, 403, 408, 422, 500, 502])
def test_rejected_or_uncertain_calls_do_not_retry_with_changed_parameters(store, status):
    posts = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 500})
        posts.append(1)
        return httpx.Response(status, json={"error": {"message": "invalid response format"}})
    store.set_setting("max_retries", 3)
    api = AITunnel(store, "mock-key", httpx.Client(transport=httpx.MockTransport(handler)))
    for response_format in ({"type": "json_object"}, {"type": "json_schema"}):
        with pytest.raises(UserActionRequired):
            api.call(call_key="rejected", operation="DISCOVERY", model="sonar", payload={
                "messages": [], "response_format": response_format})
    assert posts == [1]
    receipt = store.one("SELECT status,result_json FROM ai_calls")
    assert receipt["status"] == ("UNCERTAIN" if status >= 500 else "ERROR")
    assert json.loads(receipt["result_json"])["http_error"]["http_status"] == status


def test_large_plain_error_is_redacted_before_truncation_and_malformed_usage_does_not_break_receipt(store):
    key = "private-api-key-with-visible-prefix-1234567890"
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 500})
        return httpx.Response(400, json={"error": {"message": "x" * 32700 + key + "z" * 1000}, "usage": []})
    api = AITunnel(store, key, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(UserActionRequired):
        api.call(call_key="large", operation="DISCOVERY", model="sonar", payload={"messages": []})
    receipt = store.one("SELECT * FROM ai_calls")
    assert receipt["status"] == "ERROR"
    diagnostic = json.loads(receipt["result_json"])["http_error"]
    assert diagnostic["response_body_truncated"]
    assert "private-api-key-with-visible-prefix" not in receipt["result_json"]


def test_discovery_resume_requires_consent_skips_successful_analytics_and_keeps_history(store, analytics_v1, monkeypatch):
    prompts = Prompts(store)
    for stage in ("DISCOVERY", "PLANNER", "RESEARCH", "WRITER"):
        prompts.save(stage, "Author " + stage, dumps({"contract_version": "1.0", "stage": stage.lower()}))
    store.set_setting("research_model", "sonar")  # Same fallback as the real failed run.
    store.set_setting("max_articles_per_week", 1)
    run_id = create_run(store, "2030-01-07")
    store.execute("INSERT INTO analytics_results(run_id,payload_json,created_at) VALUES(?,?,'2030-01-01')", (run_id, dumps(analytics_v1)))
    store.execute("UPDATE pipeline_stages SET status='SUCCEEDED',attempts=2 WHERE run_id=? AND stage IN ('ANALYZE','COLLECT_STATS')", (run_id,))
    store.execute("UPDATE pipeline_stages SET status='ERROR' WHERE run_id=? AND stage='DISCOVERY'", (run_id,))
    call_key = f"run:{run_id}:discovery"
    # Represents the already-failed historical request, whose body was not captured.
    store.execute("INSERT INTO ai_calls(call_key,status,error,created_at,updated_at) VALUES(?,'ERROR','AITUNNEL: ошибка HTTP 400','2030','2030')", (call_key,))
    before_analytics = store.rows("SELECT * FROM analytics_results")
    before_prompt = store.rows("SELECT * FROM prompt_versions")
    before_stage = store.one("SELECT * FROM pipeline_stages WHERE stage='ANALYZE'")
    calls = []
    fixture = json.loads((Path(__file__).parent / "fixtures/editorial_v1.json").read_text("utf-8"))["discovery"]

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 500})
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["response_format"]["type"] == "json_schema"
        context = json.loads(payload["messages"][1]["content"])
        assert context["analytics"] == analytics_v1
        result = copy.deepcopy(fixture[0])
        result.update(run_id=run_id, planning_window=context["run_context"]["planning_window"])
        idea = copy.deepcopy(fixture[1])
        idea.update(topic="Реставрация плёнки", content_format="making_of",
                    sources=[{**fixture[3], "url": "https://example.com/source"}],
                    anchor_facts=[{**fixture[2], "claim": "Факт", "source_ids": ["S001"]}])
        result["candidates"] = [idea]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": dumps(result)}}],
                                        "citations": ["https://example.com/source"], "usage": {"cost_rub": .2}})

    class DiscoveryBoundary(Pipeline):
        def _stage(self, stage, run, service):
            if stage == "PLAN":
                raise UserActionRequired("Mock boundary before Planner")
            return super()._stage(stage, run, service)

    def forbidden_analytics(*args, **kwargs):
        raise AssertionError("Successful Analytics must never run again")
    monkeypatch.setattr(ContentService, "analyze", forbidden_analytics)
    pipeline = DiscoveryBoundary(store, AITunnel(store, "mock-key", httpx.Client(transport=httpx.MockTransport(handler))))
    assert pipeline.execute(run_id) == "NEEDS_ACTION" and not calls
    with pytest.raises(UserActionRequired):
        authorize_retry(store, call_key, confirmed=False)
    assert not calls
    authorize_retry(store, call_key, confirmed=True)
    assert pipeline.execute(run_id) == "NEEDS_ACTION"  # Explicit local boundary before another paid stage.
    assert pipeline.execute(run_id) == "NEEDS_ACTION"
    assert len(calls) == 1
    assert store.one("SELECT status FROM pipeline_stages WHERE stage='DISCOVERY'")["status"] == "SUCCEEDED"
    assert store.rows("SELECT * FROM analytics_results") == before_analytics
    assert store.rows("SELECT * FROM prompt_versions") == before_prompt
    assert store.one("SELECT * FROM pipeline_stages WHERE stage='ANALYZE'") == before_stage
    assert store.one("SELECT status,error FROM ai_calls WHERE call_key LIKE '%:archived:%'") == {
        "status": "ERROR", "error": "AITUNNEL: ошибка HTTP 400"}
    assert store.costs()["total"] == .2
