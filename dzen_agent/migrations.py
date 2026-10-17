"""Additive, transactional SQLite migrations; never rebuild the V0 articles table."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
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
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
