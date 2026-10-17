import sqlite3

from dzen_agent.store import Store


def test_v0_migration_preserves_articles_and_backup(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE articles(id INTEGER PRIMARY KEY,topic TEXT,title TEXT,"
                     "body TEXT,status TEXT,created_at TEXT,updated_at TEXT)")
        conn.execute("INSERT INTO articles VALUES(42,'topic','title','body','PREPARED_IN_DZEN',"
                     "'2025-01-01','2025-01-01')")
    store = Store(path)
    store.initialize()
    store.initialize()
    assert store.get(42).body == "body"
    assert len(list((tmp_path / "backups").glob('*.db'))) == 1
    store.update_content(42, "new", "text")
    assert store.one("SELECT body FROM article_revisions")["body"] == "body"
    assert store.one("PRAGMA integrity_check")["integrity_check"] == "ok"
