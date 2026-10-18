import json
from pathlib import Path

import pytest

from dzen_agent.pipeline.orchestrator import create_run
from dzen_agent.store import Store, dumps, now_iso


@pytest.fixture
def analytics_v1():
    return json.loads((Path(__file__).parent / "fixtures" / "analytics_v1.json").read_text("utf-8"))


@pytest.fixture
def plan_factory(tmp_path):
    store = Store(tmp_path / "editorial.db")
    store.initialize()
    for role in ("analytics", "research", "planner", "writer", "image"):
        store.set_setting(role + "_model", role + "-test")
    store.set_setting("max_articles_per_week", 2)
    store.set_setting("pipeline_flow", "legacy")  # Fixture represents an existing pre-Discovery run.

    def create(week="2030-01-07"):
        run_id = create_run(store, week)
        plan_id = store.execute("INSERT INTO content_plans(run_id,week_start,created_at) VALUES(?,?,?)",
                                (run_id, week, now_iso()))
        analytics = {"summary": "Разные темы полезны", "best_topics": [], "weak_topics": [],
                     "useful_patterns": [], "headline_patterns": [], "recommendations": [],
                     "topics_to_avoid": [], "opportunities": ["Кино", "Литература"]}
        store.execute("INSERT INTO analytics_results(run_id,payload_json,created_at) VALUES(?,?,?)",
                       (run_id, dumps(analytics), now_iso()))
        concepts = []
        for index, topic in enumerate(["История советской анимации", "Режиссёрские дневники"]):
            sources = [{"id": "s1", "url": "https://example.com/a", "title": "Источник A"},
                       {"id": "s2", "url": "https://example.com/b", "title": "Источник B"}]
            research = {"status": "sufficient", "summary": "Справка о " + topic,
                        "key_facts": ["Проверенный факт"], "claims": [{"text": "Факт", "source_ids": ["s1"]}],
                        "sources": sources, "contradictions": [], "uncertainty": [], "suggested_angles": []}
            research_id = store.execute("INSERT INTO research_jobs(run_id,topic,status,payload_json,created_at) "
                                         "VALUES(?,?,'sufficient',?,?)", (run_id, topic, dumps(research), now_iso()))
            from datetime import datetime, timedelta
            scheduled = (datetime.fromisoformat(week) + timedelta(days=index)).replace(hour=12)
            slot = scheduled.isoformat() + "+03:00"
            payload = {"topic": topic, "working_title": topic + ": взгляд изнутри", "angle": "История создания",
                       "why_now": "Польза вне новостного контекста", "audience_value": "Узнать новое",
                       "thesis": "За результатом стоят люди", "key_points": ["Сценарий", "Производство"],
                       "research_job_id": research_id, "research_source_ids": ["s1", "s2"],
                       "suggested_publication_date": scheduled.date().isoformat(),
                       "suggested_publication_time": "12:00", "score": 80}
            concept_id = store.execute("INSERT INTO concepts(plan_id,research_id,topic,payload_json,scheduled_at,status) "
                                       "VALUES(?,?,?,?,?,'PLANNED')", (plan_id, research_id, topic, dumps(payload), slot))
            concepts.append(concept_id)
        store.execute("UPDATE pipeline_stages SET status='SUCCEEDED' WHERE run_id=? "
                       "AND stage IN ('COLLECT_STATS','ANALYZE','RESEARCH','PLAN')", (run_id,))
        return store, run_id, plan_id, concepts

    return create


@pytest.fixture
def editorial_plan(plan_factory):
    return plan_factory()


@pytest.fixture
def proposal_message():
    def create(store, plan_id, proposals):
        items = []
        for proposal in proposals:
            row = store.one("SELECT revision FROM concepts WHERE id=?", (proposal["concept_id"],))
            items.append({"proposal": proposal, "base_revision": row["revision"], "status": "PENDING"})
        return store.execute("INSERT INTO editorial_chat_messages(plan_id,role,content,suggestions_json,created_at) "
                             "VALUES(?,'assistant','Обсудим изменения',?,?)", (plan_id, json.dumps(items), now_iso()))
    return create
