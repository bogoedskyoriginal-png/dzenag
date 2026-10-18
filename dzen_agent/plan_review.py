"""Explicit, revision-aware plan approvals and atomic application of editorial proposals."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta

from dzen_agent.errors import UserActionRequired
from dzen_agent.locking import exclusive_lock
from dzen_agent.schemas import Concept, EditorialProposal
from dzen_agent.services import normalize, similar
from dzen_agent.store import MSK, Store, dumps, now_iso


def is_approved(plan: dict) -> bool:
    return bool(plan["approved_at"] and plan["approved_revision"] == plan["revision"])


def editing_reason(store: Store, plan_id: int) -> str | None:
    with store._connect() as conn:
        plan = conn.execute("SELECT * FROM content_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            return "Контент-план не найден."
        return _editing_reason(conn, plan)


def _editing_reason(conn, plan):
    started = conn.execute("SELECT 1 FROM pipeline_stages WHERE run_id=? AND stage='WRITE' "
                           "AND (attempts>0 OR status='SUCCEEDED')", (plan["run_id"],)).fetchone()
    articles = conn.execute("SELECT 1 FROM article_details WHERE run_id=?", (plan["run_id"],)).fetchone()
    if started or articles:
        return ("Создание статей уже началось. Груминг доступен для обсуждения; "
                "готовые материалы редактируются в разделе «Статьи».")
    week_end = datetime.fromisoformat(plan["week_start"]).date() + timedelta(days=7)
    if datetime.now(MSK).date() >= week_end:
        return "Эта неделя уже завершилась. История груминга доступна для просмотра и обсуждения."
    return None


def changed_concept(row, proposal: EditorialProposal, week_start: str) -> tuple[dict, str, bool]:
    payload = json.loads(row["payload_json"])
    old_topic, old_angle = payload["topic"], payload["angle"]
    for field in ("working_title", "angle", "why_now", "audience_value", "thesis", "key_points"):
        value = getattr(proposal, field)
        if value is not None:
            payload[field] = value
    if proposal.new_topic is not None:
        payload["topic"] = proposal.new_topic
    scheduled_at = row["scheduled_at"]
    if proposal.scheduled_at:
        scheduled = proposal.scheduled_at.astimezone(MSK)
        if scheduled.second or scheduled.microsecond:
            raise ValueError("Укажите время с точностью до минуты.")
        scheduled_at = scheduled.isoformat()
        payload["suggested_publication_date"] = scheduled.date().isoformat()
        payload["suggested_publication_time"] = scheduled.strftime("%H:%M")
    _validate_slot(scheduled_at, week_start)
    payload = Concept.model_validate(payload).model_dump(mode="json")
    fresh = (proposal.type == "replace_concept" or normalize(old_topic) != normalize(payload["topic"])
             or normalize(old_angle) != normalize(payload["angle"]))
    if payload.get("editorial_contract"):
        item = payload["editorial_contract"]
        if proposal.type == "replace_concept":
            payload["thesis"] = proposal.thesis or proposal.angle
            payload["audience_value"] = proposal.audience_value or proposal.new_topic
            payload["why_now"] = proposal.why_now or "Новая тема предложена редактором."
            payload["key_points"] = proposal.key_points or [
                f"Проверить тему «{proposal.new_topic}» и ракурс «{proposal.angle}» по независимым источникам."]
        item.update(topic=payload["topic"], working_headline=payload["working_title"], angle=payload["angle"],
                    key_thesis=payload["thesis"], reader_promise=payload["audience_value"],
                    scheduled_at=scheduled_at, approval_status="pending_review", revision=row["revision"] + 1)
        if proposal.type == "replace_concept":
            item.update(source_idea_id=None, initial_sources=[])
            item["research_brief"].update(initial_source_ids=[], essential_entities=[], selection_subjects=[])
        item["research_brief"].update(objective=payload["topic"] + ": " + payload["angle"],
                                     questions=payload["key_points"])
        # Native dossiers are pinned to the full concept revision, including editorial changes.
        fresh = True
    return payload, scheduled_at, fresh


def _validate_slot(value, week_start):
    scheduled = datetime.fromisoformat(value)
    if scheduled.tzinfo is None:
        raise ValueError("Дата публикации должна содержать часовой пояс.")
    start = datetime.fromisoformat(week_start).replace(tzinfo=MSK)
    if not start <= scheduled < start + timedelta(days=7):
        raise ValueError("Новая дата должна относиться к выбранной неделе (по Москве).")
    if scheduled <= datetime.now(MSK):
        raise ValueError("Время публикации должно быть в будущем.")


def proposal_preview(store: Store, plan_id: int, suggestion: dict) -> dict:
    proposal = EditorialProposal.model_validate(suggestion["proposal"])
    row = store.one("SELECT * FROM concepts WHERE id=? AND plan_id=?", (proposal.concept_id, plan_id))
    plan = store.one("SELECT * FROM content_plans WHERE id=?", (plan_id,))
    if not row or not plan or row["revision"] != suggestion["base_revision"]:
        raise ValueError("Концепт уже изменился. Обсудите актуальную версию и запросите новое предложение.")
    payload, slot, fresh = changed_concept(row, proposal, plan["week_start"])
    before = {**json.loads(row["payload_json"]), "scheduled_at": row["scheduled_at"]}
    after = {**payload, "scheduled_at": slot}
    diff = [{"field": key, "before": before.get(key), "after": value}
            for key, value in after.items() if value != before.get(key)]
    return {"concept": row, "diff": diff, "needs_research": fresh or bool(row["needs_research"])}


def _validate_final_slots(conn, plan, rows):
    occupied = set()
    run = conn.execute("SELECT settings_json FROM pipeline_runs WHERE id=?", (plan["run_id"],)).fetchone()
    settings = json.loads(run["settings_json"])
    maximum = settings.get("max_articles_per_day", max(1, len(settings.get("publication_times") or [])))
    daily = Counter(datetime.fromisoformat(row["scheduled_at"]).astimezone(MSK).date() for row in rows)
    for row in rows:
        _validate_slot(row["scheduled_at"], plan["week_start"])
        canonical = datetime.fromisoformat(row["scheduled_at"]).astimezone(MSK).isoformat()
        if canonical in occupied:
            raise ValueError("Две публикации назначены на одно время. Перестановку примените вместе.")
        occupied.add(canonical)
        other = conn.execute("SELECT id,scheduled_at FROM concepts WHERE plan_id<>?", (plan["id"],)).fetchall()
        article_slots = conn.execute("SELECT scheduled_at FROM article_details WHERE scheduled_at IS NOT NULL "
                                     "AND (run_id IS NULL OR run_id<>?)", (plan["run_id"],)).fetchall()
        if any(datetime.fromisoformat(s["scheduled_at"]) == datetime.fromisoformat(canonical)
               for s in list(other) + list(article_slots)):
            raise ValueError("Это время уже занято другой статьёй или контент-планом.")
    if any(count > maximum for count in daily.values()):
        raise ValueError("Превышено максимальное число публикаций в день для этой недели.")


def apply_proposals(store: Store, message_id: int, indices: list[int]) -> None:
    if not indices or len(set(indices)) != len(indices):
        raise ValueError("Выберите предложения для применения.")
    with exclusive_lock(store.database_path.parent / "pipeline.lock"), store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        message = conn.execute("SELECT * FROM editorial_chat_messages WHERE id=? AND role='assistant'",
                               (message_id,)).fetchone()
        if not message:
            raise ValueError("Предложение не найдено.")
        plan = conn.execute("SELECT * FROM content_plans WHERE id=?", (message["plan_id"],)).fetchone()
        reason = _editing_reason(conn, plan)
        if reason:
            raise UserActionRequired(reason)
        suggestions = json.loads(message["suggestions_json"])
        current = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM concepts WHERE plan_id=?",
                                                          (plan["id"],)).fetchall()}
        updates, selected_ids = [], set()
        for index in indices:
            if not 0 <= index < len(suggestions):
                raise ValueError("Предложение не найдено.")
            item = suggestions[index]
            if item["status"] != "PENDING":
                raise ValueError("Это предложение уже обработано.")
            proposal = EditorialProposal.model_validate(item["proposal"])
            row = current.get(proposal.concept_id)
            if not row or row["revision"] != item["base_revision"]:
                raise ValueError("Концепт уже изменился. Запросите предложение для актуального плана.")
            if row["id"] in selected_ids:
                raise ValueError("Выберите один вариант для каждого концепта.")
            selected_ids.add(row["id"])
            payload, slot, fresh = changed_concept(row, proposal, plan["week_start"])
            if payload == json.loads(row["payload_json"]) and slot == row["scheduled_at"] and not fresh:
                raise ValueError("Предложение не содержит изменений.")
            updates.append((row, payload, slot, fresh))
            current[row["id"]] = {**row, "topic": payload["topic"], "scheduled_at": slot}
        _validate_final_slots(conn, plan, list(current.values()))
        for row, payload, slot, fresh in updates:
            if payload["topic"] != row["topic"]:
                other_topics = [r["topic"] for r in current.values() if r["id"] != row["id"]]
                other_topics += [r[0] for r in conn.execute("SELECT topic FROM articles").fetchall()]
                if any(similar(payload["topic"], topic) for topic in other_topics):
                    raise ValueError("Новая тема слишком похожа на другую тему или уже созданную статью.")
            needs = bool(row["needs_research"]) or fresh
            conn.execute("UPDATE concepts SET topic=?,payload_json=?,scheduled_at=?,revision=revision+1,"
                         "needs_research=?,research_version=research_version+?,status=? WHERE id=?",
                         (payload["topic"], dumps(payload), slot, int(needs), int(fresh),
                          "RESEARCH_REQUIRED" if needs else "PLANNED", row["id"]))
        for index in indices:
            suggestions[index]["status"] = "APPLIED"
            suggestions[index]["resolved_at"] = now_iso()
        conn.execute("UPDATE editorial_chat_messages SET suggestions_json=? WHERE id=?",
                     (dumps(suggestions), message_id))
        conn.execute("UPDATE content_plans SET revision=revision+1,approved_revision=NULL,approved_at=NULL "
                     "WHERE id=?", (plan["id"],))
        conn.execute("UPDATE pipeline_runs SET approved_at=NULL,status='AWAITING_PLAN_REVIEW',error=NULL "
                     "WHERE id=?", (plan["run_id"],))
        conn.execute("UPDATE pipeline_stages SET status='PENDING',error=NULL,ended_at=NULL "
                     "WHERE run_id=? AND stage IN ('PLAN_REVIEW','REFRESH_RESEARCH')", (plan["run_id"],))


def reject_proposal(store: Store, message_id: int, index: int) -> None:
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        message = conn.execute("SELECT * FROM editorial_chat_messages WHERE id=? AND role='assistant'",
                               (message_id,)).fetchone()
        if not message:
            raise ValueError("Предложение не найдено.")
        suggestions = json.loads(message["suggestions_json"])
        if not 0 <= index < len(suggestions) or suggestions[index]["status"] != "PENDING":
            raise ValueError("Предложение уже обработано или не найдено.")
        suggestions[index]["status"] = "REJECTED"
        suggestions[index]["resolved_at"] = now_iso()
        conn.execute("UPDATE editorial_chat_messages SET suggestions_json=? WHERE id=?",
                     (dumps(suggestions), message_id))


def approve_plan(store: Store, plan_id: int) -> None:
    with exclusive_lock(store.database_path.parent / "pipeline.lock"), store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        plan = conn.execute("SELECT * FROM content_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise ValueError("Контент-план не найден.")
        rows = conn.execute("SELECT * FROM concepts WHERE plan_id=?", (plan_id,)).fetchall()
        run = conn.execute("SELECT * FROM pipeline_runs WHERE id=?", (plan["run_id"],)).fetchone()
        expected = json.loads(run["settings_json"])["max_articles_per_week"]
        if not rows or len(rows) != expected:
            raise ValueError("Контент-план ещё не сформирован полностью.")
        for row in rows:
            Concept.model_validate_json(row["payload_json"])
        _validate_final_slots(conn, plan, rows)
        conn.execute("UPDATE content_plans SET approved_revision=revision,approved_at=? WHERE id=?",
                     (now_iso(), plan_id))
        conn.execute("UPDATE pipeline_stages SET status='SUCCEEDED',error=NULL,ended_at=? "
                     "WHERE run_id=? AND stage='PLAN_REVIEW'", (now_iso(), plan["run_id"]))
        conn.execute("UPDATE pipeline_runs SET status='PLAN_APPROVED',error=NULL WHERE id=?", (plan["run_id"],))
