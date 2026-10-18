"""Synthetic paid Research fixture; app blocks outbound HTTP and worker launches."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright
from dzen_agent.config import PROJECT_ROOT
from dzen_agent.store import Store, dumps
from browser_scenarios import fingerprints
from test_editorial_contracts_v1 import native_run
from test_research_replay import paid_research
from dzen_agent.pipeline.items import item_fingerprint
from dzen_agent.store import now_iso


def main():
    before = fingerprints()
    folder = PROJECT_ROOT / "data/research-replay-audit" / ("browser-" + uuid4().hex[:8])
    folder.mkdir(parents=True)
    database = folder / "ux-test-research.db"
    with pytest.MonkeyPatch.context() as monkeypatch:
        fixture = native_run.__wrapped__(folder, monkeypatch)
        original, run_id, concept_id, _, _ = paid_research.__wrapped__(fixture)
        original.execute("UPDATE pipeline_stages SET status='ERROR',error='Unknown source_ids' WHERE run_id=? AND stage='RESEARCH'", (run_id,))
        concept = original.one("SELECT * FROM concepts WHERE id=?", (concept_id,))
        original.execute("INSERT INTO pipeline_items(run_id,concept_id,stage,input_hash,status,attempts,error,updated_at) VALUES(?,?,'RESEARCH',?,'ERROR',1,'Unknown source_ids',?)",
                         (run_id, concept_id, item_fingerprint(original, concept, "RESEARCH"), now_iso()))
        with sqlite3.connect(original.database_path) as src, sqlite3.connect(database) as dst:
            src.backup(dst)
    store = Store(database)
    protected = ("ai_calls", "usage_events", "prompt_versions", "analytics_results", "articles", "article_details")
    snapshot = {t: store.rows(f"SELECT * FROM {t} ORDER BY rowid") for t in protected}
    with (folder / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "tests/browser_mock_app.py",
            "--server.port=8504", "--server.address=127.0.0.1", "--server.headless=true", "--browser.gatherUsageStats=false"],
            cwd=PROJECT_ROOT, env={**os.environ, "DZEN_UI_TEST_DATABASE": str(database)}, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                for _ in range(30):
                    try:
                        page.goto("http://localhost:8504", timeout=2000)
                        break
                    except Exception:
                        page.wait_for_timeout(200)
                page.get_by_role("button", name="Разобраться с ошибкой", exact=True).wait_for(timeout=30000)
                page.get_by_role("button", name="Разобраться с ошибкой", exact=True).click()
                page.get_by_text("Диагностика ссылок сохранённого Research", exact=True).click()
                page.get_by_role("button", name="Проверить сохранённый Research бесплатно", exact=True).wait_for()
                page.screenshot(path=str(folder / "before-replay.png"), full_page=True)
                page.get_by_role("button", name="Проверить сохранённый Research бесплатно", exact=True).click()
                page.get_by_role("button", name="Продолжить с Writer (платно)", exact=True).wait_for(timeout=30000)
                assert not page.get_by_test_id("stException").count()
                assert store.one("SELECT status FROM pipeline_runs WHERE id=?", (run_id,))["status"] == "RESEARCH_READY"
                assert {t: store.rows(f"SELECT * FROM {t} ORDER BY rowid") for t in protected} == snapshot
                page.get_by_text("Результат локальной проверки Research", exact=True).click()
                page.screenshot(path=str(folder / "after-replay.png"), full_page=True)
                browser.close()
            assert fingerprints() == before
            (folder / "report.json").write_text(dumps({"free_research_replay": "passed", "paid_stages_started": False,
                "production_unchanged": True, "copy_receipts_and_costs_unchanged": True}), encoding="utf-8")
            print(folder)
        finally:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            else:
                process.terminate()
            assert fingerprints() == before


if __name__ == "__main__":
    main()
