import json

import httpx
import pytest
from pydantic import ValidationError

from dzen_agent.editorial import EditorialChat, build_chat_context
from dzen_agent.errors import UserActionRequired
from dzen_agent.plan_review import apply_proposals, approve_plan, proposal_preview, reject_proposal
from dzen_agent.provider import AITunnel
from dzen_agent.schemas import EditorialReply
from dzen_agent.store import dumps


def test_compact_context_is_allowlisted_and_contains_plan(editorial_plan):
    store, run_id, plan_id, concepts = editorial_plan
    store.set_setting("channel_topic", "История кино")
    store.execute("INSERT INTO app_settings(key,value) VALUES('private_debug',?)", (dumps("SECRET"),))
    store.create("Недавняя тема", "Недавний заголовок", "PRIVATE_ARTICLE_BODY")
    data = build_chat_context(store, plan_id)
    assert data["week"] == "2030-01-07"
    assert [c["concept_id"] for c in data["plan"]] == concepts
    assert data["analytics"]["summary"]
    assert len(data["research"]) == 2
    assert data["research"][0]["sources"][0]["url"]
    assert data["history"][0]["title"] == "Недавний заголовок"
    assert data["channel_topic"] == "История кино"
    encoded = dumps(data)
    assert "SECRET" not in encoded and "PRIVATE_ARTICLE_BODY" not in encoded
    assert "api_key" not in encoded and "cookie" not in encoded
    assert len(encoded) < 36000


@pytest.mark.parametrize("proposal", [
    {"type": "sql", "concept_id": 1, "reason": "do it"},
    {"type": "update_concept", "concept_id": 1, "reason": "nothing"},
    {"type": "reschedule_concept", "concept_id": 1, "scheduled_at": "2030-01-07T10:00:00", "reason": "no zone"},
    {"type": "replace_concept", "concept_id": 1, "new_topic": "x", "reason": "missing angle"},
    {"type": "update_concept", "concept_id": 1, "working_title": "ok", "reason": "x", "research_job_id": 2},
])
def test_structured_editor_response_rejects_invalid_actions(proposal):
    with pytest.raises(ValidationError):
        EditorialReply.model_validate({"message": "Давайте", "proposals": [proposal]})


def test_chat_uses_current_planner_tracks_cost_and_never_applies(editorial_plan):
    store, run_id, plan_id, concepts = editorial_plan
    store.set_setting("planner_model", "current-planner")
    store.set_setting("research_use_web_tool", True)
    before = store.rows("SELECT * FROM concepts ORDER BY id")
    calls = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 400})
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": dumps({
            "message": "Можно уточнить заголовок", "proposals": [{"type": "update_concept",
            "concept_id": concepts[0], "working_title": "Новый заголовок", "reason": "Конкретнее"}]})}}],
            "usage": {"cost_rub": 0.17, "balance": 399.83}})

    api = AITunnel(store, "test", httpx.Client(transport=httpx.MockTransport(handler)))
    chat = EditorialChat(store, api)
    message_id = chat.send(plan_id, "Уточни первую идею")
    assert store.rows("SELECT * FROM concepts ORDER BY id") == before
    assert calls[0]["model"] == "current-planner"
    assert "tools" not in calls[0] and "max_tool_calls" not in calls[0]
    assert store.costs(run_id=run_id)["total"] == pytest.approx(0.17)
    assert store.one("SELECT operation FROM usage_events")["operation"] == "EDITOR_CHAT"
    assert chat.retry(chat.messages(plan_id)[0]["id"]) == message_id
    assert len(calls) == 1
    assert [m["role"] for m in EditorialChat(store, api).messages(plan_id)] == ["user", "assistant"]


def test_chat_history_is_separate_for_each_plan(plan_factory):
    store, _, first, _ = plan_factory()
    _, _, second, _ = plan_factory("2030-01-14")
    class FakeProvider:
        def call(self, **kwargs):
            return {"choices": [{"message": {"content": '{"message":"Обсудим","proposals":[]}'}}]}
    chat = EditorialChat(store, FakeProvider())
    chat.send(first, "Первый план")
    chat.send(second, "Второй план")
    assert chat.messages(first)[0]["content"] == "Первый план"
    assert chat.messages(second)[0]["content"] == "Второй план"
    assert len(chat.messages(first)) == len(chat.messages(second)) == 2


def test_apply_changes_only_target_and_title_needs_no_research(editorial_plan, proposal_message):
    store, run_id, plan_id, ids = editorial_plan
    approve_plan(store, plan_id)
    untouched = store.one("SELECT * FROM concepts WHERE id=?", (ids[1],))
    message_id = proposal_message(store, plan_id, [{"type": "update_concept", "concept_id": ids[0],
                                                   "working_title": "Необычный заголовок", "reason": "Точнее"}])
    item = json.loads(store.one("SELECT suggestions_json FROM editorial_chat_messages WHERE id=?",
                                (message_id,))["suggestions_json"])[0]
    assert proposal_preview(store, plan_id, item)["diff"][0]["field"] == "working_title"
    apply_proposals(store, message_id, [0])
    updated = store.one("SELECT * FROM concepts WHERE id=?", (ids[0],))
    assert json.loads(updated["payload_json"])["working_title"] == "Необычный заголовок"
    assert updated["needs_research"] == 0 and updated["research_version"] == 0
    assert updated["revision"] == 2
    assert store.one("SELECT * FROM concepts WHERE id=?", (ids[1],)) == untouched
    assert store.one("SELECT approved_at FROM content_plans WHERE id=?", (plan_id,))["approved_at"] is None
    assert store.one("SELECT status FROM pipeline_runs WHERE id=?", (run_id,))["status"] == "AWAITING_PLAN_REVIEW"
    with pytest.raises(ValueError):
        apply_proposals(store, message_id, [0])


