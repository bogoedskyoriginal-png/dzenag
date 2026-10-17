"""Scheduling boundary. No publication selectors are guessed or shipped as verified.

An adapter is enabled only after the live Dzen UI has been inspected and a future-dated
publication has been verified. Until then both scheduling and updating fail closed.
V0 editor preparation remains available independently.
"""
from __future__ import annotations

from dzen_agent.errors import UserActionRequired
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

    def schedule(self, article_id: int, *, run_id=None):
        self._ready()
        existing = self.store.one("SELECT * FROM publications WHERE article_id=?", (article_id,))
        if existing:
            if existing["status"] == "SCHEDULED_VERIFIED":
                return self.verify(article_id)
            raise UserActionRequired("Создание уже начиналось. Проверьте Дзен, чтобы не создать дубликат.")
        article, detail = self.store.get(article_id), self.store.detail(article_id)
        if self.store.settings()["mode"] != "AUTOPILOT" and article.status != "APPROVED":
            raise UserActionRequired("Сначала утвердите материал.")
        self.store.execute("INSERT INTO publications(article_id,scheduled_at,status,updated_at) "
                           "VALUES(?,?,'PREPARING_IN_DZEN',?)",
                           (article_id, detail["scheduled_at"], now_iso()))
        # Persist intent before the remote side effect; never blindly repeat it.
        evidence = self.adapter.create_scheduled(article, detail, self.store.active_image(article_id))
        self._record_verified(article_id, evidence)

    def verify(self, article_id: int):
        self._ready()
        publication = self.store.one("SELECT * FROM publications WHERE article_id=?", (article_id,))
        if not publication or not publication["dzen_url"]:
            raise UserActionRequired("Нет однозначного адреса публикации для проверки.")
        evidence = self.adapter.verify(publication)
        self._record_verified(article_id, evidence)

    def update(self, article_id: int):
        self._ready()
        publication = self.store.one("SELECT * FROM publications WHERE article_id=?", (article_id,))
        if not publication or not publication["dzen_url"]:
            raise UserActionRequired("Нет однозначного адреса публикации. Обновление остановлено.")
        evidence = self.adapter.update_scheduled(publication, self.store.get(article_id),
                                                self.store.active_image(article_id))
        self._record_verified(article_id, evidence)

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
                         "content_hash=?,verified_at=?,evidence_json=?,updated_at=? WHERE article_id=?",
                         (url, evidence.get("id"), expected_hash, now_iso(), dumps(evidence), now_iso(), article_id))
            conn.execute("UPDATE articles SET status='SCHEDULED_VERIFIED',updated_at=? WHERE id=?",
                         (now_iso(), article_id))
