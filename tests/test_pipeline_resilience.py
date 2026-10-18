"""Local-only recovery scenarios: paid transport is always httpx.MockTransport."""
import copy
import json

import httpx
import pytest

from test_discovery_replay import candidate, response, saved_run as _saved_run
from dzen_agent.discovery_supply import authorize_topup, fill_discovery, reduce_plan, supply_status
from dzen_agent.discovery_validation import parse_discovery
from dzen_agent.errors import UserActionRequired
from dzen_agent.provider import AITunnel
from dzen_agent.recovery import reprocess_discovery
from dzen_agent.services import ContentService, week_slots
from dzen_agent.store import dumps

saved_run = _saved_run


def service_for(store, run_id, answers, *, cost=0.5):
    calls = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 500})
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(context)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        result = response(answer)
        result["choices"][0]["message"]["annotations"] = [
            {"type": "url_citation", "url_citation": {"url": s["url"]}}
            for i in answer["candidates"] for s in i["sources"]]
        result["usage"] = {"cost_rub": cost}
        return httpx.Response(200, json=result)

    run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    provider = AITunnel(store, "mock-key", httpx.Client(transport=httpx.MockTransport(handler)))
    service = ContentService(store, provider, settings=json.loads(run["settings_json"]),
                             prompts=json.loads(run["prompts_json"]), run_id=run_id)
    return service, calls


def grant(store, run_id, **overrides):
    authorize_topup(store, run_id, confirmed=True,
                    **({"budget_rub": 2.0, "max_calls": 2, "reserve_rub": 1.0} | overrides))


def analytics(store):
    return json.loads(store.one("SELECT payload_json FROM analytics_results")["payload_json"])


def extra(data, *, ready=True):
    result = copy.deepcopy(data)
    result["candidates"] = [candidate("D001", "Музыкальное оформление немых кинолент", "https://example.com/music",
                                      status="ready_for_planning" if ready else "needs_validation")]
    return result


def test_repairs_only_unambiguous_representations(saved_run):
    _, _, data = saved_run
    changed = copy.deepcopy(data)
    idea = changed["candidates"][0]
    idea["sources"] = {**idea["sources"][0], "source_id": " S1 "}
    idea["anchor_facts"] = {**idea["anchor_facts"][0], "source_ids": "s1 "}
    original = copy.deepcopy(changed)
    parsed, issues = parse_discovery(dumps(changed))
    assert parsed.candidates[0].anchor_facts[0].source_ids == ["S1"]
    assert parsed.candidates[0].sources[0].source_id == "S1"
    assert any(i["type"] == "technical_repair" for i in issues)
    assert [i.idea_id for i in parsed.candidates] == ["D001", "D002"]
    assert changed == original  # Missing source of D003 was never invented.


def test_does_not_guess_ambiguous_or_nonexistent_source_ids(saved_run):
    _, _, data = saved_run
    data["candidates"][0]["sources"].append({**data["candidates"][0]["sources"][0], "source_id": "s1"})
    data["candidates"][0]["anchor_facts"][0]["source_ids"] = [" S1 "]
    parsed, _ = parse_discovery(dumps(data))
    assert "D001" not in [i.idea_id for i in parsed.candidates]


def test_no_paid_topup_without_consent(saved_run):
    store, run_id, _ = saved_run
    reprocess_discovery(store, run_id)
    service, calls = service_for(store, run_id, [])
    with pytest.raises(UserActionRequired):
        fill_discovery(service, analytics(store), f"run:{run_id}:discovery")
    assert not calls and not store.rows("SELECT * FROM discovery_requests")
    with pytest.raises(UserActionRequired):
        authorize_topup(store, run_id, budget_rub=2, max_calls=2, reserve_rub=1, confirmed=False)
    assert not store.rows("SELECT * FROM discovery_topups")


def test_incremental_search_merges_preserves_pending_and_replays_free(saved_run):
    store, run_id, data = saved_run
    reprocess_discovery(store, run_id)
    protected = {t: store.rows(f"SELECT * FROM {t}") for t in ("analytics_results", "prompt_versions")}
    original_receipt = store.one("SELECT * FROM ai_calls")
    grant(store, run_id)
    service, calls = service_for(store, run_id, [extra(data)])
    result = fill_discovery(service, analytics(store), f"run:{run_id}:discovery")
    assert len(calls) == 1
    assert calls[0]["search_config"]["target_candidates"] == 1
    assert len(calls[0]["existing_candidates"]) == 2
    assert calls[0]["excluded_ideas"]
    assert [i["idea_id"] for i in result["candidates"]] == ["D001", "D002", "D001:topup:1"]
    assert result["candidates"][1]["viability_status"] == "needs_validation"
    assert store.one("SELECT * FROM ai_calls WHERE call_key=?", (original_receipt["call_key"],)) == original_receipt
    for table, rows in protected.items():
        assert store.rows(f"SELECT * FROM {table}") == rows
    assert fill_discovery(service, analytics(store), f"run:{run_id}:discovery") == result
    # Crash after aggregate commit, before marking request DONE: replay is also idempotent.
    store.execute("UPDATE discovery_requests SET status='ERROR'")
    service.discover(analytics(store), f"run:{run_id}:discovery:topup:1", additional={"missing_count": 1})
    assert supply_status(store, run_id)["ready"] == 2 and len(calls) == 1
    assert len(store.rows("SELECT * FROM usage_events")) == 1


