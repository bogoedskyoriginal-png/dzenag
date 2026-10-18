import copy
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from dzen_agent.config import PROJECT_ROOT, get_settings
from dzen_agent.discovery_validation import parse_discovery
from dzen_agent.editorial_contracts import DiscoveryV1
from dzen_agent.editorial_pipeline import plan
from dzen_agent.errors import UserActionRequired
from dzen_agent.pipeline.orchestrator import create_run
from dzen_agent.prompt_service import Prompts
from dzen_agent.provider import AITunnel
from dzen_agent.recovery import reprocess_discovery
from dzen_agent.services import ContentService, citation_urls, search_evidence_summary
from dzen_agent.store import Store, dumps, now_iso

EXAMPLES = json.loads((Path(__file__).parent / "fixtures/editorial_v1.json").read_text("utf-8"))


def candidate(idea_id, topic, url, *, status="ready_for_planning"):
    idea = copy.deepcopy(EXAMPLES["discovery"][1])
    idea.update(idea_id=idea_id, topic=topic, working_headline=topic, angle=topic,
                content_format="making_of", viability_status=status,
                sources=[{"source_id": "S1", "title": "Архив", "publisher": "Киностудия", "url": url,
                          "publication_date": None, "source_type": "official", "supports": "История производства"}],
                anchor_facts=[{"claim": "Архив опубликовал историю производства.", "source_ids": ["S1"], "confidence": "high"}],
                analytics_observation_ids=["O1"])
    return idea


def payload():
    result = copy.deepcopy(EXAMPLES["discovery"][0])
    good = candidate("D001", "Реставрация цветной плёнки", "https://example.com/first")
    pending = candidate("D002", "Работа ледовых каскадёров", "https://example.com/second", status="needs_validation")
    broken = candidate("D003", "Непроверенная подборка", "https://example.com/absent", status="needs_validation")
    broken["sources"] = []
    broken["anchor_facts"] *= 2
    broken["content_format"] = "thematic_selection"
    broken["selection_feasibility"].update(status="needs_research", target_count=5)
    result["candidates"] = [good, pending, broken]
    return result


def response(data):
    return {"choices": [{"finish_reason": "stop", "message": {"content": dumps(data), "annotations": [
        {"type": "url_citation", "url_citation": {"url": "https://example.com/first", "title": "Архив"}},
        {"type": "url_citation", "url_citation": {"url": "https://example.com/second", "title": "Каскадёры"}}]}}],
        "citations": [], "search_results": [], "usage": {"cost_rub": 5}, "model": "sonar"}


@pytest.fixture
def saved_run(tmp_path, analytics_v1):
    store = Store(tmp_path / "replay.db")
    store.initialize()
    Prompts(store).seed()
    Prompts(store).save("DISCOVERY", "Авторский промпт", dumps({"contract_version": "1.0", "stage": "discovery"}))
    store.set_setting("max_articles_per_week", 2)
    store.set_setting("discovery_model", "sonar")
    run_id = create_run(store, "2030-01-07")
    data = payload()
    data.update(run_id=run_id, planning_window={"start": "2030-01-07", "end": "2030-01-13", "timezone": "Europe/Moscow"})
    store.execute("INSERT INTO analytics_results(run_id,payload_json,created_at) VALUES(?,?,?)", (run_id, dumps(analytics_v1), now_iso()))
    store.execute("INSERT INTO ai_calls(call_key,status,result_json,created_at,updated_at) VALUES(?,'SUCCEEDED',?,?,?)",
                  (f"run:{run_id}:discovery", dumps(response(data)), now_iso(), now_iso()))
    store.execute("UPDATE pipeline_stages SET status='SUCCEEDED' WHERE run_id=? AND stage IN ('COLLECT_STATS','ANALYZE')", (run_id,))
    store.execute("UPDATE pipeline_stages SET status='ERROR',error='original validation error',attempts=1 WHERE run_id=? AND stage='DISCOVERY'", (run_id,))
    store.execute("UPDATE pipeline_runs SET status='NEEDS_ACTION',error='original validation error' WHERE id=?", (run_id,))
    return store, run_id, data


def forbidden(*args, **kwargs):
    pytest.fail("Local replay must never request HTTP, AI or a background pipeline")


