from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from dzen_agent.models import Article, ArticleStatus


class ArticleRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        from dzen_agent.migrations import migrate

        migrate(self.database_path)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_updated_at ON articles(updated_at DESC)"
            )

    def create(self, topic: str, title: str, body: str) -> Article:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO articles (topic, title, body, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (topic.strip(), title.strip(), body.strip(), ArticleStatus.DRAFT.value, now, now),
            )
            article_id = int(cursor.lastrowid or 0)
        return self.get(article_id)

    def get(self, article_id: int) -> Article:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
        if row is None:
            raise KeyError(f"Article {article_id} not found")
        return _to_article(row)

    def list_all(self) -> list[Article]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM articles ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        return [_to_article(row) for row in rows]

    def update_content(self, article_id: int, title: str, body: str) -> Article:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            old = connection.execute(
                "SELECT title, body FROM articles WHERE id=?", (article_id,)
            ).fetchone()
            if old and (old["title"] != title.strip() or old["body"] != body.strip()):
                connection.execute(
                    "INSERT INTO article_revisions(article_id,title,body,created_at) VALUES(?,?,?,?)",
                    (article_id, old["title"], old["body"], now),
                )
                connection.execute(
                    "UPDATE article_details SET qa_json=NULL WHERE article_id=?", (article_id,)
                )
                connection.execute(
                    "UPDATE articles SET status='WRITTEN' WHERE id=? "
                    "AND status IN ('APPROVED','READY_FOR_REVIEW')", (article_id,)
                )
            cursor = connection.execute(
                "UPDATE articles SET title = ?, body = ?, updated_at = ? WHERE id = ?",
                (title.strip(), body.strip(), now, article_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Article {article_id} not found")
        return self.get(article_id)

    def update_status(self, article_id: int, status: ArticleStatus) -> Article:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE articles SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now, article_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Article {article_id} not found")
        return self.get(article_id)


def _to_article(row: sqlite3.Row) -> Article:
    return Article(
        id=row["id"],
        topic=row["topic"],
        title=row["title"],
        body=row["body"],
        status=ArticleStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )

