import sqlite3

from dzen_agent.migrations import EDITORIAL_SCHEMA, SCHEMA, SCHEMA_VERSION
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
    assert store.one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
    assert store.one("PRAGMA foreign_key_check") is None
    store.execute("INSERT INTO editorial_chat_messages(plan_id,role,content,created_at) VALUES(9,'user','Сохранено','now')")
    reopened = Store(path)
    reopened.initialize()
    assert reopened.one("SELECT content FROM editorial_chat_messages")["content"] == "Сохранено"


def test_v2_to_v3_keeps_raw_analytics_prompt_and_run_snapshot(tmp_path):
    path = tmp_path / "v2.db"
    original = '{"summary":"Старый результат","best_topics":[],"weak_topics":[],"useful_patterns":[],"headline_patterns":[],"recommendations":[],"topics_to_avoid":[],"opportunities":[]}'
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.executescript(EDITORIAL_SCHEMA)
        conn.execute("PRAGMA user_version=2")
        conn.execute("INSERT INTO prompt_versions(id,prompt_type,title,content,version,created_at,is_active) "
                     "VALUES(33,'ANALYTICS','Мой Analytics','  Дословный текст\n',3,'2026-10-18',1)")
        conn.execute("INSERT INTO pipeline_runs(id,week_start,mode,status,settings_json,prompts_json,created_at) "
                     "VALUES(1,'2030-01-07','REVIEW','ERROR','{}','{}','2026-10-18')")
        conn.execute("INSERT INTO analytics_results(run_id,payload_json,created_at) VALUES(1,?,'2026-10-18')", (original,))
    store = Store(path)
    before = store.rows("SELECT * FROM prompt_versions")
    store.initialize()
    store.initialize()
    assert store.rows("SELECT * FROM prompt_versions") == before
    assert store.one("SELECT payload_json,superseded FROM analytics_results") == {"payload_json": original, "superseded": 0}
    assert store.one("SELECT settings_json FROM pipeline_runs")["settings_json"] == "{}"
    assert store.one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
    assert store.one("PRAGMA integrity_check")["integrity_check"] == "ok"
    assert len(list((tmp_path / "backups").glob('*.db'))) == 1


def test_v4_resilience_migration_preserves_receipts_and_user_data(tmp_path):
    from dzen_agent.migrations import ANALYTICS_SCHEMA, VISUAL_SCHEMA
    path = tmp_path / "v4.db"
    with sqlite3.connect(path) as conn:
        for schema in (SCHEMA, EDITORIAL_SCHEMA, ANALYTICS_SCHEMA, VISUAL_SCHEMA):
            conn.executescript(schema)
        conn.execute("PRAGMA user_version=4")
        conn.execute("INSERT INTO articles VALUES(42,'topic','title','untouched body','DRAFT','now','now')")
        conn.execute("INSERT INTO ai_calls(call_key,status,result_json,created_at,updated_at) VALUES('paid','SUCCEEDED','{\"raw\":true}','now','now')")
        conn.execute("INSERT INTO usage_events(timestamp,operation,model,cost_rub,success,call_key) VALUES('now','DISCOVERY','sonar',6,1,'paid')")
    store = Store(path)
    tables = ("articles", "ai_calls", "usage_events")
    before = {t: store.rows(f"SELECT * FROM {t}") for t in tables}
    store.initialize()
    store.initialize()
    assert {t: store.rows(f"SELECT * FROM {t}") for t in tables} == before
    assert store.one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
    assert store.one("PRAGMA integrity_check")["integrity_check"] == "ok"
    assert len(list((tmp_path / "backups").glob('*v4-*.db'))) == 1
    assert store.rows("SELECT * FROM pipeline_items") == []
