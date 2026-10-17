import sqlite3

from dzen_agent.migrations import SCHEMA
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


def test_v1_editorial_migration_keeps_existing_plan_and_articles(tmp_path):
    path = tmp_path / "v1.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version=1")
        conn.execute("INSERT INTO pipeline_runs(id,week_start,mode,status,settings_json,prompts_json,created_at) "
                     "VALUES(7,'2030-01-07','REVIEW','AWAITING_REVIEW','{}','{}','2026-01-01')")
        conn.execute("INSERT INTO content_plans VALUES(9,7,'2030-01-07','2026-01-01')")
        conn.execute("INSERT INTO articles VALUES(42,'topic','title','body','DRAFT','2026-01-01','2026-01-01')")
        conn.execute("INSERT INTO article_details(article_id,run_id) VALUES(42,7)")
        conn.execute("INSERT INTO usage_events(timestamp,operation,model,cost_rub,success,call_key) "
                     "VALUES('2026-01-01','WRITE','old-model',1.25,1,'old-call')")
    store = Store(path)
    store.initialize()
    store.initialize()
    assert store.get(42).body == "body"
    assert store.one("SELECT * FROM content_plans WHERE id=9")["approved_revision"] == 1
    assert store.one("SELECT cost_rub FROM usage_events")["cost_rub"] == 1.25
    assert len(store.rows("SELECT * FROM pipeline_stages WHERE stage IN ('PLAN_REVIEW','REFRESH_RESEARCH')")) == 2
    assert all(row["status"] == "SUCCEEDED" for row in store.rows("SELECT status FROM pipeline_stages"))
    assert len(list((tmp_path / "backups").glob('*.db'))) == 1
    assert store.one("PRAGMA user_version")["user_version"] == 2
    assert store.one("PRAGMA foreign_key_check") is None
    store.execute("INSERT INTO editorial_chat_messages(plan_id,role,content,created_at) VALUES(9,'user','Сохранено','now')")
    reopened = Store(path)
    reopened.initialize()
    assert reopened.one("SELECT content FROM editorial_chat_messages")["content"] == "Сохранено"
