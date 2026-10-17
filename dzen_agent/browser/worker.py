from __future__ import annotations

import argparse

from dzen_agent.browser.automation import prepare_in_dzen
from dzen_agent.config import get_settings
from dzen_agent.database import ArticleRepository
from dzen_agent.models import ArticleStatus


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare an article in Dzen")
    parser.add_argument("article_id", type=int)
    args = parser.parse_args()

    settings = get_settings()
    repository = ArticleRepository(settings.database_path)
    repository.initialize()
    article = repository.get(args.article_id)
    try:
        result = prepare_in_dzen(
            article.title,
            article.body,
            settings.dzen_editor_url,
            settings.browser_profile_dir,
            headless=settings.dzen_headless,
            executable_path=settings.dzen_browser_executable,
        )
    except Exception as error:
        print(f"Не удалось подготовить материал: {error}")
        return 1
    repository.update_status(article.id, ArticleStatus.PREPARED_IN_DZEN)
    print(result.message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