def test_enough_ideas_never_requests_sonar_even_without_grant(saved_run):
    store, run_id, _ = saved_run
    reprocess_discovery(store, run_id)
    reduce_plan(store, run_id, 1, confirmed=True)
    service, calls = service_for(store, run_id, [])
    assert fill_discovery(service, analytics(store), f"run:{run_id}:discovery")["candidates"]
    assert not calls
    run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    assert len(week_slots(run["week_start"], json.loads(run["settings_json"]))) == 1
    assert store.settings()["max_articles_per_week"] == 2


def test_bounded_search_preserves_all_good_results_without_infinite_retry(saved_run):
    store, run_id, data = saved_run
    reprocess_discovery(store, run_id)
    grant(store, run_id, max_calls=1)
    service, calls = service_for(store, run_id, [extra(data, ready=False)])
    for _ in range(2):
        with pytest.raises(UserActionRequired, match="исчерпан"):
            fill_discovery(service, analytics(store), f"run:{run_id}:discovery")
    assert len(calls) == 1
    assert len(supply_status(store, run_id)["result"].candidates) == 3


@pytest.mark.parametrize("failure", ["timeout", "http500", "http429", "json"])
def test_failed_topup_never_automatically_pays_again(saved_run, failure):
    store, run_id, _ = saved_run
    reprocess_discovery(store, run_id)
    grant(store, run_id)
    answer = {"timeout": httpx.ReadTimeout("mock timeout"),
              "http500": httpx.Response(500, json={"error": "mock"}),
              "http429": httpx.Response(429, json={"error": "mock"}),
              "json": httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{"}}],
                                                     "usage": {"cost_rub": 0.5}})}[failure]
    service, calls = service_for(store, run_id, [answer])
    for _ in range(2):
        with pytest.raises(Exception):
            fill_discovery(service, analytics(store), f"run:{run_id}:discovery")
    assert len(calls) == 1
    assert supply_status(store, run_id)["ready"] == 1


def test_reduction_requires_confirmation_and_cannot_rewrite_started_planner(saved_run):
    store, run_id, _ = saved_run
    reprocess_discovery(store, run_id)
    with pytest.raises(UserActionRequired):
        reduce_plan(store, run_id, 1, confirmed=False)
    store.execute("INSERT INTO ai_calls(call_key,status,created_at,updated_at) VALUES(?,'UNCERTAIN','now','now')",
                  (f"run:{run_id}:plan",))
    with pytest.raises(UserActionRequired, match="уже запускался"):
        reduce_plan(store, run_id, 1, confirmed=True)


@pytest.mark.parametrize("cost", [None, 1.5])
def test_unknown_cost_or_exceeded_reservation_blocks_next_search(saved_run, cost):
    store, run_id, data = saved_run
    reprocess_discovery(store, run_id)
    grant(store, run_id, budget_rub=10)
    service, calls = service_for(store, run_id, [extra(data, ready=False)], cost=cost)
    for _ in range(2):
        with pytest.raises(UserActionRequired):
            fill_discovery(service, analytics(store), f"run:{run_id}:discovery")
    assert len(calls) == 1
    assert len(supply_status(store, run_id)["result"].candidates) == 3


def test_reduction_preserves_three_moscow_slots_in_one_day(saved_run):
    store, run_id, data = saved_run
    reprocess_discovery(store, run_id)
    snapshot = json.loads(store.one("SELECT settings_json FROM pipeline_runs")["settings_json"])
    snapshot.update(max_articles_per_week=5, publication_days=list(range(7)),
                    publication_times=["05:00", "06:00", "16:00"], publication_slots={}, max_articles_per_day=3)
    store.execute("UPDATE pipeline_runs SET settings_json=?", (dumps(snapshot),))
    # Independent synthetic verified topics; no paid request is performed here.
    result = supply_status(store, run_id)["result"].model_dump(mode="json")
    result["candidates"].extend([
        candidate("D010", "Монтаж звукового оформления", "https://example.com/sound"),
        candidate("D011", "Архивы художников декораций", "https://example.com/art")])
    store.execute("UPDATE discovery_results SET payload_json=?", (dumps(result),))
    slots = reduce_plan(store, run_id, 3, confirmed=True)
    assert slots == [f"2030-01-07T{t}:00+03:00" for t in ("05:00", "06:00", "16:00")]
    assert supply_status(store, run_id)["target"] == 3
