import json
from pathlib import Path
from types import SimpleNamespace

from dzen_agent.ai import ArticleGenerator


def test_generator_uses_external_prompts(tmp_path: Path) -> None:
    for filename in ("system.md", "style.md", "examples.md", "user_instructions.md"):
        (tmp_path / filename).write_text(filename, encoding="utf-8")

    generator = ArticleGenerator("test-key", "test-model", prompts_dir=tmp_path)
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(output_text=json.dumps({"title": "Заголовок", "body": "Текст"}))

    generator.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    result = generator.generate("Тема")

    assert result.title == "Заголовок"
    assert "system.md" in captured["instructions"]
    assert captured["input"] == "Тема статьи: Тема"

