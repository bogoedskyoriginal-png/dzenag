import copy
import json

import pytest

from test_editorial_contracts_v1 import example, native_run as _native_run
from dzen_agent.editorial_pipeline import approved_concept
from dzen_agent.pipeline.orchestrator import Pipeline, create_run
from dzen_agent.plan_review import approve_plan
from dzen_agent.recovery import reprocess_research
from dzen_agent.research_validation import ResearchReferenceError, inspect_references, parse_research
from dzen_agent.services import ContentService
from dzen_agent.store import dumps, now_iso

native_run = _native_run


def dossier():
    data = example("research")
    data.update(status="ok", research_status="ready", works=[], people=[], quotes=[], timeline=[],
                contradictions=[], excluded_claims=[])
    data["facts"] = [{**example("research", 3), "fact_id": "F001", "work_ids": [], "person_ids": [], "source_ids": ["S001"],
                      "claim": "Подтверждённая история производства фильма", "usage": "direct", "confidence": "high"}]
    data["sources"] = [{**example("research", 9), "source_id": "S001", "url": "https://example.com/r1", "supports": ["F001"]}]
    data["article_structure"] = [{**example("research", 10), "fact_ids": ["F001"], "quote_ids": [], "work_ids": []}]
    data["writer_handoff"].update(approved_fact_ids=["F001"], approved_quote_ids=[], forbidden_claim_ids=[])
    data["writer_handoff"]["sufficiency"].update(coverage="sufficient", missing_sections=[])
    return data


def test_all_invalid_reference_paths_are_reported():
    data = dossier()
    data["facts"][0]["source_ids"] = ["S015", "S099"]
    data["timeline"] = [{**example("research", 5), "fact_ids": [], "source_ids": ["S100"]}]
    issues = inspect_references(data)
    assert [i["path"] for i in issues] == [["facts", 0, "source_ids", 0], ["facts", 0, "source_ids", 1], ["timeline", 0, "source_ids", 0]]
    assert [i["actual"] for i in issues] == ["S015", "S099", "S100"]
    assert all(i["expected"] == ["S001"] for i in issues)


@pytest.mark.parametrize("reference", [" S001 ", "s001", "S1", "Ｓ００１", "s-0001"])
def test_normalizes_unambiguous_ids_without_mutating_receipt(reference):
    data = dossier()
    data["facts"][0]["source_ids"] = [reference]
    original = copy.deepcopy(data)
    result, report = parse_research(dumps(data))
    assert result.facts[0].source_ids == ["S001"]
    assert report["repairs"] and not report["quarantined"]
    assert data == original and result.research_status == "ready"


def test_isolates_unlinked_timeline_event_but_keeps_ready_facts():
    data = dossier()
    data["timeline"] = [{**example("research", 5), "event": "Unsupported date", "fact_ids": [], "source_ids": ["S015"]}]
    result, report = parse_research(dumps(data))
    assert not result.timeline and result.research_status == "ready"
    assert result.writer_handoff.approved_fact_ids == ["F001"]
    assert report["quarantined"][0]["item"] == data["timeline"][0]
    assert "S015" in result.research_limitations[-1]


def test_fact_quarantine_cascades_and_never_claims_sufficiency():
    data = dossier()
    data["facts"].append({**data["facts"][0], "fact_id": "F002", "source_ids": ["S404"]})
    data["writer_handoff"]["approved_fact_ids"].append("F002")
    data["sources"][0]["supports"].append("F002")
    data["article_structure"][0]["fact_ids"].append("F002")
    data["timeline"] = [{**example("research", 5), "fact_ids": ["F002"], "source_ids": ["S001"]}]
    result, report = parse_research(dumps(data))
    assert [f.fact_id for f in result.facts] == ["F001"]
    assert not result.article_structure and not result.timeline
    assert result.sources[0].supports == ["F001"]
    assert result.writer_handoff.approved_fact_ids == ["F001"]
    assert result.research_status == "editorial_review_required"
    assert result.writer_handoff.sufficiency.coverage == "limited"
    assert {q["path"][0] for q in report["quarantined"]} == {"facts", "timeline", "article_structure"}


def test_unverified_quote_and_dependants_are_isolated():
    data = dossier()
    data["quotes"] = [{**example("research", 4), "quote_id": "Q001", "source_ids": ["missing"], "usage": "direct", "verification_status": "verified"}]
    data["article_structure"][0]["quote_ids"] = ["Q001"]
    data["writer_handoff"]["approved_quote_ids"] = ["Q001"]
    result, _ = parse_research(dumps(data))
    assert not result.quotes and not result.article_structure
    assert not result.writer_handoff.approved_quote_ids
    assert result.research_status != "ready"


def test_ambiguous_ids_are_not_guessed():
    data = dossier()
    data["sources"].append({**data["sources"][0], "source_id": "S1"})
    data["facts"][0]["source_ids"] = ["s01"]
    result, report = parse_research(dumps(data))
    assert not result.facts and result.research_status != "ready"
    assert not any(r.get("after") == "S001" for r in report["repairs"])


def test_forbidden_claims_never_silently_disappear():
    data = dossier()
    data["writer_handoff"]["forbidden_claim_ids"] = ["C404"]
    with pytest.raises(ResearchReferenceError, match="writer_handoff.forbidden_claim_ids.0") as error:
        parse_research(dumps(data))
    assert error.value.issues[0]["actual"] == "C404"