def test_broken_candidate_does_not_invalidate_other_ideas():
    data = payload()
    with pytest.raises(ValidationError):
        DiscoveryV1.model_validate(data)  # Public schema remains strict.
    result, errors = parse_discovery(dumps(data))
    assert [i.idea_id for i in result.candidates] == ["D001", "D002"]
    assert result.candidates[1].viability_status == "needs_validation"
    assert len([e for e in errors if e["type"] == "unknown_source_id"]) == 2
    assert errors[1]["loc"] == ["candidates", 2, "anchor_facts", 0, "source_ids", 0]
    assert errors[1]["available_source_ids"] == []
    assert len(result.rejected_ideas) == 1
    assert data["candidates"][2]["sources"] == []
    assert DiscoveryV1.model_validate_json(result.model_dump_json())


@pytest.mark.parametrize("mutation", ["missing", "extra", "wrong_candidates_type", "wrong_version"])
def test_invalid_envelope_is_never_salvaged(mutation):
    data = payload()
    if mutation == "missing":
        del data["search_report"]
    elif mutation == "extra":
        data["invented"] = True
    elif mutation == "wrong_candidates_type":
        data["candidates"] = {}
    else:
        data["contract_version"] = "2.0"
    with pytest.raises(ValidationError):
        parse_discovery(dumps(data))


def test_duplicate_ids_quarantine_both_without_arbitrary_selection():
    data = payload()
    data["candidates"][1]["idea_id"] = "D001"
    result, errors = parse_discovery(dumps(data))
    assert result.candidates == []
    assert sum(e["type"] == "duplicate_idea_id" for e in errors) == 2


def test_annotations_are_valid_search_metadata_even_with_empty_legacy_fields():
    paid = response(payload())
    assert citation_urls(paid) == {"https://example.com/first", "https://example.com/second"}
    assert search_evidence_summary(paid)["annotations_count"] == 2
    assert search_evidence_summary(paid)["upstream_fields"] == "unknown_for_legacy_receipt"


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_provider_preserves_annotations_and_distinguishes_missing_fields(tmp_path, explicit_empty):
    store = Store(tmp_path / "provider.db")
    store.initialize()
    upstream = response(payload())
    if not explicit_empty:
        del upstream["citations"], upstream["search_results"]
    requests = []
    def handler(request):
        requests.append(request.method)
        return httpx.Response(200, json={"balance": 100} if request.method == "GET" else upstream)
    provider = AITunnel(store, "mock-key", httpx.Client(transport=httpx.MockTransport(handler)))
    args = dict(call_key="saved", operation="DISCOVERY", model="sonar", payload={})
    result = provider.call(**args)
    assert provider.call(**args) == result
    assert requests.count("POST") == 1
    assert result["choices"][0]["message"]["annotations"] == upstream["choices"][0]["message"]["annotations"]
    assert result["search_metadata"] == {"citations_field_present": explicit_empty, "search_results_field_present": explicit_empty}
    assert citation_urls(result)
    assert store.costs()["total"] == 5


def test_offline_replay_preserves_receipt_analytics_prompts_and_does_not_start_planner(saved_run, monkeypatch):
    store, run_id, _ = saved_run
    for target in ("httpx.Client.request", "dzen_agent.provider.AITunnel.call", "dzen_agent.scheduler.launch_pipeline"):
        monkeypatch.setattr(target, forbidden)
    tables = ("ai_calls", "analytics_results", "prompt_versions", "usage_events")
    before = {t: store.rows(f"SELECT * FROM {t} ORDER BY rowid") for t in tables}
    report = reprocess_discovery(store, run_id)
    assert report["ready_ids"] == ["D001"]
    assert report["accepted_ids"] == ["D001", "D002"]
    assert not report["planner_ready"] and not report["planner_started"]
    assert Path(report["backup_path"]).is_file()
    assert store.one("SELECT status FROM pipeline_stages WHERE run_id=? AND stage='DISCOVERY'", (run_id,))["status"] == "SUCCEEDED"
    assert store.one("SELECT attempts FROM pipeline_stages WHERE run_id=? AND stage='PLAN'", (run_id,))["attempts"] == 0
    assert not store.rows("SELECT * FROM content_plans")
    assert before == {t: store.rows(f"SELECT * FROM {t} ORDER BY rowid") for t in tables}
    assert reprocess_discovery(store, run_id)["result_id"] == report["result_id"]
    assert len(store.rows("SELECT * FROM discovery_results")) == 1


