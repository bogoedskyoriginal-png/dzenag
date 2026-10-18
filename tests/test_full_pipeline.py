import base64
import io
import json
from pathlib import Path

import httpx
from PIL import Image

from dzen_agent.pipeline.orchestrator import Pipeline, approve_week, create_run
from dzen_agent.prompt_service import Prompts
from dzen_agent.provider import AITunnel
from dzen_agent.plan_review import approve_plan
from dzen_agent.statistics import import_csv, preview_csv
from dzen_agent.store import Store


def test_full_pipeline_resume_keeps_paid_text_and_images(tmp_path):
    store = Store(tmp_path / "pipeline.db")
    store.initialize()
    Prompts(store).seed()
    for role in ("analytics", "research", "planner", "writer", "image"):
        store.set_setting(role + "_model", role)
    store.set_setting("max_articles_per_week", 2)
    store.set_setting("max_retries", 0)
    store.set_setting("pipeline_flow", "legacy")
    import_csv(store, "stats.csv", preview_csv(b"title;views\nfirst;10\nsecond;3"))
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(buffer, "PNG")
    counts = {role: 0 for role in ("analytics", "research", "planner", "writer", "image")}
    topics = ["Кофемолка и размер помола", "Уход за комнатными орхидеями"]

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 500})
        payload = json.loads(request.content)
        role = payload["model"]
        counts[role] += 1
        if role == "image":
            if counts[role] == 2:
                return httpx.Response(429, json={"error": "rate limit"})
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(
                buffer.getvalue()).decode()}], "usage": {"cost_rub": 1, "balance": 490}})
        context_text = payload["messages"][1]["content"]
        context = json.loads(context_text) if context_text.startswith("{") else json.loads(
            context_text.split("\nВерни только JSON по этой схеме:")[0].split("\n", 1)[1])
        if role == "analytics":
            output = json.loads((Path(__file__).parent / "fixtures" / "analytics_v1.json").read_text("utf-8"))
            output["observations"][0]["article_ids"] = [context["articles"][0]["article_id"]]
            output["observations"][0]["value"] = 10
            output["discovery_brief"]["promising_directions"] = topics
        elif role == "research":
            output = {"status": "sufficient", "summary": context["topic"], "key_facts": ["Fact"],
                      "claims": [{"text": "Fact", "source_ids": ["s1"]}], "sources": [
                          {"id": "s1", "url": "https://example.com/a", "title": "A"},
                          {"id": "s2", "url": "https://example.com/b", "title": "B"}],
                      "contradictions": [], "uncertainty": [], "suggested_angles": []}
        elif role == "planner":
            output = {"concepts": [{"topic": r["topic"], "working_title": r["topic"],
                "angle": "angle", "why_now": "now", "audience_value": "value", "thesis": "thesis",
                "key_points": ["point"], "research_job_id": r["research_job_id"],
                "research_source_ids": ["s1"], "suggested_publication_date": "2030-01-07",
                "suggested_publication_time": "12:00", "score": 80} for r in context["research"]]}
        else:
            output = {"title": context["topic"], "body": context["topic"] * 10,
                      "short_summary": "Summary", "source_ids": ["s1"]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)}}],
            "citations": ["https://example.com/a", "https://example.com/b"],
            "usage": {"cost_rub": 1, "balance": 490}})

    api = AITunnel(store, "key", httpx.Client(transport=httpx.MockTransport(handler)))
    pipeline = Pipeline(store, api)
    run_id = create_run(store, "2030-01-07")
    assert pipeline.execute(run_id) == "AWAITING_PLAN_REVIEW"
    assert len(store.list_all()) == 0
    assert counts["writer"] == 0
    approve_plan(store, store.one("SELECT id FROM content_plans WHERE run_id=?", (run_id,))["id"])
    assert pipeline.execute(run_id) == "NEEDS_ACTION"
    assert len(store.list_all()) == 2
    assert pipeline.execute(run_id) == "AWAITING_REVIEW"
    assert counts == {"analytics": 1, "research": 2, "planner": 1, "writer": 2, "image": 3}
    assert store.costs(run_id=run_id)["total"] == 8
    approve_week(store, run_id)
    assert pipeline.execute(run_id) == "AWAITING_SEND"
    article = store.list_all()[0]
    store.update_content(article.id, article.title + " обновлено", article.body)
    assert store.one("SELECT approved_at FROM pipeline_runs WHERE id=?", (run_id,))["approved_at"] is None
    assert pipeline.execute(run_id, allow_dzen=True) == "AWAITING_REVIEW"
    assert counts["writer"] == 2
