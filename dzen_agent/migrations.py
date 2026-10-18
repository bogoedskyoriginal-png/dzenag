"""Additive, transactional SQLite migrations; never rebuild the V0 articles table."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 5
SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
 id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, title TEXT NOT NULL,
 body TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE prompt_versions (
 id INTEGER PRIMARY KEY, prompt_type TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
 version INTEGER NOT NULL, created_at TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1,
 UNIQUE(prompt_type,version)
);
CREATE UNIQUE INDEX one_active_prompt ON prompt_versions(prompt_type) WHERE is_active=1;
CREATE TABLE statistics_imports (
 id INTEGER PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE,
 encoding TEXT NOT NULL, delimiter TEXT NOT NULL, columns_json TEXT NOT NULL,
 rows_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE pipeline_runs (
 id INTEGER PRIMARY KEY, week_start TEXT NOT NULL UNIQUE, mode TEXT NOT NULL,
 status TEXT NOT NULL, settings_json TEXT NOT NULL, prompts_json TEXT NOT NULL,
 statistics_id INTEGER REFERENCES statistics_imports(id), created_at TEXT NOT NULL,
 started_at TEXT, ended_at TEXT, error TEXT, approved_at TEXT
);
CREATE TABLE pipeline_stages (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES pipeline_runs(id),
 stage TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT, ended_at TEXT, error TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, UNIQUE(run_id,stage)
);
CREATE TABLE analytics_results (
 id INTEGER PRIMARY KEY, statistics_id INTEGER REFERENCES statistics_imports(id),
 run_id INTEGER REFERENCES pipeline_runs(id), payload_json TEXT NOT NULL,
 prompt_version INTEGER REFERENCES prompt_versions(id), created_at TEXT NOT NULL
);
CREATE TABLE research_jobs (
 id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES pipeline_runs(id), topic TEXT NOT NULL,
 status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(run_id,topic)
);
CREATE TABLE research_sources (
 id INTEGER PRIMARY KEY, research_id INTEGER NOT NULL REFERENCES research_jobs(id),
 source_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
 publication_date TEXT, excerpt TEXT, UNIQUE(research_id,source_id)
);
CREATE TABLE content_plans (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL UNIQUE REFERENCES pipeline_runs(id),
 week_start TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE concepts (
 id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES content_plans(id),
 research_id INTEGER NOT NULL REFERENCES research_jobs(id), topic TEXT NOT NULL,
 payload_json TEXT NOT NULL, scheduled_at TEXT NOT NULL, status TEXT NOT NULL,
 UNIQUE(plan_id,topic)
);
CREATE TABLE article_details (
 article_id INTEGER PRIMARY KEY REFERENCES articles(id),
 concept_id INTEGER UNIQUE REFERENCES concepts(id), research_id INTEGER REFERENCES research_jobs(id),
 run_id INTEGER REFERENCES pipeline_runs(id), short_summary TEXT NOT NULL DEFAULT '',
 source_ids_json TEXT NOT NULL DEFAULT '[]', metadata_json TEXT NOT NULL DEFAULT '{}',
 scheduled_at TEXT, qa_json TEXT
);
CREATE TABLE article_revisions (
 id INTEGER PRIMARY KEY, article_id INTEGER NOT NULL REFERENCES articles(id),
 title TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE images (
 id INTEGER PRIMARY KEY, article_id INTEGER NOT NULL REFERENCES articles(id), path TEXT NOT NULL,
 model TEXT, prompt_version INTEGER REFERENCES prompt_versions(id), cost_rub REAL,
 origin TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX one_active_image ON images(article_id) WHERE is_active=1;
CREATE TABLE publications (
 id INTEGER PRIMARY KEY, article_id INTEGER NOT NULL UNIQUE REFERENCES articles(id),
 dzen_url TEXT UNIQUE, dzen_id TEXT, scheduled_at TEXT, status TEXT NOT NULL,
 content_hash TEXT, verified_at TEXT, evidence_json TEXT, error TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE ai_calls (
 call_key TEXT PRIMARY KEY, status TEXT NOT NULL, result_json TEXT, error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE usage_events (
 id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, pipeline_run_id INTEGER,
 article_id INTEGER, operation TEXT NOT NULL, model TEXT NOT NULL,
 prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
 cost_rub REAL, balance_after REAL, success INTEGER NOT NULL, error TEXT,
 request_id TEXT, call_key TEXT NOT NULL
);
CREATE INDEX usage_timestamp ON usage_events(timestamp);
CREATE INDEX usage_article ON usage_events(article_id);
CREATE INDEX usage_run ON usage_events(pipeline_run_id);
CREATE TABLE event_log (
 id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, stage TEXT NOT NULL,
 message TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}'
);
"""

