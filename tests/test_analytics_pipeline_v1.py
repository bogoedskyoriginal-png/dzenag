import base64
import copy
import io
import json

import httpx
import pytest
from PIL import Image

from dzen_agent.pipeline.orchestrator import Pipeline, approve_week, create_run
from dzen_agent.plan_review import approve_plan
from dzen_agent.prompt_service import Prompts
from dzen_agent.provider import AITunnel
from dzen_agent.recovery import restart_analytics
from dzen_agent.schemas import Discovery
from dzen_agent.statistics import import_csv, preview_csv
from dzen_agent.store import Store, dumps


def setup_run(tmp_path, *, discovery_prompt=True):
    store = Store(tmp_path / "pipeline.db")
    store.initialize()
    prompts = Prompts(store)
    prompts.seed()
    if discovery_prompt:
        prompts.save("DISCOVERY", "Explicitly approved test prompt", "Ищи разные идеи с проверенными ссылками.")
    for role in ("analytics", "discovery", "planner", "research", "writer", "image"):
        store.set_setting(role + "_model", role + "-test")
    store.set_setting("max_articles_per_week", 2)
    import_csv(store, "stats.csv", preview_csv(b"article_id;title;views\na;First;300\nb;Second;200"))
    return store, create_run(store, "2030-01-07")


def test_full_v1_flow_briefs_research_gate_and_replay(tmp_path, analytics_v1):
    store, run_id = setup_run(tmp_path)
    requests = []
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(buffer, "PNG")
    topics = ["Как восстанавливают цветную киноплёнку", "Работа художника по свету в советском кино"]
    discovery = {"status": "ok", "summary": "Разные темы", "uncertainty": [],
        "sources": [{"id": "D1", "url": "https://example.com/d1", "title": "Каталог"},
                    {"id": "D2", "url": "https://example.com/d2", "title": "Архив"}],
        "candidates": [{"id": f"C{i}", "topic": topic, "working_title": topic, "angle": "Люди и ремесло",
                        "why_now": "Evergreen", "audience_value": "Узнать о кино", "thesis": "Важен труд мастеров",
                        "key_points": ["Техника", "Результат"], "research_questions": ["Какие факты подтверждены?"],
                        "source_ids": [f"D{i + 1}"], "kind": "evergreen", "score": 80}
                       for i, topic in enumerate(topics)]}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 500})
        payload = json.loads(request.content)
        role = payload["model"].removesuffix("-test")
        text = payload.get("messages", [{}, {"content": ""}])[1]["content"] if role != "image" else ""
        context = json.loads(text) if text.startswith("{") else json.loads(
            text.split("\nВерни только JSON по этой схеме:")[0].split("\n", 1)[1]) if text else {}
        requests.append((role, context))
        if role == "image":
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(buffer.getvalue()).decode()}],
                                             "usage": {"cost_rub": 0.05}})
        if role == "analytics":
            output = analytics_v1
            assert "tools" not in payload
        elif role == "discovery":
            assert context["discovery_brief"] == analytics_v1["discovery_brief"]
            output = discovery
        elif role == "planner":
            assert context["planner_brief"] == analytics_v1["planner_brief"]
            assert context["discovery"] == Discovery.model_validate(discovery).model_dump(mode="json")
            assert context["research"] == []
            output = {"concepts": [{"topic": c["topic"], "working_title": c["working_title"], "angle": c["angle"],
                "why_now": c["why_now"], "audience_value": c["audience_value"], "thesis": c["thesis"],
                "key_points": c["key_points"], "research_job_id": c["research_job_id"],
                "research_source_ids": c["source_ids"], "suggested_publication_date": slot[:10],
                "suggested_publication_time": "12:00", "score": c["score"]}
                for c, slot in zip(context["planning_candidates"], context["schedule"], strict=True)]}
        elif role == "research":
            output = {"status": "sufficient", "summary": context["topic"], "key_facts": ["Факт"],
                "claims": [{"text": "Факт", "source_ids": ["R1"]}], "sources": [
                    {"id": "R1", "url": "https://example.com/r1", "title": "Исследование"},
                    {"id": "R2", "url": "https://example.com/r2", "title": "Подтверждение"}],
                "contradictions": [], "uncertainty": [], "suggested_angles": []}
        else:
            assert context["sources"][0]["id"] == "R1"  # Writer never substitutes Discovery for Research.
            output = {"title": context["topic"], "body": context["topic"] * 10,
                      "short_summary": "Summary", "source_ids": ["R1"]}
        return httpx.Response(200, json={"choices": [{"message": {"content": dumps(output)}}],
            "citations": ["https://example.com/d1", "https://example.com/d2", "https://example.com/r1", "https://example.com/r2"],
            "usage": {"cost_rub": 0.05}})

    api = AITunnel(store, "test", httpx.Client(transport=httpx.MockTransport(handler)))
    pipeline = Pipeline(store, api)
    assert pipeline.execute(run_id) == "AWAITING_PLAN_REVIEW"
    assert [r for r, _ in requests] == ["analytics", "discovery", "planner"]
    assert not store.list_all()
    assert {row["status"] for row in store.rows("SELECT status FROM research_jobs")} == {"pending"}
    assert pipeline.execute(run_id) == "AWAITING_PLAN_REVIEW"
    assert len(requests) == 3
    approve_plan(store, store.one("SELECT id FROM content_plans WHERE run_id=?", (run_id,))["id"])
    assert pipeline.execute(run_id) == "AWAITING_REVIEW"
    assert [r for r, _ in requests] == ["analytics", "discovery", "planner", "research", "research", "writer", "writer", "image", "image"]
    assert pipeline.execute(run_id) == "AWAITING_REVIEW"
    assert len(requests) == 9
    assert store.costs(run_id=run_id)["total"] == pytest.approx(0.45)
    approve_week(store, run_id)
    assert pipeline.execute(run_id) == "AWAITING_SEND"


