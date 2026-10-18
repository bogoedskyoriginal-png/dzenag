from __future__ import annotations

import json
from datetime import datetime, timedelta

from dzen_agent.errors import UserActionRequired, log_exception, redact
from dzen_agent import editorial_pipeline
from dzen_agent.analytics_contract import briefs, parse_analytics
from dzen_agent.locking import exclusive_lock
from dzen_agent.models import ArticleStatus
from dzen_agent.plan_review import is_approved
from dzen_agent.prompt_service import Prompts
from dzen_agent.schemas import ContentQA
from dzen_agent.services import ContentService, qa_article, similar, week_slots
from dzen_agent.store import MSK, Store, dumps, now_iso

LEGACY_STAGES = ("COLLECT_STATS", "ANALYZE", "RESEARCH", "PLAN", "PLAN_REVIEW", "REFRESH_RESEARCH", "WRITE", "IMAGE", "QA",
                 "REVIEW", "DZEN_SCHEDULE", "VERIFY")
STAGES = ("COLLECT_STATS", "ANALYZE", "DISCOVERY", "PLAN", "PLAN_REVIEW", "RESEARCH", "REFRESH_RESEARCH", "WRITE", "IMAGE", "QA",
          "REVIEW", "DZEN_SCHEDULE", "VERIFY")


def stages_for(settings: dict) -> tuple:
    return STAGES if settings.get("pipeline_flow") == "analytics_v1" else LEGACY_STAGES


def next_week() -> str:
    today = datetime.now(MSK).date()
    return (today + timedelta(days=7 - today.weekday())).isoformat()


def estimate_operations(settings: dict) -> dict:
    count = int(settings["max_articles_per_week"])
    return {"Аналитика": 1, "Discovery": 0 if settings.get("pipeline_flow") == "legacy" else 1,
        "Исследования (максимум)": count * int(
        settings["max_research_calls_per_article"]), "План": 1, "Статьи": count,
        "Изображения": count, "AI-проверки": count if settings["ai_qa_enabled"] else 0}


def create_run(store: Store, week: str | None = None) -> int:
    week = week or next_week()
    day = datetime.fromisoformat(week)
    if day.weekday() != 0:
        raise ValueError("Неделя должна начинаться с понедельника.")
    existing = store.one("SELECT id FROM pipeline_runs WHERE week_start=?", (week,))
    if existing:
        return existing["id"]
    settings = store.settings()
    settings.setdefault("pipeline_flow", "analytics_v1")
    slots = week_slots(week, settings)
    if any(datetime.fromisoformat(slot) <= datetime.now(MSK) for slot in slots):
        raise ValueError("Выберите будущую неделю.")
    occupied = store.rows("SELECT scheduled_at FROM concepts UNION SELECT scheduled_at FROM article_details "
                          "WHERE scheduled_at IS NOT NULL")
    if {datetime.fromisoformat(x["scheduled_at"]) for x in occupied} & {datetime.fromisoformat(x) for x in slots}:
        raise ValueError("В расписании уже есть статьи или концепты на выбранное время. Измените слоты новой недели.")
    Prompts(store).seed()
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT id FROM pipeline_runs WHERE week_start=?", (week,)).fetchone()
        if existing:
            return existing[0]
        run_id = conn.execute(
            "INSERT INTO pipeline_runs(week_start,mode,status,settings_json,prompts_json,created_at) "
            "VALUES(?,?,'PENDING',?,?,?)",
            (week, settings["mode"], dumps(settings), dumps(Prompts(store).snapshot()), now_iso()),
        ).lastrowid
        conn.executemany("INSERT INTO pipeline_stages(run_id,stage,status) VALUES(?,?,'PENDING')",
                         [(run_id, stage) for stage in stages_for(settings)])
    return run_id