@pytest.mark.parametrize("bad_reference", ["analytics", "article", "search", "fact"])
def test_context_and_grounding_errors_quarantine_one_candidate(saved_run, monkeypatch, bad_reference):
    store, run_id, data = saved_run
    good = data["candidates"][0]
    if bad_reference == "analytics":
        good["analytics_observation_ids"] = ["UNKNOWN"]
    elif bad_reference == "article":
        good["possible_duplicate_article_ids"] = [123456]
    elif bad_reference == "search":
        good["sources"][0]["url"] = "https://invented.invalid/film"
    else:
        good["anchor_facts"][0]["source_ids"] = []
    # Supply a second independently good ready candidate.
    data["candidates"].append(candidate("D004", "Звук в немом кино", "https://example.com/second"))
    store.execute("UPDATE ai_calls SET result_json=?", (dumps(response(data)),))
    monkeypatch.setattr("httpx.Client.request", forbidden)
    report = reprocess_discovery(store, run_id)
    assert report["ready_ids"] == ["D004"]
    assert "D001" not in report["accepted_ids"]


def test_shortfall_blocks_planner_before_any_paid_call(saved_run, monkeypatch, analytics_v1):
    store, run_id, _ = saved_run
    reprocess_discovery(store, run_id)
    service = ContentService(store, object(), run_id=run_id)
    monkeypatch.setattr(service, "structured", forbidden)
    result = json.loads(store.one("SELECT payload_json FROM discovery_results")["payload_json"])
    with pytest.raises(UserActionRequired, match="Planner не запускался"):
        plan(service, analytics_v1, result, "2030-01-07", "must-not-call")


def test_ui_offline_replay_needs_no_key_and_shows_shortfall(saved_run, monkeypatch):
    store, run_id, _ = saved_run
    config = replace(get_settings(), database_path=store.database_path, aitunnel_api_key=None)
    monkeypatch.setattr("dzen_agent.config.get_settings", lambda: config)
    monkeypatch.setattr("dzen_agent.scheduler.launch_pipeline", forbidden)
    monkeypatch.setattr("httpx.Client.request", forbidden)
    app = AppTest.from_file(str(PROJECT_ROOT / "dzen_agent/ui.py"), default_timeout=20).run()
    next(b for b in app.button if b.label == "Разобраться с ошибкой").click().run()
    button = next(b for b in app.button if b.label == "Обработать сохранённый Discovery бесплатно")
    assert not button.disabled
    button.click().run()
    assert not app.exception
    assert not any(b.label == "Перейти к планированию (платно)" for b in app.button)
    assert any("не хватает" in item.value for item in app.warning)
    assert store.one("SELECT attempts FROM pipeline_stages WHERE run_id=? AND stage='PLAN'", (run_id,))["attempts"] == 0


@pytest.mark.parametrize("invalid", ["run_id", "window", "length", "json"])
def test_replay_never_salvages_wrong_run_window_truncated_or_broken_json(saved_run, monkeypatch, invalid):
    store, run_id, data = saved_run
    if invalid == "run_id":
        data["run_id"] = 9999
    elif invalid == "window":
        data["planning_window"]["end"] = "2030-01-14"
    saved = response(data)
    if invalid == "length":
        saved["choices"][0]["finish_reason"] = "length"
    elif invalid == "json":
        saved["choices"][0]["message"]["content"] = '{"unfinished":'
    store.execute("UPDATE ai_calls SET result_json=?", (dumps(saved),))
    monkeypatch.setattr("httpx.Client.request", forbidden)
    receipt = store.rows("SELECT * FROM ai_calls")
    with pytest.raises((UserActionRequired, ValueError)):
        reprocess_discovery(store, run_id)
    assert store.rows("SELECT * FROM ai_calls") == receipt
    assert not store.rows("SELECT * FROM discovery_results")
    assert not store.rows("SELECT * FROM content_plans")
    assert store.one("SELECT status FROM pipeline_stages WHERE run_id=? AND stage='DISCOVERY'", (run_id,))["status"] == "ERROR"


def test_ready_saved_response_waits_for_explicit_planner_action(saved_run, monkeypatch):
    store, run_id, data = saved_run
    data["candidates"][1]["viability_status"] = "ready_for_planning"
    store.execute("UPDATE ai_calls SET result_json=?", (dumps(response(data)),))
    monkeypatch.setattr("httpx.Client.request", forbidden)
    report = reprocess_discovery(store, run_id)
    assert report["planner_ready"]
    assert store.one("SELECT status FROM pipeline_runs WHERE id=?", (run_id,))["status"] == "DISCOVERY_READY"
    assert store.one("SELECT attempts FROM pipeline_stages WHERE run_id=? AND stage='PLAN'", (run_id,))["attempts"] == 0