@pytest.mark.parametrize("changes", [{"new_topic": "Как реставрируют киноплёнку"}, {"angle": "Экономика создания"}])
def test_topic_or_angle_change_marks_fresh_research(editorial_plan, proposal_message, changes):
    store, _, plan_id, ids = editorial_plan
    message_id = proposal_message(store, plan_id, [{"type": "update_concept", "concept_id": ids[0],
                                                   "reason": "Новая идея", **changes}])
    old = store.one("SELECT research_id FROM concepts WHERE id=?", (ids[0],))["research_id"]
    apply_proposals(store, message_id, [0])
    row = store.one("SELECT * FROM concepts WHERE id=?", (ids[0],))
    assert row["needs_research"] == 1 and row["research_version"] == 1
    assert row["research_id"] == old


def test_reject_changes_no_concepts(editorial_plan, proposal_message):
    store, _, plan_id, ids = editorial_plan
    before = store.rows("SELECT * FROM concepts ORDER BY id")
    message_id = proposal_message(store, plan_id, [{"type": "replace_concept", "concept_id": ids[0],
        "new_topic": "Новая тема", "working_title": "Заголовок", "angle": "Ракурс", "reason": "Свежая идея"}])
    reject_proposal(store, message_id, 0)
    assert store.rows("SELECT * FROM concepts ORDER BY id") == before
    with pytest.raises(ValueError):
        apply_proposals(store, message_id, [0])


def test_atomic_swap_and_wrong_week_rollback(editorial_plan, proposal_message):
    store, _, plan_id, ids = editorial_plan
    before = store.rows("SELECT * FROM concepts ORDER BY id")
    message_id = proposal_message(store, plan_id, [{"type": "reschedule_concept", "concept_id": ids[0],
        "scheduled_at": before[1]["scheduled_at"], "reason": "Переставить"},
        {"type": "reschedule_concept", "concept_id": ids[1], "scheduled_at": before[0]["scheduled_at"], "reason": "Переставить"}])
    with pytest.raises(ValueError, match="Перестановку"):
        apply_proposals(store, message_id, [0])
    assert store.rows("SELECT * FROM concepts ORDER BY id") == before
    apply_proposals(store, message_id, [0, 1])
    assert store.one("SELECT scheduled_at FROM concepts WHERE id=?", (ids[0],))["scheduled_at"] == before[1]["scheduled_at"]
    assert not store.one("SELECT needs_research FROM concepts WHERE id=?", (ids[0],))["needs_research"]
    invalid = proposal_message(store, plan_id, [{"type": "reschedule_concept", "concept_id": ids[0],
                                               "scheduled_at": "2030-02-01T12:00:00+03:00", "reason": "Bad"}])
    with pytest.raises(ValueError, match="неделе"):
        apply_proposals(store, invalid, [0])


def test_stale_or_cross_plan_proposals_cannot_overwrite(editorial_plan, proposal_message, plan_factory):
    store, _, plan_id, ids = editorial_plan
    first = proposal_message(store, plan_id, [{"type": "update_concept", "concept_id": ids[0],
                                              "working_title": "Вариант A", "reason": "A"}])
    second = proposal_message(store, plan_id, [{"type": "update_concept", "concept_id": ids[0],
                                               "working_title": "Вариант B", "reason": "B"}])
    apply_proposals(store, first, [0])
    with pytest.raises(ValueError, match="изменился"):
        apply_proposals(store, second, [0])
    _, _, other_plan, _ = plan_factory("2030-01-14")
    wrong = proposal_message(store, other_plan, [{"type": "update_concept", "concept_id": ids[0],
                                                "working_title": "Чужой", "reason": "Нельзя"}])
    with pytest.raises(ValueError):
        apply_proposals(store, wrong, [0])


def test_started_writer_makes_plan_read_only(editorial_plan, proposal_message):
    store, run_id, plan_id, ids = editorial_plan
    store.execute("UPDATE pipeline_stages SET attempts=1 WHERE run_id=? AND stage='WRITE'", (run_id,))
    message_id = proposal_message(store, plan_id, [{"type": "update_concept", "concept_id": ids[0],
                                                   "working_title": "Новый", "reason": "X"}])
    with pytest.raises(UserActionRequired, match="уже началось"):
        apply_proposals(store, message_id, [0])


def test_chat_obeys_existing_budget_guard(editorial_plan):
    store, _, plan_id, _ = editorial_plan
    store.set_setting("monthly_soft_budget_rub", 0)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"balance": 500})
    chat = EditorialChat(store, AITunnel(store, "test", httpx.Client(transport=httpx.MockTransport(handler))))
    with pytest.raises(UserActionRequired, match="бюджет"):
        chat.send(plan_id, "Обсудим идеи")
    assert not requests  # No generation and no paid retry when the local limit is reached.
    assert chat.messages(plan_id)[0]["status"] == "ERROR"


def test_chat_does_not_accept_a_model_proposal_for_another_plan(editorial_plan):
    store, _, plan_id, _ = editorial_plan
    class WrongPlan:
        def call(self, **kwargs):
            return {"choices": [{"message": {"content": dumps({"message": "Правка", "proposals": [
                {"type": "update_concept", "concept_id": 99999, "working_title": "Чужой", "reason": "X"}]})}}]}
    before = store.rows("SELECT * FROM concepts ORDER BY id")
    with pytest.raises(UserActionRequired, match="другой недели"):
        EditorialChat(store, WrongPlan()).send(plan_id, "Изменить тему")
    assert store.rows("SELECT * FROM concepts ORDER BY id") == before