EDITORIAL_SCHEMA = """
ALTER TABLE content_plans ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE content_plans ADD COLUMN approved_revision INTEGER;
ALTER TABLE content_plans ADD COLUMN approved_at TEXT;
ALTER TABLE concepts ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE concepts ADD COLUMN needs_research INTEGER NOT NULL DEFAULT 0;
ALTER TABLE concepts ADD COLUMN research_version INTEGER NOT NULL DEFAULT 0;
CREATE TABLE editorial_chat_messages (
 id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES content_plans(id),
 role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
 suggestions_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
 reply_to INTEGER UNIQUE REFERENCES editorial_chat_messages(id),
 status TEXT NOT NULL DEFAULT 'COMPLETE', error TEXT, request_json TEXT
);
CREATE INDEX editorial_chat_plan ON editorial_chat_messages(plan_id,id);
INSERT INTO pipeline_stages(run_id,stage,status)
 SELECT id,'PLAN_REVIEW','PENDING' FROM pipeline_runs;
INSERT INTO pipeline_stages(run_id,stage,status)
 SELECT id,'REFRESH_RESEARCH','PENDING' FROM pipeline_runs;
UPDATE content_plans SET approved_revision=revision,approved_at=created_at
 WHERE EXISTS (SELECT 1 FROM article_details a WHERE a.run_id=content_plans.run_id)
 OR EXISTS (SELECT 1 FROM pipeline_stages s WHERE s.run_id=content_plans.run_id
            AND s.stage='WRITE' AND s.status='SUCCEEDED');
UPDATE pipeline_stages SET status='SUCCEEDED'
 WHERE stage IN ('PLAN_REVIEW','REFRESH_RESEARCH')
 AND run_id IN (SELECT run_id FROM content_plans WHERE approved_revision IS NOT NULL);
"""

ANALYTICS_SCHEMA = """
ALTER TABLE statistics_imports ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE analytics_results ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0;
CREATE TABLE discovery_results (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL UNIQUE REFERENCES pipeline_runs(id),
 payload_json TEXT NOT NULL, prompt_version INTEGER REFERENCES prompt_versions(id),
 created_at TEXT NOT NULL, analysis_revision INTEGER NOT NULL DEFAULT 0
);
"""


VISUAL_SCHEMA = """
ALTER TABLE content_plans ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}';
CREATE TABLE visual_results (
 id INTEGER PRIMARY KEY, article_id INTEGER NOT NULL REFERENCES articles(id),
 call_key TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


RESILIENCE_SCHEMA = """
CREATE TABLE discovery_batches (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES pipeline_runs(id),
 analysis_revision INTEGER NOT NULL, call_key TEXT NOT NULL UNIQUE,
 payload_json TEXT NOT NULL, context_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE discovery_topups (
 run_id INTEGER NOT NULL REFERENCES pipeline_runs(id), analysis_revision INTEGER NOT NULL,
 budget_rub REAL NOT NULL, max_calls INTEGER NOT NULL, reserve_rub REAL NOT NULL,
 authorized_at TEXT NOT NULL, PRIMARY KEY(run_id,analysis_revision)
);
CREATE TABLE discovery_requests (
 call_key TEXT PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES pipeline_runs(id),
 analysis_revision INTEGER NOT NULL, status TEXT NOT NULL, reserved_rub REAL NOT NULL,
 cost_rub REAL, context_json TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE pipeline_items (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES pipeline_runs(id),
 concept_id INTEGER NOT NULL REFERENCES concepts(id), stage TEXT NOT NULL, input_hash TEXT NOT NULL,
 status TEXT NOT NULL, article_id INTEGER REFERENCES articles(id), attempts INTEGER NOT NULL DEFAULT 0,
 error TEXT, updated_at TEXT NOT NULL, UNIQUE(run_id,concept_id,stage,input_hash)
);
"""


def migrate(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=30) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("База создана более новой версией приложения. Обновите приложение.")
        if version == SCHEMA_VERSION:
            return
        # SQLite backup API includes committed WAL data. Backup remains local.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='articles'").fetchone():
            folder = path.parent / "backups"
            folder.mkdir(exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            with sqlite3.connect(folder / f"{path.stem}-v{version}-{stamp}.db") as backup:
                conn.backup(backup)
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have migrated while we acquired the write lock.
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            for target, script in ((1, SCHEMA), (2, EDITORIAL_SCHEMA), (3, ANALYTICS_SCHEMA), (4, VISUAL_SCHEMA), (5, RESILIENCE_SCHEMA)):
                if current < target:
                    for statement in script.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute(f"PRAGMA user_version={target}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