class Pipeline:
    def __init__(self, store: Store, provider, *, dzen=None, service_factory=ContentService):
        self.store, self.provider, self.dzen = store, provider, dzen
        self.service_factory = service_factory

    def _check_stop(self):
        if self.store.settings()["stop_requested"]:
            raise UserActionRequired("Остановлено пользователем. Готовые результаты сохранены.")

    def execute(self, run_id: int, *, allow_dzen=False, analytics_only=False) -> str:
        if allow_dzen and analytics_only:
            raise ValueError("Режим только Analytics несовместим с отправкой в Дзен.")
        with exclusive_lock(self.store.database_path.parent / "pipeline.lock"):
            return self._execute(run_id, allow_dzen=allow_dzen, analytics_only=analytics_only)

    def _execute(self, run_id: int, *, allow_dzen=False, analytics_only=False) -> str:
        run = self.store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
        if not run:
            raise ValueError("Запуск не найден.")
        if run["status"] == "SUCCEEDED":
            return "SUCCEEDED"
        settings = json.loads(run["settings_json"])
        service = self.service_factory(self.store, self.provider, settings=settings,
                                       prompts=json.loads(run["prompts_json"]), run_id=run_id)
        self.store.execute("UPDATE pipeline_runs SET status='RUNNING',started_at=COALESCE(started_at,?),"
                           "ended_at=NULL,error=NULL WHERE id=?", (now_iso(), run_id))
        stage = "COLLECT_STATS"
        try:
            if settings.get("pipeline_flow") == "analytics_v1" and not analytics_only:
                editorial_pipeline.preflight(settings, json.loads(run["prompts_json"]))
            for stage in stages_for(settings):
                if analytics_only and stage not in ("COLLECT_STATS", "ANALYZE"):
                    self.store.execute("UPDATE pipeline_runs SET status='ANALYTICS_READY',error=NULL,ended_at=? WHERE id=?",
                                       (now_iso(), run_id))
                    return "ANALYTICS_READY"
                self._check_stop()
                record = self.store.one("SELECT * FROM pipeline_stages WHERE run_id=? AND stage=?",
                                        (run_id, stage))
                if stage == "PLAN_REVIEW":
                    plan = self.store.one("SELECT * FROM content_plans WHERE run_id=?", (run_id,))
                    if not plan:
                        raise UserActionRequired("Контент-план ещё не сохранён. Завершите этап PLAN.")
                    native_research = editorial_pipeline.native(json.loads(run["prompts_json"]).get("RESEARCH"), "research")
                    if (native_research or self.store.settings()["mode"] != "AUTOPILOT") and not is_approved(plan):
                        return self._wait(run_id, stage, "AWAITING_PLAN_REVIEW",
                                          "Контент-план готов. Проверьте идеи перед созданием статей.")
                if record["status"] == "SUCCEEDED":
                    continue
                self.store.execute("UPDATE pipeline_stages SET status='RUNNING',started_at=?,"
                                   "ended_at=NULL,error=NULL,attempts=attempts+1 WHERE id=?",
                                   (now_iso(), record["id"]))
                if stage == "PLAN_REVIEW":
                    pass  # Explicitly approved above, or skipped in AUTOPILOT; no paid operation.
                elif stage == "REVIEW":
                    current_mode = self.store.settings()["mode"]
                    fresh = self.store.one("SELECT approved_at FROM pipeline_runs WHERE id=?", (run_id,))
                    if current_mode != "AUTOPILOT" and not fresh["approved_at"]:
                        return self._wait(run_id, stage, "AWAITING_REVIEW", "Ожидает утверждения недели.")
                    if current_mode == "MANUAL" and not allow_dzen:
                        return self._wait(run_id, stage, "AWAITING_SEND", "Отправка запускается кнопкой.")
                elif stage in ("DZEN_SCHEDULE", "VERIFY"):
                    current_mode = self.store.settings()["mode"]
                    fresh = self.store.one("SELECT approved_at FROM pipeline_runs WHERE id=?", (run_id,))
                    if current_mode != "AUTOPILOT" and not fresh["approved_at"]:
                        return self._wait(run_id, stage, "AWAITING_REVIEW", "Нужно повторное утверждение недели.")
                    if current_mode != "AUTOPILOT" and not allow_dzen:
                        return self._wait(run_id, stage, "AWAITING_SEND", "Нажмите «Отправить в Дзен».")
                    if not self.dzen:
                        raise UserActionRequired(
                            "Отложенная публикация ещё не проверена на вашем канале. "
                            "Откройте диагностику Дзена и выполните вход.")
                    for row in self.articles(run_id):
                        self._check_stop()
                        if stage == "DZEN_SCHEDULE":
                            check = qa_article(self.store, row["article_id"])
                            if not check["passed"]:
                                raise UserActionRequired("QA: " + "; ".join(check["issues"]))
                            self.dzen.schedule(row["article_id"], run_id=run_id)
                        else:
                            self.dzen.verify(row["article_id"])
                else:
                    self._stage(stage, run, service)
                self.store.execute("UPDATE pipeline_stages SET status='SUCCEEDED',ended_at=? "
                                   "WHERE run_id=? AND stage=?", (now_iso(), run_id, stage))
            self.store.execute("UPDATE pipeline_runs SET status='SUCCEEDED',ended_at=? WHERE id=?",
                               (now_iso(), run_id))
            return "SUCCEEDED"
        except Exception as error:
            path = log_exception(error, f"pipeline={run_id}; stage={stage}")
            message = redact(str(error)) if isinstance(error, (UserActionRequired, ValueError)) else (
                "Ошибка выполнения. Подробности сохранены в журнале.")
            self.store.log(stage, message, {"run_id": run_id, "log": str(path)})
            status = "STOPPED" if self.store.settings()["stop_requested"] else "NEEDS_ACTION"
            self.store.execute("UPDATE pipeline_stages SET status='ERROR',ended_at=?,error=? "
                               "WHERE run_id=? AND stage=?", (now_iso(), message, run_id, stage))
            self.store.execute("UPDATE pipeline_runs SET status=?,ended_at=?,error=? WHERE id=?",
                               (status, now_iso(), message, run_id))
            return status

    def _wait(self, run_id, stage, status, message):
        self.store.execute("UPDATE pipeline_stages SET status='WAITING',error=?,started_at=COALESCE(started_at,?),"
                           "ended_at=NULL WHERE run_id=? AND stage=?", (message, now_iso(), run_id, stage))
        self.store.execute("UPDATE pipeline_runs SET status=?,error=NULL WHERE id=?", (status, run_id))
        return status

    def articles(self, run_id):
        return self.store.rows("SELECT * FROM article_details WHERE run_id=? ORDER BY article_id", (run_id,))

    def _stage(self, stage, run, service):
        run_id = run["id"]
        key = f"run:{run_id}"
        settings = service.settings
        if settings.get("analysis_revision", 0):
            key += f":analysis-v{settings['analysis_revision']}"
        if stage == "COLLECT_STATS":
            latest = self.store.one("SELECT * FROM statistics_imports ORDER BY id DESC LIMIT 1")
            if not latest:
                raise UserActionRequired("Загрузите CSV в разделе «Статистика».")
            if datetime.now(MSK) - datetime.fromisoformat(latest["created_at"]) > timedelta(
                    days=settings["statistics_max_age_days"]):
                raise UserActionRequired("Статистика устарела. Загрузите свежий CSV.")
            self.store.execute("UPDATE pipeline_runs SET statistics_id=? WHERE id=?", (latest["id"], run_id))
            return
        fresh = self.store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
        if stage == "ANALYZE":
            service.analyze(fresh["statistics_id"], key + ":analytics")
            return
        analytics_row = self.store.one("SELECT * FROM analytics_results WHERE run_id=? AND superseded=0 ORDER BY id DESC", (run_id,))
        analytics = json.loads(analytics_row["payload_json"])
        parse_analytics(analytics)
        if stage == "DISCOVERY":
            service.discover(analytics, key + ":discovery")
        elif stage == "RESEARCH" and settings.get("pipeline_flow") == "analytics_v1":
            self._research_concepts(run_id, analytics, key, service)
        elif stage == "RESEARCH":
            used = [a.topic for a in self.store.list_all()]
            topics = []
            for topic in briefs(analytics)[0]["promising_directions"]:
                if topic.strip() and not any(similar(topic, t) for t in topics + used):
                    topics.append(topic)
            count = settings["max_articles_per_week"]
            if len(topics) < count:
                raise UserActionRequired("В аналитике недостаточно новых тем для недели. "
                                         "Проверьте статистику и промпт Analytics.")
            for index, topic in enumerate(topics[:count]):
                self._check_stop()
                service.research(topic, analytics, key + f":research:{index}")
        elif stage == "PLAN":
            if settings.get("pipeline_flow") == "analytics_v1":
                discovery = self.store.one("SELECT payload_json FROM discovery_results WHERE run_id=?", (run_id,))
                if not discovery:
                    raise UserActionRequired("Сначала завершите Discovery.")
                service.plan_from_discovery(analytics, json.loads(discovery["payload_json"]), run["week_start"], key + ":plan")
            else:
                rows = self.store.rows("SELECT * FROM research_jobs WHERE run_id=? AND status='sufficient'", (run_id,))
                service.plan(analytics, rows, run["week_start"], key + ":plan")
        elif stage == "REFRESH_RESEARCH":
            self._research_concepts(run_id, analytics, key, service)
        elif stage == "WRITE":
            concepts = self.store.rows("SELECT c.* FROM concepts c JOIN content_plans p ON p.id=c.plan_id "
                                       "WHERE p.run_id=? ORDER BY c.id", (run_id,))
            for concept in concepts:
                self._check_stop()
                service.write(concept["topic"], concept["research_id"], key + f":write:{concept['id']}",
                              concept_row=concept)
        elif stage == "IMAGE":
            for row in self.articles(run_id):
                self._check_stop()
                service.image(row["article_id"], key + f":image:{row['article_id']}")
        elif stage == "QA":
            articles = self.articles(run_id)
            if len(articles) != settings["max_articles_per_week"]:
                raise UserActionRequired("Не все статьи недели созданы.")
            for row in articles:
                check = qa_article(self.store, row["article_id"])
                if not check["passed"]:
                    raise UserActionRequired(f"Статья №{row['article_id']}: " + "; ".join(check["issues"]))
                if settings["ai_qa_enabled"]:
                    article = self.store.get(row["article_id"])
                    research = self.store.one("SELECT payload_json FROM research_jobs WHERE id=?",
                                              (row["research_id"],))
                    result, _ = service.structured("WRITER", ContentQA, service.context(
                        topic=article.topic, article_title=article.title, article_body=article.body,
                        article_summary=row["short_summary"], concept={},
                        research=json.loads(research["payload_json"]),
                        sources=json.loads(research["payload_json"])["sources"],
                        publication_date=row["scheduled_at"],
                    ), key + f":qa:{row['article_id']}:{check['fingerprint']}",
                        article_id=row["article_id"], operation="OTHER")
                    if not result.passed:
                        raise UserActionRequired("Содержательная проверка: " + "; ".join(result.issues))

    def _research_concepts(self, run_id, analytics, key, service):
        concepts = self.store.rows("SELECT c.* FROM concepts c JOIN content_plans p ON p.id=c.plan_id "
                                   "WHERE p.run_id=? AND c.needs_research=1 ORDER BY c.id", (run_id,))
        for concept in concepts:
            self._check_stop()
            payload = json.loads(concept["payload_json"])
            query = concept["topic"] + "\nРедакционный ракурс: " + payload["angle"]
            fresh = concept["research_version"] > 0
            if editorial_pipeline.native(service.prompts.get("RESEARCH"), "research"):
                research_id = editorial_pipeline.research(service, concept,
                    key + f":refresh:{concept['id']}:v{concept['research_version']}")
            else:
                research_id = service.research(
                    query, analytics, key + f":refresh:{concept['id']}:v{concept['research_version']}",
                    cache_topic=f"{concept['topic']} [груминг {concept['id']} v{concept['research_version']}]" if fresh else concept["topic"],
                )
            research = self.store.one("SELECT * FROM research_jobs WHERE id=?", (research_id,))
            if not research or research["status"] != "sufficient":
                raise UserActionRequired("Новое исследование не подтвердило изменённую тему.")
            payload["research_job_id"] = research_id
            payload["research_source_ids"] = [s["id"] for s in json.loads(research["payload_json"])["sources"]]
            with self.store._connect() as conn:
                conn.execute("UPDATE concepts SET research_id=?,payload_json=?,needs_research=0,status='PLANNED' "
                             "WHERE id=? AND research_version=?",
                             (research_id, dumps(payload), concept["id"], concept["research_version"]))


def approve_week(store: Store, run_id: int) -> None:
    rows = store.rows("SELECT * FROM article_details WHERE run_id=?", (run_id,))
    if not rows:
        raise ValueError("В этой неделе нет статей.")
    qa = store.one("SELECT status FROM pipeline_stages WHERE run_id=? AND stage='QA'", (run_id,))
    if not qa or qa["status"] != "SUCCEEDED":
        raise ValueError("Сначала завершите все этапы, включая QA.")
    for row in rows:
        check = qa_article(store, row["article_id"])
        if not check["passed"]:
            raise ValueError(f"Статья №{row['article_id']}: " + "; ".join(check["issues"]))
    for row in rows:
        store.update_status(row["article_id"], ArticleStatus.APPROVED)
    store.execute("UPDATE pipeline_runs SET approved_at=?,status='APPROVED' WHERE id=?",
                  (now_iso(), run_id))