def test_insufficient_data_stops_paid_discovery_and_explicit_retry_keeps_old_result(tmp_path, analytics_v1):
    store, run_id = setup_run(tmp_path, discovery_prompt=False)
    assert Prompts(store).active("DISCOVERY") is None
    insufficient = copy.deepcopy(analytics_v1)
    insufficient.update(status="insufficient_data", observations=[], interpretations=[], experiments=[], cluster_signals=[])
    insufficient["data_quality"]["articles_usable"] = 0
    insufficient["planner_brief"]["priority_experiment_ids"] = []
    response = [insufficient]
    requests = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 500})
        requests.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": dumps(response[0])}}], "usage": {"cost_rub": 0.1}})
    pipeline = Pipeline(store, AITunnel(store, "test", httpx.Client(transport=httpx.MockTransport(handler))))
    assert pipeline.execute(run_id) == "NEEDS_ACTION"
    assert "недостаточно данных" in store.one("SELECT error FROM pipeline_runs WHERE id=?", (run_id,))["error"]
    assert pipeline.execute(run_id) == "NEEDS_ACTION"
    assert requests == ["analytics-test"]
    old = store.one("SELECT payload_json FROM analytics_results")["payload_json"]
    with pytest.raises(Exception):
        restart_analytics(store, run_id, confirmed=False)
    restart_analytics(store, run_id, confirmed=True)
    response[0] = analytics_v1
    assert pipeline.execute(run_id) == "NEEDS_ACTION"  # No authored Discovery prompt; no silent substitute.
    assert "промпт DISCOVERY" in store.one("SELECT error FROM pipeline_runs WHERE id=?", (run_id,))["error"]
    assert requests == ["analytics-test", "analytics-test"]
    rows = store.rows("SELECT payload_json,superseded FROM analytics_results ORDER BY id")
    assert rows[0] == {"payload_json": old, "superseded": 1}
    assert rows[1]["superseded"] == 0
    assert store.costs(run_id=run_id)["total"] == pytest.approx(0.2)
