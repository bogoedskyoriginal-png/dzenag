from pathlib import Path

from dzen_agent.database import ArticleRepository
from dzen_agent.models import ArticleStatus


def test_article_lifecycle(tmp_path: Path) -> None:
    repository = ArticleRepository(tmp_path / "articles.db")
    repository.initialize()

    created = repository.create("Тема", "Заголовок", "Текст")
    assert created.status is ArticleStatus.DRAFT
    assert repository.list_all() == [created]

    updated = repository.update_content(created.id, "Новый заголовок", "Новый текст")
    assert updated.title == "Новый заголовок"

    prepared = repository.update_status(created.id, ArticleStatus.PREPARED_IN_DZEN)
    assert prepared.status is ArticleStatus.PREPARED_IN_DZEN

