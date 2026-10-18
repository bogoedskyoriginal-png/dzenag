from __future__ import annotations

import re
from pathlib import Path

from dzen_agent.store import Store, dumps, now_iso

PROMPT_TYPES = ("ANALYTICS", "DISCOVERY", "RESEARCH", "PLANNER", "WRITER", "IMAGE", "EDITOR")
VARIABLES = {
    "statistics", "previous_statistics", "analytics", "research", "topic", "concept",
    "article_title", "article_body", "article_summary", "sources", "publication_date",
    "history", "used_topics", "article_count", "schedule", "channel_topic", "today",
    "plan", "week", "editorial_constraints",
    "discovery", "discovery_brief", "planner_brief", "planning_candidates",
    "run_context", "articles", "channel_metrics", "metric_definitions", "editorial_context",
    "previous_analytics", "experiment_history", "analysis_config",
}
VARIABLE = re.compile(r"{{\s*([a-z_][a-z_0-9]*)\s*}}")


def template_variables(content: str) -> set[str]:
    names = set(VARIABLE.findall(content))
    remaining = VARIABLE.sub("", content)
    if "{{" in remaining or "}}" in remaining:
        raise ValueError("Неверный шаблон. Переменная записывается так: {{topic}}.")
    unknown = names - VARIABLES
    if unknown:
        raise ValueError("Неизвестные переменные: " + ", ".join(sorted(unknown)))
    return names


def render(content: str, values: dict) -> str:
    missing = {key for key in template_variables(content)
               if key not in values or values[key] is None}
    if missing:
        raise ValueError("В промпте не заполнены переменные: " + ", ".join(sorted(missing)))

    def replace(match):
        value = values[match.group(1)]
        return value if isinstance(value, str) else dumps(value)

    return VARIABLE.sub(replace, content)


class Prompts:
    def __init__(self, store: Store):
        self.store = store

    def seed(self) -> None:
        directory = Path(__file__).parent / "prompts"
        for kind in PROMPT_TYPES:
            if kind == "DISCOVERY":
                continue  # User approval is required; never silently author an active Discovery prompt.
            if self.active(kind):
                continue
            if kind == "WRITER":
                content = "\n\n".join((directory / name).read_text("utf-8").strip()
                                       for name in ("system.md", "style.md", "examples.md",
                                                    "user_instructions.md"))
                title = "Writer — инструкции из V0"
            else:
                content = (directory / f"{kind.lower()}_starter.md").read_text("utf-8")
                title = f"{kind.title()} — стартовый шаблон"
            self.save(kind, title, content, only_if_empty=True)

    def active(self, kind: str) -> dict | None:
        return self.store.one("SELECT * FROM prompt_versions WHERE prompt_type=? AND is_active=1",
                              (kind,))

    def save(self, kind: str, title: str, content: str, *, only_if_empty=False) -> int:
        if kind not in PROMPT_TYPES or not content.strip() or not title.strip():
            raise ValueError("Укажите тип, название и непустой текст промпта.")
        template_variables(content)
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("SELECT COALESCE(MAX(version),0) FROM prompt_versions "
                                   "WHERE prompt_type=?", (kind,)).fetchone()[0]
            if only_if_empty and version:
                return 0
            conn.execute("UPDATE prompt_versions SET is_active=0 WHERE prompt_type=?", (kind,))
            cursor = conn.execute(
                "INSERT INTO prompt_versions(prompt_type,title,content,version,created_at,is_active) "
                "VALUES(?,?,?,?,?,1)", (kind, title, content, version + 1, now_iso()),
            )
            return cursor.lastrowid

    def restore(self, version_id: int) -> int:
        old = self.store.one("SELECT * FROM prompt_versions WHERE id=?", (version_id,))
        if not old:
            raise ValueError("Версия промпта не найдена.")
        return self.save(old["prompt_type"], old["title"], old["content"])

    def snapshot(self) -> dict:
        return {kind: self.active(kind) for kind in PROMPT_TYPES}