@pytest.fixture
def paid_research(native_run):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 1)
    run = create_run(store, "2030-01-07")
    Pipeline(store, provider).execute(run)
    plan = store.one("SELECT id FROM content_plans")["id"]
    approve_plan(store, plan)
    concept = store.one("SELECT * FROM concepts WHERE plan_id=?", (plan,))
    run_row = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run,))
    service = ContentService(store, None, settings=json.loads(run_row["settings_json"]), prompts=json.loads(run_row["prompts_json"]), run_id=run)
    approved = approved_concept(service, concept)
    data = dossier()
    data.update(run_id=run, concept_id=approved["concept_id"], concept_revision=approved["revision"])
    data["facts"][0]["claim"] = approved["topic"]
    data["timeline"] = [{**example("research", 5), "fact_ids": [], "source_ids": ["S015"]}]
    response = {"choices": [{"finish_reason": "stop", "message": {"content": dumps(data)}}], "citations": ["https://example.com/r1"], "usage": {"cost_rub": 3}}
    key = f"run:{run}:refresh:{concept['id']}:v0"
    store.execute("INSERT INTO ai_calls(call_key,status,result_json,created_at,updated_at) VALUES(?,'SUCCEEDED',?,?,?)", (key, dumps(response), now_iso(), now_iso()))
    store.execute("INSERT INTO usage_events(timestamp,pipeline_run_id,operation,model,cost_rub,success,call_key) VALUES(?,?,'RESEARCH','sonar',3,1,?)", (now_iso(), run, key))
    store.execute("UPDATE pipeline_runs SET status='NEEDS_ACTION' WHERE id=?", (run,))
    return store, run, concept["id"], provider, api


def test_paid_replay_never_calls_ai_or_next_stages_and_preserves_history(paid_research, monkeypatch):
    store, run, concept, _, _ = paid_research
    tables = ("ai_calls", "usage_events", "analytics_results", "discovery_results", "prompt_versions", "content_plans")
    before = {t: store.rows(f"SELECT * FROM {t}") for t in tables}
    def forbidden(*a, **k):
        pytest.fail("Offline replay must never start HTTP, AI or pipeline")
    monkeypatch.setattr("dzen_agent.provider.AITunnel.call", forbidden)
    monkeypatch.setattr("httpx.Client.request", forbidden)
    monkeypatch.setattr(Pipeline, "execute", forbidden)
    for _ in range(2):
        report = reprocess_research(store, run, concept)
        assert report["restored"] and report["sufficient_for_writer"] and not report["next_paid_stages_started"]
        assert report["validation"]["issues"][0]["actual"] == "S015"
    assert {t: store.rows(f"SELECT * FROM {t}") for t in tables} == before
    assert not store.rows("SELECT * FROM articles")
    assert store.costs(run_id=run)["total"] == pytest.approx(3.15)
    assert store.one("SELECT needs_research FROM concepts")["needs_research"] == 0
    assert store.one("SELECT status FROM pipeline_stages WHERE stage='RESEARCH'")["status"] == "SUCCEEDED"


def test_resume_starts_writer_without_repaying_research(paid_research):
    store, run, concept, provider, api = paid_research
    reprocess_research(store, run, concept)
    before = len([c for c in api.calls if c[0] in ("research", "analytics", "discovery", "planner")])
    # Writer is reached with the restored dossier; native mock may reject the synthetic article,
    # but no prior stage or Research is dispatched again.
    Pipeline(store, provider).execute(run)
    assert any(stage == "writer" for stage, _, _ in api.calls)
    assert len([c for c in api.calls if c[0] in ("research", "analytics", "discovery", "planner")]) == before


def test_insufficient_replay_preserves_remaining_evidence_and_blocks_writer(paid_research):
    store, run, concept, _, _ = paid_research
    row = store.one("SELECT * FROM ai_calls WHERE call_key LIKE '%:refresh:%'")
    response = json.loads(row["result_json"])
    data = json.loads(response["choices"][0]["message"]["content"])
    data["facts"][0]["source_ids"] = ["S404"]
    response["choices"][0]["message"]["content"] = dumps(data)
    store.execute("UPDATE ai_calls SET result_json=? WHERE call_key=?", (dumps(response), row["call_key"]))
    cost = store.costs(run_id=run)
    report = reprocess_research(store, run, concept)
    assert not report["restored"] and not report["sufficient_for_writer"]
    assert store.one("SELECT needs_research FROM concepts")["needs_research"] == 1
    assert store.one("SELECT status FROM research_jobs")["status"] == "insufficient_research"
    assert store.costs(run_id=run) == cost
    assert not store.rows("SELECT * FROM articles")


@pytest.mark.parametrize("change", ["revision", "length", "writer"])
def test_replay_rejects_stale_incomplete_or_already_consumed_result(paid_research, change):
    from dzen_agent.errors import UserActionRequired
    store, run, concept, _, _ = paid_research
    row = store.one("SELECT * FROM ai_calls WHERE call_key LIKE '%:refresh:%'")
    response = json.loads(row["result_json"])
    if change == "revision":
        data = json.loads(response["choices"][0]["message"]["content"])
        data["concept_revision"] += 1
        response["choices"][0]["message"]["content"] = dumps(data)
    elif change == "length":
        response["choices"][0]["finish_reason"] = "length"
    else:
        store.execute("INSERT INTO ai_calls(call_key,status,created_at,updated_at) VALUES(?,'UNCERTAIN','now','now')", (f"run:{run}:write:{concept}",))
    store.execute("UPDATE ai_calls SET result_json=? WHERE call_key=?", (dumps(response), row["call_key"]))
    before = store.rows("SELECT * FROM research_jobs")
    with pytest.raises(UserActionRequired):
        reprocess_research(store, run, concept)
    assert store.rows("SELECT * FROM research_jobs") == before
