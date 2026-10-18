import json
from unittest.mock import Mock

import httpx
import pytest

from dzen_agent.errors import UserActionRequired
from dzen_agent.pipeline.orchestrator import Pipeline
from dzen_agent.plan_review import apply_proposals, approve_plan
from dzen_agent.provider import AITunnel
from dzen_agent.services import ContentService
from dzen_agent.store import dumps, now_iso


@pytest.mark.parametrize("mode", ["REVIEW", "MANUAL"])
def test_plan_review_stops_before_any_writing(editorial_plan, mode):
    store, run_id, _, _ = editorial_plan
    store.set_setting("mode", mode)
    service_factory = Mock()
    pipeline = Pipeline(store, None, service_factory=service_factory)
    for _ in range(2):
        assert pipeline.execute(run_id) == "AWAITING_PLAN_REVIEW"
    service_factory.return_value.write.assert_not_called()
    stage = store.one("SELECT * FROM pipeline_stages WHERE run_id=? AND stage='PLAN_REVIEW'", (run_id,))
    assert stage["status"] == "WAITING" and stage["started_at"]


def test_autopilot_skips_plan_gate(editorial_plan):
    store, run_id, _, _ = editorial_plan
    store.set_setting("mode", "AUTOPILOT")
    store.set_setting("dzen_autopilot_authorized", True)
    stages = []

    class Controlled(Pipeline):
        def _stage(self, stage, run, service):
            stages.append(stage)
    assert Controlled(store, None, dzen=Mock()).execute(run_id) == "SUCCEEDED"
    assert "WRITE" in stages
    assert store.one("SELECT status FROM pipeline_stages WHERE run_id=? AND stage='PLAN_REVIEW'", (run_id,))["status"] == "SUCCEEDED"


def test_refresh_only_changed_concept_is_paid_once_and_resumable(editorial_plan, proposal_message):
    store, run_id, plan_id, ids = editorial_plan
    originals = store.rows("SELECT * FROM concepts ORDER BY id")
    store.set_setting("max_research_calls_per_article", 1)
    for concept_id, topic in zip(ids, ["Технологии реставрации киноплёнки", "Профессия художника по свету"], strict=True):
        message = proposal_message(store, plan_id, [{"type": "update_concept", "concept_id": concept_id,
                                                    "new_topic": topic, "reason": "Новый взгляд"}])
        apply_proposals(store, message, [0])
    approve_plan(store, plan_id)
    store.execute("UPDATE pipeline_runs SET settings_json=? WHERE id=?", (dumps(store.settings()), run_id))
    requests = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 500})
        requests.append(json.loads(request.content))
        research = {"status": "sufficient", "summary": "Новая проверенная фактура", "key_facts": ["Новый факт"],
                    "claims": [{"text": "Новый факт", "source_ids": ["fresh1"]}], "sources": [
                        {"id": "fresh1", "url": "https://example.com/fresh1", "title": "Новый источник"},
                        {"id": "fresh2", "url": "https://example.com/fresh2", "title": "Второй источник"}],
                    "contradictions": [], "uncertainty": [], "suggested_angles": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": dumps(research)}}],
            "citations": ["https://example.com/fresh1", "https://example.com/fresh2"],
            "usage": {"cost_rub": 0.5, "balance": 499}})

    api = AITunnel(store, "test", httpx.Client(transport=httpx.MockTransport(handler)))
    interruptions = []

    class InterruptedService(ContentService):
        def research(self, *args, **kwargs):
            if "Технологии" not in args[0] and not interruptions:
                interruptions.append(1)
                raise UserActionRequired("Остановка между исследованиями")
            return super().research(*args, **kwargs)

    class StopAtWriter(Pipeline):
        def _stage(self, stage, run, service):
            if stage == "WRITE":
                raise UserActionRequired("Тест остановлен перед платным Writer")
            return super()._stage(stage, run, service)

    pipeline = StopAtWriter(store, api, service_factory=InterruptedService)
    assert pipeline.execute(run_id) == "NEEDS_ACTION"
    assert len(requests) == 1
    first = store.one("SELECT * FROM concepts WHERE id=?", (ids[0],))
    untouched = store.one("SELECT * FROM concepts WHERE id=?", (ids[1],))
    assert first["research_id"] != originals[0]["research_id"] and first["needs_research"] == 0
    assert untouched["research_id"] == originals[1]["research_id"] and untouched["needs_research"] == 1
    assert pipeline.execute(run_id) == "NEEDS_ACTION"
    assert len(requests) == 2
    assert pipeline.execute(run_id) == "NEEDS_ACTION"
    assert len(requests) == 2  # No successful research is repeated, including across partial failure.
    assert store.costs(run_id=run_id)["total"] == 1
    assert len(store.rows("SELECT * FROM research_jobs WHERE run_id=?", (run_id,))) == 4
    payload = json.loads(store.one("SELECT payload_json FROM concepts WHERE id=?", (ids[0],))["payload_json"])
    assert payload["research_source_ids"] == ["fresh1", "fresh2"]


def test_title_change_reuses_research_and_writer_refuses_stale_data(editorial_plan, proposal_message):
    store, run_id, plan_id, ids = editorial_plan
    message = proposal_message(store, plan_id, [{"type": "update_concept", "concept_id": ids[0],
                                               "working_title": "Уточнённый заголовок", "reason": "Лучше"}])
    apply_proposals(store, message, [0])
    approve_plan(store, plan_id)
    api = Mock()

    class StopAtWriter(Pipeline):
        def _stage(self, stage, run, service):
            if stage == "WRITE":
                raise UserActionRequired("Тест")
            return super()._stage(stage, run, service)

    assert StopAtWriter(store, api).execute(run_id) == "NEEDS_ACTION"
    api.call.assert_not_called()
    row = store.one("SELECT * FROM concepts WHERE id=?", (ids[0],))
    row["needs_research"] = 1
    with pytest.raises(UserActionRequired, match="свежее исследование"):
        ContentService(store, api).write(row["topic"], row["research_id"], "test", concept_row=row)


def test_insufficient_refresh_blocks_only_affected_writer(editorial_plan):
    store, run_id, plan_id, ids = editorial_plan
    store.execute("UPDATE concepts SET needs_research=1,research_version=1 WHERE id=?", (ids[0],))
    approve_plan(store, plan_id)

    written = []
    class NoResearch(ContentService):
        def research(self, *args, **kwargs):
            return self.store.execute("INSERT INTO research_jobs(run_id,topic,status,payload_json,created_at) "
                                      "VALUES(?,'failed','insufficient_research','{}',?)", (run_id, now_iso()))
        def write(self, *args, **kwargs):
            concept_id = kwargs["concept_row"]["id"]
            assert concept_id != ids[0], "Failed Research must never reach Writer"
            written.append(concept_id)
            raise UserActionRequired("Mock Writer: no paid transport")

    assert Pipeline(store, None, service_factory=NoResearch).execute(run_id) == "NEEDS_ACTION"
    assert store.one("SELECT needs_research FROM concepts WHERE id=?", (ids[0],))["needs_research"] == 1
    assert written == [ids[1]]
