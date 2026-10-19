"""Autonomous preparation policy on top of the existing worker, receipts and item ledger."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from dzen_agent.errors import UserActionRequired
from dzen_agent.execution import authorization, authorize_run, category
from dzen_agent.schemas import Concept
from dzen_agent.store import MSK, dumps, now_iso


def start_run(store, calendar, wishes, budget, *, confirmed, drafts=False):
    from dzen_agent.pipeline.orchestrator import create_run
    from dzen_agent.locking import exclusive_lock
    if not confirmed:
        raise UserActionRequired("Разрешите расходы на автономную подготовку.")
    if any(type(n) is not int or not 0 <= n <= 21 for n in calendar.values()):
        raise ValueError("Количество статей указывается целым числом от 0 до 21.")
    calendar = {str(d): n for d, n in calendar.items() if n}
    if not calendar:
        raise ValueError("Выберите даты и количество статей.")
    first = datetime.fromisoformat(min(calendar)).date()
    if any(datetime.fromisoformat(d).date().isoformat() != d for d in calendar):
        raise ValueError("Календарю нужны даты без времени.")
    from dzen_agent.services import week_slots
    settings_override = {"max_articles_per_week": sum(calendar.values()),
                         "generation_calendar": calendar, "editorial_wishes": wishes.strip()}
    slots = week_slots(first.isoformat(), {**store.settings(), **settings_override})
    if any(datetime.fromisoformat(slot) <= datetime.now(MSK) for slot in slots):
        raise ValueError("Выберите будущие даты: назначенное время части статей уже прошло.")
    week = (first - timedelta(days=first.weekday())).isoformat()
    with exclusive_lock(store.database_path.parent / "pipeline.lock"):
        existing = store.one("SELECT * FROM pipeline_runs WHERE week_start=?", (week,))
        if existing:
            raise UserActionRequired("На этот период уже есть запуск. Продолжите его с главной страницы.")
        run_id = create_run(store, week, settings_override=settings_override)
        authorize_run(store, run_id, budget_rub=budget, confirmed=True, drafts=drafts)
        return run_id


def resume_interrupted(store):
    """Only previously authorized work, and only after acquiring the released OS lock."""
    from dzen_agent.locking import exclusive_lock
    from dzen_agent import scheduler
    if store.settings()["stop_requested"]:
        return
    try:
        with exclusive_lock(store.database_path.parent / "pipeline.lock"):
            rows = store.rows("SELECT r.id FROM pipeline_runs r JOIN run_authorizations a ON a.run_id=r.id WHERE r.status='RUNNING'")
            for row in rows:
                store.execute("UPDATE pipeline_runs SET status='RECOVERING' WHERE id=? AND status='RUNNING'", (row["id"],))
    except UserActionRequired:
        return
    for row in rows:
        store.log("RESTART", "Восстановление прерванного фонового процесса по сохранённому разрешению", {"run_id": row["id"]})
        scheduler.launch_pipeline(row["id"])


def approve_automatically(store, run_id):
    from dzen_agent.editorial_contracts import PlanItem
    from dzen_agent.services import similar, week_slots
    grant = authorization(store, run_id)
    if not grant or not json.loads(grant["policy_json"])["autonomous_preparation"]:
        raise UserActionRequired("Нет разрешения на автоматическое утверждение.")
    run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    settings = json.loads(run["settings_json"])
    plan = store.one("SELECT * FROM content_plans WHERE run_id=?", (run_id,))
    rows = store.rows("SELECT * FROM concepts WHERE plan_id=?", (plan["id"],))
    slots = week_slots(run["week_start"], settings)
    if not rows or len(rows) != len(slots) or {r["scheduled_at"] for r in rows} != set(slots):
        raise UserActionRequired("План не соответствует разрешённому календарю.")
    topics = []
    for row in rows:
        payload = Concept.model_validate_json(row["payload_json"])
        item = PlanItem.model_validate(payload.editorial_contract)
        if not item.initial_sources or not item.research_brief.questions or any(similar(row["topic"], t) for t in topics):
            raise UserActionRequired("План содержит повтор либо не имеет источников и задания Research.")
        if datetime.fromisoformat(row["scheduled_at"]) <= datetime.now(MSK):
            raise UserActionRequired("Даты плана должны быть в будущем.")
        topics.append(row["topic"])
    conditions = {"calendar": slots, "unique_topics": True, "initial_sources": True, "research_briefs": True,
                  "wishes": settings.get("editorial_wishes", ""), "authorization_at": grant["authorized_at"]}
    original_payload = json.loads(plan["payload_json"] or "{}")
    updated_payload = {**original_payload}
    if isinstance(updated_payload.get("items"), list):
        updated_payload["items"] = [json.loads(row["payload_json"])["editorial_contract"] for row in rows]
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE content_plans SET approved_revision=revision,approved_at=?,payload_json=? WHERE id=?", (now_iso(), dumps(updated_payload), plan["id"]))
        conn.execute("INSERT OR IGNORE INTO plan_approvals(plan_id,revision,actor,mode,conditions_json,created_at) VALUES(?,?,'orchestrator','autonomous',?,?)",
                     (plan["id"], plan["revision"], dumps(conditions), now_iso()))
    store.log("PLAN_APPROVAL", "План программно проверен и разрешён политикой пользователя", {"run_id": run_id, **conditions})
    if original_payload != updated_payload:
        store.log("PLAN_REVISION", "Снимок плана синхронизирован с актуальными концептами; прежний снимок сохранён", {"run_id": run_id, "previous_payload": original_payload})


def replace_from_reserve(store, concept, service):
    """One replacement per concept version, using only an already grounded ready idea."""
    from dzen_agent.editorial_contracts import PlanItem
    discovery = store.one("SELECT payload_json FROM discovery_results WHERE run_id=?", (service.run_id,))
    if not discovery:
        return False
    previous = store.rows("SELECT topic FROM research_jobs WHERE run_id=?", (service.run_id,))
    used = {r["topic"] for r in previous}
    ideas = json.loads(discovery["payload_json"])["candidates"]
    idea = next((i for i in ideas if i["viability_status"] == "ready_for_planning" and i["topic"] not in used and i["research_questions"]), None)
    if not idea:
        return False
    old = json.loads(concept["payload_json"])
    item = old["editorial_contract"]
    item.update(revision=concept["revision"] + 1, source_idea_id=idea["idea_id"], topic=idea["topic"],
                working_headline=idea["working_headline"], angle=idea["angle"], key_thesis=idea["reader_promise"],
                reader_promise=idea["reader_promise"], content_format=idea["content_format"],
                initial_sources=[{k:s[k] for k in ("source_id", "title", "url", "supports")} for s in idea["sources"]])
    item["research_brief"].update(objective=idea["topic"] + ": " + idea["angle"], questions=idea["research_questions"],
                                  initial_source_ids=[s["source_id"] for s in idea["sources"]], claims_to_verify=[], selection_subjects=[], essential_entities=[])
    PlanItem.model_validate(item)
    with store._connect() as conn:
        previous_plan = dict(conn.execute("SELECT * FROM content_plans WHERE id=?", (concept["plan_id"],)).fetchone())
        job = conn.execute("INSERT INTO research_jobs(run_id,topic,status,payload_json,created_at) VALUES(?,?,'pending','{}',?)",
                           (service.run_id, idea["topic"], now_iso())).lastrowid
        old.update(topic=idea["topic"], working_title=idea["working_headline"], angle=idea["angle"], thesis=idea["reader_promise"],
                   audience_value=idea["reader_promise"], research_job_id=job, research_source_ids=[s["source_id"] for s in idea["sources"]])
        conn.execute("UPDATE concepts SET topic=?,payload_json=?,research_id=?,needs_research=1,revision=revision+1,research_version=research_version+1 WHERE id=?",
                     (idea["topic"], dumps(old), job, concept["id"]))
        conn.execute("UPDATE content_plans SET revision=revision+1,approved_revision=NULL,approved_at=NULL WHERE id=?", (concept["plan_id"],))
    approve_automatically(store, service.run_id)
    store.log("REPLACEMENT", "Неподтверждённая тема заменена проверенной резервной идеей", {"run_id": service.run_id, "previous": concept, "previous_plan": previous_plan, "new_idea": idea["idea_id"]})
    return True


def produce_articles(worker, run, service):
    """Each article advances all the way to QA before the next article; no global barrier."""
    store, run_id = worker.store, run["id"]
    worker._item_errors = []
    prefix = f"run:{run_id}" + (f":analysis-v{service.settings['analysis_revision']}" if service.settings.get("analysis_revision") else "")
    concepts = store.rows("SELECT c.* FROM concepts c JOIN content_plans p ON p.id=c.plan_id WHERE p.run_id=? ORDER BY c.scheduled_at,c.id", (run_id,))
    stages = ("RESEARCH", "REFRESH_RESEARCH", "WRITE", "IMAGE", "QA")
    for stage in stages:
        store.execute("UPDATE pipeline_stages SET status='RUNNING',started_at=COALESCE(started_at,?),attempts=attempts+1 WHERE run_id=? AND stage=? AND status<>'SUCCEEDED'", (now_iso(), run_id, stage))
    for concept in concepts:
        worker._check_stop()
        if concept["needs_research"]:
            grant = authorization(store, run_id)
            if concept["research_version"] < grant["max_repairs"] and refine_from_research(store, concept, service):
                concept = store.one("SELECT * FROM concepts WHERE id=?", (concept["id"],))
            worker._item(run_id, concept, "RESEARCH", lambda: worker._research_one(concept, {}, prefix, service))
            concept = store.one("SELECT * FROM concepts WHERE id=?", (concept["id"],))
            if concept["needs_research"]:
                grant = authorization(store, run_id)
                while concept["needs_research"] and concept["research_version"] < grant["max_repairs"]:
                    if not refine_from_research(store, concept, service) and not replace_from_reserve(store, concept, service):
                        # Search only a missing reserve, never regenerate the original pool.
                        attempt = concept["research_version"] + 1
                        search_key = prefix + f":discovery:reserve:{concept['id']}:{attempt}"
                        if store.one("SELECT call_key FROM ai_calls WHERE call_key=?", (search_key,)):
                            break
                        try:
                            from dzen_agent.execution import perform
                            analytics = json.loads(store.one("SELECT payload_json FROM analytics_results WHERE run_id=? AND superseded=0 ORDER BY id DESC", (run_id,))["payload_json"])
                            perform(service, lambda: service.discover(analytics, search_key, additional={"missing_count": 1, "purpose": "replace_unconfirmed_research", "excluded_topics": [concept["topic"]]}))
                        except UserActionRequired as error:
                            store.log("RESERVE", str(error), {"run_id": run_id, "category": category(error)})
                            break
                        if not replace_from_reserve(store, concept, service):
                            break
                    concept = store.one("SELECT * FROM concepts WHERE id=?", (concept["id"],))
                    worker._item(run_id, concept, "RESEARCH", lambda: worker._research_one(concept, {}, prefix, service))
                    concept = store.one("SELECT * FROM concepts WHERE id=?", (concept["id"],))
                if concept["needs_research"]:
                    continue
        else:
            worker._item(run_id, concept, "RESEARCH", lambda: None)
        suffix = f":r{concept['revision']}" if concept["research_version"] else ""
        article_id = worker._item(run_id, concept, "WRITE", lambda: service.write(concept["topic"], concept["research_id"], prefix + f":write:{concept['id']}" + suffix, concept_row=concept))
        if not article_id:
            continue
        worker._item(run_id, concept, "IMAGE", lambda: service.image(article_id, prefix + f":image:{article_id}"), article_id=article_id)
        detail = store.detail(article_id)
        worker._item(run_id, concept, "QA", lambda: worker._qa(detail, service, prefix, service.settings), article_id=article_id)
    ready = []
    for concept in concepts:
        record = store.one("SELECT status FROM pipeline_items WHERE run_id=? AND concept_id=? AND stage='QA' ORDER BY updated_at DESC,id DESC LIMIT 1", (run_id, concept["id"]))
        if record and record["status"] == "SUCCEEDED":
            row = store.one("SELECT article_id FROM article_details WHERE concept_id=?", (concept["id"],))
            ready.append(row["article_id"])
    for stage in stages:
        item_stage = "RESEARCH" if stage == "REFRESH_RESEARCH" else stage
        statuses = [store.one("SELECT status FROM pipeline_items WHERE run_id=? AND concept_id=? AND stage=? ORDER BY updated_at DESC,id DESC LIMIT 1", (run_id, c["id"], item_stage)) for c in concepts]
        done = all(s and s["status"] == "SUCCEEDED" for s in statuses)
        store.execute("UPDATE pipeline_stages SET status=?,ended_at=?,error=? WHERE run_id=? AND stage=?",
                      ("SUCCEEDED" if done else "ERROR", now_iso(), None if done else "Часть материалов требует проверки", run_id, stage))
    requested = service.settings.get("requested_count", service.settings["max_articles_per_week"])
    status = "PREPARED" if len(ready) == requested else ("PARTIAL_READY" if ready else "NEEDS_ACTION")
    message = None if status == "PREPARED" else f"Готово {len(ready)} из {requested}. Остальные результаты и диагностика сохранены."
    store.execute("UPDATE pipeline_runs SET status=?,error=?,ended_at=? WHERE id=?", (status, message, now_iso(), run_id))
    for error in worker._item_errors:
        store.log("DECISION", "Материал не блокирует остальные статьи", {"run_id": run_id, "category": category(error), "error": error})
    deliver_drafts(worker, run_id, ready)
    return status


def refine_from_research(store, concept, service):
    """Narrow an unsupported thesis, never promote the incomplete dossier to ready."""
    from dzen_agent.editorial_contracts import PlanItem, ResearchV1
    job = store.one("SELECT * FROM research_jobs WHERE id=?", (concept["research_id"],))
    research = json.loads(job["payload_json"]).get("editorial_contract") if job else None
    if not research:
        return False
    result = ResearchV1.model_validate(research)
    change = result.concept_validation.proposed_change
    approved = [f for f in result.facts if f.fact_id in result.writer_handoff.approved_fact_ids]
    facts = [f.claim for f in approved]
    if (result.concept_validation.thesis_status == "supported" or not change or len(facts) < 2
            or len(result.sources) < 2 or not result.works):
        return False
    payload = json.loads(concept["payload_json"])
    item = payload["editorial_contract"]
    # proposed_change is an instruction, often repeating the disproved claim in a negation.
    # The new thesis itself contains only existing approved assertions.
    thesis = " ".join(f.claim for f in approved if f.importance == "core") or " ".join(facts[:3])
    title = result.works[0].title.strip('«»"')
    angle = "Подтверждённая история создания фильма «" + title + "»"
    item.update(revision=concept["revision"] + 1, angle=angle, key_thesis=thesis, reader_promise=angle,
                working_headline="Как создавали «" + title + "»",
                editorial_notes="Прежний неподтверждённый тезис исключён из ракурса; новый Research проверяет только актуальную редакцию.")
    item["research_brief"].update(objective=angle, claims_to_verify=facts,
        questions=["Какие источники подтверждают: " + fact for fact in facts],
        sufficiency_criteria=["Подтверждён скорректированный тезис", "Достаточно проверяемой фактуры для заданного объёма"],
        editorial_constraints=[*item["research_brief"]["editorial_constraints"],
            "Не использовать исключённые утверждения прежнего исследования. Его готовность не подтверждена."])
    PlanItem.model_validate(item)
    payload.update(angle=angle, thesis=thesis, audience_value=angle, working_title=item["working_headline"], key_points=item["research_brief"]["questions"])
    with store._connect() as conn:
        new_job = conn.execute("INSERT INTO research_jobs(run_id,topic,status,payload_json,created_at) VALUES(?,?,'pending',?,?)",
            (service.run_id, concept["topic"] + f" · редакция {concept['revision'] + 1}", dumps({"previous_research": research}), now_iso())).lastrowid
        payload["research_job_id"] = new_job
        conn.execute("UPDATE concepts SET payload_json=?,research_id=?,revision=revision+1,research_version=research_version+1,needs_research=1 WHERE id=?",
                     (dumps(payload), new_job, concept["id"]))
        conn.execute("UPDATE content_plans SET revision=revision+1,approved_revision=NULL,approved_at=NULL WHERE id=?", (concept["plan_id"],))
    approve_automatically(store, service.run_id)
    store.log("REFRAME", "Неподтверждённый тезис сужен по досье. Новая редакция требует отдельного Research; прежнее досье не объявлено достаточным.",
              {"run_id": service.run_id, "concept_id": concept["id"], "previous": concept, "previous_research_id": job["id"], "research_proposal": change, "new_thesis": thesis})
    return True


def deliver_drafts(worker, run_id, ready=None):
    """Retry only the browser handoff, including after preparation is already complete."""
    grant = authorization(worker.store, run_id)
    if not grant or not json.loads(grant["policy_json"])["drafts"]:
        return
    if ready is None:
        ready = []
        for row in worker.articles(run_id):
            result = worker.store.one("SELECT status FROM pipeline_items WHERE run_id=? AND concept_id=? AND stage='QA' ORDER BY updated_at DESC,id DESC LIMIT 1", (run_id, row["concept_id"]))
            if result and result["status"] == "SUCCEEDED":
                ready.append(row["article_id"])
    if not worker.dzen:
        worker.store.log("DRAFT", "Подготовка сохранена. Подключение к Студии не подтверждено; черновики не создавались.", {"run_id": run_id})
        return
    if ready:
        for article_id in ready:
            try:
                worker._check_stop()
                worker.dzen.create_draft(article_id, run_id=run_id)
            except Exception as error:
                worker.store.log("DRAFT", str(error), {"run_id": run_id, "article_id": article_id, "category": category(error)})
