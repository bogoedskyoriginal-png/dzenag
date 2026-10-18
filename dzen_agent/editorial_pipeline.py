"""Native editorial handoffs, with explicit projections for existing SQLite/UI consumers."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from dzen_agent.analytics_contract import briefs
from dzen_agent.editorial_contracts import DiscoveryV1, PlannerV1, ResearchV1, WriterV1, refs
from dzen_agent.errors import UserActionRequired
from dzen_agent.schemas import Concept, Research, WrittenArticle
from dzen_agent.store import MSK, now_iso


def native(prompt, stage):
    return bool(prompt and re.search(r'"contract_version"\s*:\s*"1\.0"', prompt["content"])
                and re.search(r'"stage"\s*:\s*"' + stage + r'"', prompt["content"]))


def preflight(settings, prompts):
    """Reject known incompatible paid configurations before the first model call."""
    stages = {kind: native(prompts.get(kind.upper()), kind) for kind in ("discovery", "planner", "research", "writer", "image")}
    if any(stages[k] for k in ("discovery", "planner", "research", "writer")) and not all(
            stages[k] for k in ("discovery", "planner", "research", "writer")):
        raise UserActionRequired("Discovery, Planner, Research и Writer должны использовать совместимые контракты 1.0. "
                                 "Авторские промпты не изменены.")
    if stages["image"] and not stages["writer"]:
        raise UserActionRequired("Image 1.0 требует Writer 1.0.")
    if stages["planner"] and settings["max_articles_per_week"] > 5 and settings["max_output_tokens"] < settings["max_articles_per_week"] * 2000:
        raise UserActionRequired("Для подробного плана такого объёма увеличьте лимит выходных токенов "
                                 f"до {settings['max_articles_per_week'] * 2000} или выше в настройках. "
                                 "Запрос ещё не оплачивался.")


def run_context(service):
    run = service.store.one("SELECT * FROM pipeline_runs WHERE id=?", (service.run_id,))
    start = run["week_start"] if run else None
    return {"run_id": service.run_id, "as_of": datetime.now(MSK).date().isoformat(),
            "timezone": "Europe/Moscow", "planning_window": {"start": start,
                "end": (datetime.fromisoformat(start) + timedelta(days=6)).date().isoformat() if start else None,
                "timezone": "Europe/Moscow"}}


def editorial_config(service):
    return {"channel_name": "Перемотка", "content_scope": service.settings["channel_topic"] or None,
            "target_article_count": service.settings["max_articles_per_week"],
            "target_length_chars": service.settings.get("target_length_chars", 10000),
            "minimum_length_chars": service.settings.get("minimum_length_chars", 7000),
            "maximum_length_chars": service.settings.get("maximum_length_chars", 12000),
            "subscription_heading": "## Прежде, чем перейти к чтению - пожалуйста, подпишитесь на канал. Ваша поддержка очень важна для нас.",
            "final_heading": "## Ставьте лайки и подписывайтесь на Перемотку!",
            "editorial_restrictions": [], "additional_requirements": []}


def history(service):
    rows = service.store.rows("SELECT a.id AS article_id,a.title,a.topic,a.status,d.scheduled_at "
        "FROM articles a LEFT JOIN article_details d ON d.article_id=a.id ORDER BY a.id DESC LIMIT 200")
    return ([r for r in rows if r["status"] == "PUBLISHED"],
            [r for r in rows if r["status"] != "PUBLISHED"])


def validate_run(result, service):
    if result.run_id != service.run_id:
        raise ValueError("Ответ изменил run_id.")


def discover(service, analytics, call_key):
    from dzen_agent.services import citation_urls, similar, search_evidence_summary
    from dzen_agent.discovery_validation import reject
    revision = int(service.settings.get("analysis_revision", 0))
    old = service.store.one("SELECT * FROM discovery_results WHERE run_id=?", (service.run_id,))
    if old and old["analysis_revision"] == revision:
        result = DiscoveryV1.model_validate_json(old["payload_json"])
        if result.status != "insufficient_data":
            return old["id"]
        if service.store.one("SELECT call_key FROM ai_calls WHERE call_key=?", (call_key,)):
            raise UserActionRequired("Discovery: недостаточно подтверждённых кандидатов. Результат сохранён.")
    discovery_brief, _ = briefs(analytics)
    published, scheduled = history(service)
    context = {"run_context": run_context(service), "editorial_config": editorial_config(service),
        "analytics": {**analytics, "discovery_brief": discovery_brief}, "published_articles": published,
        "scheduled_articles": scheduled, "editorial_backlog": service.store.rows(
            "SELECT c.id,c.topic,c.scheduled_at,c.status FROM concepts c ORDER BY c.id DESC LIMIT 100"),
        "audience_signals": None, "search_config": {"target_candidates": service.settings["max_articles_per_week"] + 3,
            "max_sources_per_candidate": 3, "search_depth": "balanced", "excluded_domains": []}}
    result, response = service.structured("DISCOVERY", DiscoveryV1, context, call_key)
    validate_run(result, service)
    if result.planning_window.model_dump() != context["run_context"]["planning_window"]:
        raise ValueError("Discovery изменил период планирования.")
    allowed = citation_urls(response)
    observations = {o["id"] for o in analytics.get("observations", [])}
    article_ids = {r["article_id"] for r in published + scheduled}
    used = [r["topic"] for r in published + scheduled]
    issues = list(response.get("discovery_validation", []))
    accepted = []
    for idea in result.candidates:
        try:
            refs(idea.analytics_observation_ids, observations, "analytics_observation_ids")
            refs(idea.possible_duplicate_article_ids, article_ids, "possible_duplicate_article_ids")
        except ValueError as error:
            reject(result, idea.model_dump(), str(error))
            issues.append({"idea_id": idea.idea_id, "type": "unknown_context_reference", "message": str(error)})
            continue
        grounded = all(str(s.url).rstrip("/") in allowed for s in idea.sources)
        unsupported = any(f.claim.strip() and not f.source_ids for f in idea.anchor_facts)
        if not grounded or unsupported:
            message = "Источники не подтверждены поисковыми метаданными или факт не имеет источника."
            reject(result, idea.model_dump(), message, reason_code="insufficient_sources")
            issues.append({"idea_id": idea.idea_id, "type": "ungrounded_candidate", "message": message})
            continue
        if any(similar(idea.topic, t) for t in used):
            idea.viability_status = "needs_validation"
            idea.risks.append("Есть риск повтора темы.")
        if idea.viability_status == "ready_for_planning":
            used.append(idea.topic)
        accepted.append(idea)
    result.candidates = accepted
    ready = [i for i in result.candidates if i.viability_status == "ready_for_planning"]
    if not ready:
        result.status = "insufficient_data"
    elif len(ready) < service.settings["max_articles_per_week"]:
        result.status = "partial"
    elif issues and result.status == "ok":
        result.status = "partial"
    result = DiscoveryV1.model_validate(result.model_dump(mode="json"))
    service.store.log("DISCOVERY_VALIDATION", "Локальная проверка кандидатов Discovery без изменения оплаченного ответа", {
        "run_id": service.run_id, "call_key": call_key, "issues": issues, "accepted_ids": [i.idea_id for i in result.candidates],
        "ready_ids": [i.idea_id for i in ready], "search_evidence": search_evidence_summary(response)})
    service.store.execute("INSERT INTO discovery_results(run_id,payload_json,prompt_version,created_at,analysis_revision) "
        "VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET payload_json=excluded.payload_json,"
        "prompt_version=excluded.prompt_version,created_at=excluded.created_at,analysis_revision=excluded.analysis_revision",
        (service.run_id, result.model_dump_json(), service.prompts["DISCOVERY"]["id"], now_iso(), revision))
    if not ready:
        raise UserActionRequired("Discovery: нет подтверждённых идей. Результат сохранён без платного повтора.")
    return service.store.one("SELECT id FROM discovery_results WHERE run_id=?", (service.run_id,))["id"]


def plan(service, analytics, discovery, week_start, call_key):
    from dzen_agent.services import similar, week_slots
    old = service.store.one("SELECT id FROM content_plans WHERE run_id=?", (service.run_id,))
    if old:
        return old["id"]
    discovered = DiscoveryV1.model_validate(discovery)
    candidates = {c.idea_id: c for c in discovered.candidates if c.viability_status == "ready_for_planning"}
    if not candidates:
        raise UserActionRequired("Нет подтверждённых кандидатов Discovery.")
    slots = week_slots(week_start, service.settings)
    if len(candidates) < len(slots):
        raise UserActionRequired(f"Для плана нужны {len(slots)} разные подтверждённые идеи Discovery; доступно {len(candidates)}. "
                                 "Planner не запускался. Нельзя заполнять недобор непроверенными темами.")
    published, scheduled = history(service)
    _, planner_brief = briefs(analytics)
    context = {"run_context": run_context(service), "planning_config": {
        "target_article_count": len(slots), "target_scope": "total_period", "allowed_slots": slots,
        "max_articles_per_day": service.settings.get("max_articles_per_day", 1), "excluded_dates": [],
        "preferred_days": service.settings["publication_days"], "scheduling_constraints": []},
        "analytics": {**analytics, "planner_brief": planner_brief}, "discovery": discovery,
        "published_articles": published, "scheduled_articles": scheduled,
        "editorial_context": editorial_config(service), "previous_plan": None, "editorial_changes": None}
    result, _ = service.structured("PLANNER", PlannerV1, context, call_key)
    validate_run(result, service)
    if result.period.model_dump() != context["run_context"]["planning_window"] or result.mode != "create":
        raise ValueError("Planner изменил период или режим.")
    if result.target.article_count != len(slots) or result.target.target_scope != "total_period":
        raise ValueError("Planner изменил целевое количество статей.")
    used_ideas, used_slots = set(), set()
    used_topics = [r["topic"] for r in published + scheduled]
    allowed_observations = {o["id"] for o in analytics.get("observations", [])}
    allowed_experiments = {e["id"] for e in analytics.get("experiments", [])}
    for item in result.items:
        candidate = candidates.get(item.source_idea_id)
        if not candidate or item.source_idea_id in used_ideas:
            raise ValueError("Planner выбрал неизвестного, неподтверждённого или повторного кандидата.")
        if item.origin != "new" or item.approval_status != "pending_review" or item.locked:
            raise ValueError("Planner не может утверждать новые концепты.")
        slot = item.scheduled_at.astimezone(MSK).isoformat()
        if slot not in slots or slot in used_slots or item.timezone != "Europe/Moscow":
            raise ValueError("Planner использовал недопустимый временной слот.")
        if any(similar(item.topic, t) for t in used_topics):
            raise ValueError("Planner повторил тему.")
        source_lookup = {s.source_id: s for s in candidate.sources}
        for source in item.initial_sources:
            original = source_lookup.get(source.source_id)
            if not original or source.url != original.url:
                raise ValueError("Planner подменил источник Discovery.")
        refs(item.analytics_observation_ids, allowed_observations, "analytics_observation_ids")
        refs(item.analytics_experiment_ids, allowed_experiments, "analytics_experiment_ids")
        used_topics.append(item.topic)
        used_ideas.add(item.source_idea_id)
        used_slots.add(slot)
    for alternate in result.alternates:
        refs([alternate.source_idea_id], set(candidates) - used_ideas, "alternates")
    if not result.items:
        raise UserActionRequired("Planner не сформировал концепты. Оплаченный ответ сохранён.")
    with service.store._connect() as conn:
        plan_id = conn.execute("INSERT INTO content_plans(run_id,week_start,created_at,payload_json) VALUES(?,?,?,?)",
            (service.run_id, week_start, now_iso(), result.model_dump_json())).lastrowid
        for item in result.items:
            job_topic = item.topic
            job_id = conn.execute("INSERT INTO research_jobs(run_id,topic,status,payload_json,created_at) "
                "VALUES(?,?,'pending','{}',?)", (service.run_id, job_topic, now_iso())).lastrowid
            value = Concept(topic=item.topic, working_title=item.working_headline, angle=item.angle,
                why_now=candidates[item.source_idea_id].why_now or "", audience_value=item.reader_promise,
                thesis=item.key_thesis, key_points=item.research_brief.questions, research_job_id=job_id,
                research_source_ids=[s.source_id for s in item.initial_sources],
                suggested_publication_date=item.scheduled_at.date().isoformat(),
                suggested_publication_time=item.scheduled_at.strftime("%H:%M"), score=0, editorial_contract=item)
            conn.execute("INSERT INTO concepts(plan_id,research_id,topic,payload_json,scheduled_at,status,needs_research) "
                "VALUES(?,?,?,?,?,'RESEARCH_REQUIRED',1)",
                (plan_id, job_id, item.topic, value.model_dump_json(), item.scheduled_at.astimezone(MSK).isoformat()))
    return plan_id


def approved_concept(service, row):
    from dzen_agent.plan_review import is_approved
    plan_row = service.store.one("SELECT * FROM content_plans WHERE id=?", (row["plan_id"],))
    if not plan_row or not is_approved(plan_row):
        raise UserActionRequired("Research и Writer требуют ручного утверждения актуального плана.")
    payload = json.loads(row["payload_json"])
    native_item = payload.get("editorial_contract")
    if not native_item:
        raise UserActionRequired("У старого концепта отсутствует Research Brief нового контракта. "
                                 "Нужен новый план, либо прежние совместимые промпты.")
    # DB revision and explicit approval are authoritative, never the model's approval flags.
    return {**native_item, "revision": row["revision"], "approval_status": "approved",
        "topic": row["topic"], "working_headline": payload["working_title"], "angle": payload["angle"],
        "key_thesis": payload["thesis"], "reader_promise": payload["audience_value"],
        "scheduled_at": row["scheduled_at"]}


def research(service, row, call_key):
    from dzen_agent.services import citation_urls
    concept = approved_concept(service, row)
    context = {"run_context": run_context(service), "concept": concept,
        "editorial_config": editorial_config(service), "research_config": {
            "search_depth": "deep", "max_sources": 12, "preferred_domains": [], "excluded_domains": [],
            "additional_requirements": []}}
    result, response = service.structured("RESEARCH", ResearchV1, context, call_key)
    validate_run(result, service)
    if (result.concept_id, result.concept_revision) != (concept["concept_id"], concept["revision"]):
        raise ValueError("Research изменил идентификатор или версию утверждённого концепта.")
    allowed = citation_urls(response)
    grounded = all(str(s.url).rstrip("/") in allowed for s in result.sources)
    ready = (result.status in ("ok", "partial") and result.research_status == "ready"
        and result.writer_handoff.sufficiency.coverage == "sufficient" and grounded
        and result.concept_validation.thesis_status == "supported")
    value = Research(status="sufficient" if ready else "insufficient_research",
        summary="\n".join(result.research_summary.main_findings),
        key_facts=[f.claim for f in result.facts if f.fact_id in result.writer_handoff.approved_fact_ids],
        claims=[{"text": f.claim, "source_ids": f.source_ids} for f in result.facts
                if f.fact_id in result.writer_handoff.approved_fact_ids],
        sources=[{"id": s.source_id, "title": s.title, "url": s.url, "publication_date": s.publication_date,
                  "excerpt": s.reliability_note} for s in result.sources],
        contradictions=[c.assessment for c in result.contradictions],
        uncertainty=result.research_limitations + ([] if grounded else ["URL не подтверждены поисковым ответом."]),
        suggested_angles=[], editorial_contract=result)
    with service.store._connect() as conn:
        job_id = row["research_id"]
        conn.execute("UPDATE research_jobs SET status=?,payload_json=? WHERE id=?",
            (value.status, value.model_dump_json(), job_id))
        conn.execute("DELETE FROM research_sources WHERE research_id=?", (job_id,))
        for source in value.sources:
            conn.execute("INSERT INTO research_sources(research_id,source_id,url,title,publication_date,excerpt) "
                "VALUES(?,?,?,?,?,?)", (job_id, source.id, str(source.url), source.title, source.publication_date, source.excerpt))
    if not ready:
        raise UserActionRequired("Research требует дополнительной проверки: " + result.status + "/" + result.research_status
                                 + ". Досье и оплаченный ответ сохранены, Writer не запущен.")
    return job_id


def write(service, row, research_payload, call_key, existing_article=None, part="all"):
    concept = approved_concept(service, row)
    research_result = ResearchV1.model_validate(research_payload.get("editorial_contract"))
    if (research_result.concept_id, research_result.concept_revision) != (concept["concept_id"], concept["revision"]):
        raise UserActionRequired("Research относится к другой версии концепта. Нужен новый Research.")
    if research_result.research_status != "ready" or research_result.writer_handoff.sufficiency.coverage != "sufficient":
        raise UserActionRequired("Research не готов к Writer.")
    context = {"run_context": run_context(service), "concept": concept,
        "research": research_result.model_dump(mode="json"), "editorial_config": editorial_config(service),
        "editorial_materials": {"writing_mode": "new_article" if not existing_article else "rewrite",
            "author_notes": [] if part == "all" else ["Измените только " + ("заголовок" if part == "title" else "текст")
                + "; остальные части переданного текста сохраните дословно."],
            "draft_text": existing_article.body if existing_article else None, "outline": None, "approved_opinions": []}}
    result, _ = service.structured("WRITER", WriterV1, context, call_key,
        article_id=existing_article.id if existing_article else None)
    validate_run(result, service)
    result.validate_research(research_result)
    if result.status != "ok" or not result.handoff.ready_for_qa:
        raise UserActionRequired("Writer остановлен: " + result.status + ". Ответ сохранён без платного повтора.")
    if existing_article and part == "title" and result.content.article_markdown != existing_article.body:
        raise UserActionRequired("Writer изменил текст при запросе только заголовка. Статья не изменена, ответ сохранён.")
    if existing_article and part == "body":
        result.content.headline = existing_article.title
    result.content.character_count = len(result.content.article_markdown)
    source_ids = sorted({s for fact in research_result.facts
        if fact.fact_id in {e.fact_id for e in result.evidence_map} for s in fact.source_ids}
        | {s for quote in research_result.quotes if quote.quote_id in {q.quote_id for q in result.quotes_used}
           for s in quote.source_ids})
    if not source_ids:
        raise UserActionRequired("Writer не указал подтверждённые факты в evidence_map.")
    value = WrittenArticle(title=result.content.headline, body=result.content.article_markdown,
        short_summary=concept["key_thesis"] or result.content.article_markdown[:400], source_ids=source_ids)
    return value, result.model_dump(mode="json")
