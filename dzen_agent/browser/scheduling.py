"""Scheduling boundary. No publication selectors are guessed or shipped as verified.

An adapter is enabled only after the live Dzen UI has been inspected and a future-dated
publication has been verified. Until then both scheduling and updating fail closed.
V0 editor preparation remains available independently.
"""
from __future__ import annotations

import json
from datetime import datetime

from dzen_agent.errors import UserActionRequired, redact
from dzen_agent.services import article_fingerprint
from dzen_agent.store import Store, dumps, now_iso

UNVERIFIED = ("Отложенная публикация и её редактирование пока не проверены в вашем кабинете. "
              "Нужен ручной вход в Дзен и проверка текущего интерфейса.")


class DzenScheduling:
    def __init__(self, store: Store, adapter=None):
        self.store, self.adapter = store, adapter

    def _ready(self):
        if self.store.settings()["stop_requested"]:
            raise UserActionRequired("Агент остановлен пользователем.")
        if self.adapter is None or not self.adapter.verified:
            raise UserActionRequired(UNVERIFIED)

    def _approved(self, article):
        settings = self.store.settings()
        if settings["mode"] == "AUTOPILOT":
            if not settings.get("dzen_autopilot_authorized", False):
                raise UserActionRequired("Автономная отправка не разрешена в настройках Дзена.")
        elif article.status != "APPROVED":
            raise UserActionRequired("Сначала утвердите актуальный материал.")

    def collect_statistics(self):
        from pathlib import Path
        from dzen_agent.analytics_input import validate_metadata
        from dzen_agent.statistics import preview_statistics, import_statistics
        self._ready()
        evidence = self.adapter.collect_statistics()
        path = Path(evidence["path"])
        preview = preview_statistics(path.read_bytes(), path.name)
        if "title" not in preview.mapping.values() or not ({"impressions", "opens", "reads", "views"} & set(preview.mapping.values())):
            raise UserActionRequired("Столбцы выгрузки изменились. Нужна ручная проверка импорта.")
        existing = self.store.one("SELECT id FROM statistics_imports WHERE sha256=?", (preview.sha256,))
        import_id = import_statistics(self.store, path.name, preview)
        if not existing:
            row = self.store.one("SELECT metadata_json FROM statistics_imports WHERE id=?", (import_id,))
            metadata = json.loads(row["metadata_json"])
            metadata["source"].update(downloaded_at=now_iso(), origin="authorized_studio_export", export_filter=evidence["filter_type"])
            if evidence["filter_type"] == "by-event" and evidence["from"] and evidence["to"]:
                metadata.update(measurement_window="calendar_period", period={"start":evidence["from"], "end":evidence["to"], "as_of":None,"timezone":"Europe/Moscow"})
            metadata = validate_metadata(metadata)
            self.store.execute("UPDATE statistics_imports SET metadata_json=? WHERE id=?", (dumps(metadata), import_id))
        self.store.log("STATISTICS_EXPORT", "Оригинальная выгрузка получена из авторизованной Студии; прежние импорты сохранены", {"import_id":import_id, **evidence})
        return import_id

    def _validate_schedule(self, article_id, detail):
        from dzen_agent.browser.editor import publication_time
        moment = publication_time(detail.get("scheduled_at") or "")
        for row in self.store.rows("SELECT scheduled_at FROM publications WHERE article_id<>?", (article_id,)):
            if row["scheduled_at"] and datetime.fromisoformat(row["scheduled_at"]) == moment:
                raise UserActionRequired("Это время уже занято другой публикацией.")

    def schedule(self, article_id: int, *, run_id=None):
        self._ready()
        existing = self.store.one("SELECT * FROM publications WHERE article_id=?", (article_id,))
        article, detail = self.store.get(article_id), self.store.detail(article_id)
        if existing:
            if existing["status"] == "SCHEDULED_VERIFIED":
                return self.verify(article_id)
            self._approved(article)
            self._validate_schedule(article_id, detail)
            if hasattr(self.adapter, "resume_scheduled"):
                return self._perform(article_id, lambda: self.adapter.resume_scheduled(
                    existing, self.store.get(article_id), self.store.detail(article_id), self.store.active_image(article_id)))
            raise UserActionRequired("Создание уже начиналось. Проверьте Дзен, чтобы не создать дубликат.")
        self._approved(article)
        self._validate_schedule(article_id, detail)
        self.store.execute("INSERT INTO publications(article_id,scheduled_at,status,updated_at) "
                           "VALUES(?,?,'PREPARING_IN_DZEN',?)",
                           (article_id, detail["scheduled_at"], now_iso()))
        # Persist intent before the remote side effect; never blindly repeat it.
        return self._perform(article_id, lambda: self.adapter.create_scheduled(
            article, detail, self.store.active_image(article_id)))

    def create_draft(self, article_id, *, run_id):
        from dzen_agent.execution import authorization
        from dzen_agent.services import qa_article
        self._ready()
        grant = authorization(self.store, run_id)
        if not grant or not json.loads(grant["policy_json"])["drafts"]:
            raise UserActionRequired("Создание черновиков отдельно не разрешено.")
        check = qa_article(self.store, article_id)
        if not check["passed"]:
            raise UserActionRequired("Материал не прошёл QA: " + "; ".join(check["issues"]))
        publication = self.store.one("SELECT * FROM publications WHERE article_id=?", (article_id,))
        if publication and publication["status"] == "SCHEDULED_VERIFIED":
            return publication
        if publication and publication["status"] == "CREATING_DRAFT" and not publication["dzen_url"]:
            raise UserActionRequired("Неизвестен адрес созданного черновика. Дубликат заблокирован до сверки Студии.")
        if not publication:
            self.store.execute("INSERT INTO publications(article_id,status,updated_at) VALUES(?,'PREPARING_DRAFT',?)", (article_id, now_iso()))
            publication = self.store.one("SELECT * FROM publications WHERE article_id=?", (article_id,))
        try:
            result = self.adapter.create_draft(self.store.get(article_id), self.store.detail(article_id), self.store.active_image(article_id), publication)
            self.store.execute("UPDATE publications SET status='DRAFT_VERIFIED',dzen_url=?,dzen_id=?,content_hash=?,"
                               "verified_at=?,evidence_json=?,error=NULL,updated_at=? WHERE article_id=?",
                               (result["dzen_url"], result["dzen_id"], article_fingerprint(self.store, article_id), now_iso(), dumps(result), now_iso(), article_id))
            return result
        except Exception as error:
            self.store.execute("UPDATE publications SET error=?,updated_at=? WHERE article_id=?", (redact(str(error)), now_iso(), article_id))
            raise

    def _perform(self, article_id, operation):
        from dzen_agent.errors import redact
        try:
            evidence = operation()
            self._record_verified(article_id, evidence)
            return evidence
        except Exception as error:
            self.store.execute("UPDATE publications SET error=?,updated_at=? WHERE article_id=?",
                               (redact(str(error)), now_iso(), article_id))
            raise

    def verify(self, article_id: int):
        self._ready()
        publication = self.store.one("SELECT * FROM publications WHERE article_id=?", (article_id,))
        if not publication or not publication["dzen_url"]:
            raise UserActionRequired("Нет однозначного адреса публикации для проверки.")
        return self._perform(article_id, lambda: self.adapter.verify(publication))

    def update(self, article_id: int):
        self._ready()
        self._approved(self.store.get(article_id))
        self._validate_schedule(article_id, self.store.detail(article_id))
        publication = self.store.one("SELECT * FROM publications WHERE article_id=?", (article_id,))
        if not publication or not publication["dzen_url"]:
            raise UserActionRequired("Нет однозначного адреса публикации. Обновление остановлено.")
        return self._perform(article_id, lambda: self.adapter.update_scheduled(
            publication, self.store.get(article_id), self.store.active_image(article_id)))

    def _record_verified(self, article_id, evidence):
        from dzen_agent.browser.diagnostics import validate_dzen_url

        detail, article = self.store.detail(article_id), self.store.get(article_id)
        expected_hash = article_fingerprint(self.store, article_id)
        if (not evidence.get("found_in_scheduled") or evidence.get("title") != article.title
                or evidence.get("scheduled_at") != detail["scheduled_at"]
                or evidence.get("content_hash") != expected_hash):
            raise UserActionRequired("Дзен не подтвердил материал, дату и время. Статус не обновлён.")
        url = validate_dzen_url(evidence["url"])
        existing = self.store.one("SELECT dzen_url FROM publications WHERE article_id=?", (article_id,))
        if existing and existing["dzen_url"] and existing["dzen_url"] != url:
            raise UserActionRequired("Адрес публикации изменился. Требуется ручная проверка соответствия.")
        with self.store._connect() as conn:
            conn.execute("UPDATE publications SET dzen_url=?,dzen_id=?,status='SCHEDULED_VERIFIED',"
                         "content_hash=?,verified_at=?,evidence_json=?,updated_at=?,scheduled_at=?,error=NULL WHERE article_id=?",
                         (url, evidence.get("id"), expected_hash, now_iso(), dumps(evidence), now_iso(), detail["scheduled_at"], article_id))
            conn.execute("UPDATE articles SET status='SCHEDULED_VERIFIED',updated_at=? WHERE id=?",
                         (now_iso(), article_id))
            visual = conn.execute("SELECT id,payload_json FROM visual_results WHERE article_id=? ORDER BY id DESC LIMIT 1",
                                  (article_id,)).fetchone()
            if visual:
                report = json.loads(visual["payload_json"])
                if isinstance(report.get("publishing_handoff"), dict):
                    report["publishing_handoff"].update(execution_status="succeeded", verified_in_dzen=True,
                        draft_id=evidence.get("id"), draft_url=url, issues=[])
                    conn.execute("UPDATE visual_results SET payload_json=? WHERE id=?", (dumps(report), visual["id"]))


def checkpoint(store, article_id, stage, *, url=None, dzen_id=None, evidence=None):
    """Durable remote intent/progress, committed before the next browser action."""
    from dzen_agent.browser.diagnostics import validate_dzen_url
    current = store.one("SELECT * FROM publications WHERE article_id=?", (article_id,))
    if not current:
        raise UserActionRequired("Сначала сохраните намерение отправки материала.")
    if url:
        validate_dzen_url(url)
        if current["dzen_url"] and current["dzen_url"] != url:
            raise UserActionRequired("Нельзя заменить адрес уже начатой публикации.")
    previous = json.loads(current["evidence_json"] or "{}")
    previous.update(evidence or {})
    previous["transfer_stage"] = stage
    store.execute("UPDATE publications SET status=?,dzen_url=COALESCE(?,dzen_url),"
                  "dzen_id=COALESCE(?,dzen_id),evidence_json=?,updated_at=? WHERE article_id=?",
                  (stage, url, dzen_id, dumps(previous), now_iso(), article_id))
