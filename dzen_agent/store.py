from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dzen_agent.database import ArticleRepository

MSK = ZoneInfo("Europe/Moscow")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


DEFAULTS = {
    "mode": "REVIEW", "stop_requested": False, "scheduler_enabled": False,
    "scheduler_day": 6, "scheduler_time": "18:00", "timezone": "Europe/Moscow",
    "max_articles_per_week": 5, "max_research_calls_per_article": 2, "max_retries": 1,
    "monthly_soft_budget_rub": 200.0, "stop_when_balance_below_rub": 10.0,
    "publication_days": [0, 1, 2, 3, 4], "publication_time": "12:00",
    "channel_topic": "", "ai_qa_enabled": False, "max_output_tokens": 8000,
    "statistics_max_age_days": 14, "research_use_web_tool": False,
    "analytics_model": "", "research_model": "", "planner_model": "",
    "writer_model": "", "image_model": "", "dzen_statistics_url": "",
    "image_quality": "low", "dzen_adapter_verified": False,
    "dzen_editor_url": "",
}


class Store(ArticleRepository):
    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql: str, args: tuple = ()) -> int:
        with self._connect() as conn:
            cursor = conn.execute(sql, args)
            return int(cursor.lastrowid or 0)

    def settings(self) -> dict:
        result = dict(DEFAULTS)
        for row in self.rows("SELECT key,value FROM app_settings"):
            result[row["key"]] = json.loads(row["value"])
        return result

    def set_setting(self, key: str, value: Any) -> None:
        if any(word in key.lower() for word in ("api_key", "password", "secret", "token")):
            if key != "max_output_tokens":
                raise ValueError("Секреты хранятся только в .env.")
        self.execute(
            "INSERT INTO app_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)),
        )

    def detail(self, article_id: int) -> dict:
        return self.one("SELECT * FROM article_details WHERE article_id=?", (article_id,)) or {}

    def active_image(self, article_id: int) -> dict | None:
        return self.one("SELECT * FROM images WHERE article_id=? AND is_active=1", (article_id,))

    def costs(self, *, days: int | None = None, article_id=None, run_id=None) -> dict:
        clauses, values = [], []
        if days is not None:
            now = datetime.now(MSK)
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if days > 1:
                start = now - timedelta(days=days)
            clauses.append("timestamp>=?")
            values.append(start.astimezone(UTC).isoformat())
        for column, value in (("article_id", article_id), ("pipeline_run_id", run_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self.one(
            "SELECT COALESCE(SUM(cost_rub),0) AS total, COUNT(*) AS requests, "
            "COALESCE(SUM(cost_rub IS NULL),0) AS unknown FROM usage_events" + where,
            tuple(values),
        )

    def log(self, stage: str, message: str, details: dict | None = None) -> None:
        from dzen_agent.errors import redact

        self.execute(
            "INSERT INTO event_log(timestamp,stage,message,details_json) VALUES(?,?,?,?)",
            (now_iso(), stage, redact(message), redact(dumps(details or {}))),
        )
