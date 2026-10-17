from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI


@dataclass(frozen=True)
class GeneratedArticle:
    title: str
    body: str


class ArticleGenerator:
    def __init__(self, api_key: str, model: str, prompts_dir: Path | None = None) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY не задан. Добавьте его в файл .env.")
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.prompts_dir = prompts_dir or Path(__file__).parent / "prompts"

    def generate(self, topic: str) -> GeneratedArticle:
        topic = topic.strip()
        if not topic:
            raise ValueError("Введите тему статьи.")
        response = self.client.responses.create(
            model=self.model,
            instructions=self._instructions(),
            input=f"Тема статьи: {topic}",
            text={"format": {"type": "json_object"}},
        )
        try:
            payload = json.loads(response.output_text)
            title = str(payload["title"]).strip()
            body = str(payload["body"]).strip()
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise RuntimeError("AI вернул ответ в неожиданном формате.") from error
        if not title or not body:
            raise RuntimeError("AI вернул пустой заголовок или текст.")
        return GeneratedArticle(title=title, body=body)

    def _instructions(self) -> str:
        parts = []
        for name in ("system.md", "style.md", "examples.md", "user_instructions.md"):
            parts.append((self.prompts_dir / name).read_text(encoding="utf-8").strip())
        return "\n\n".join(parts)

