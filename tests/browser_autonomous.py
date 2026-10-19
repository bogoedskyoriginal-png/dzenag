"""One-click browser E2E with a separate mock worker; the tab is closed during execution."""
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from playwright.sync_api import sync_playwright
from browser_scenarios import fingerprints
from test_editorial_contracts_v1 import native_run
from dzen_agent.config import PROJECT_ROOT
from dzen_agent.store import Store, dumps


def main():
    before = fingerprints()
    folder = PROJECT_ROOT / "data/autonomy-audit" / ("browser-" + uuid4().hex[:8])
    folder.mkdir(parents=True)
    database = folder / "ux-test-autonomous.db"
    with pytest.MonkeyPatch.context() as monkeypatch:
        original, _, _ = native_run.__wrapped__(folder, monkeypatch)
        original.set_setting("generation_calendar", {"2030-01-08":3,"2030-01-10":2})
        with sqlite3.connect(original.database_path) as source,sqlite3.connect(database) as target:
            source.backup(target)
    store = Store(database)
    with (folder / "server.log").open("w", encoding="utf-8") as output:
        process = subprocess.Popen([sys.executable,"-m","streamlit","run","tests/browser_mock_app.py",
            "--server.port=8505","--server.address=127.0.0.1","--server.headless=true","--browser.gatherUsageStats=false"],
            env={**os.environ,"DZEN_UI_TEST_DATABASE":str(database),"DZEN_UI_TEST_LAYOUT":"autonomous","DZEN_UI_BACKGROUND_MOCK":"1"},
            stdout=output,stderr=output,cwd=PROJECT_ROOT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                page=browser.new_page(viewport={"width":1440,"height":1100})
                for _ in range(30):
                    try:
                        page.goto("http://localhost:8505",timeout=2000)
                        break
                    except Exception:
                        page.wait_for_timeout(200)
                page.get_by_role("heading",name="Dzen Agent",exact=True).wait_for(timeout=30000)
                assert page.get_by_role("button",name="Запустить агента",exact=True).is_disabled()
                page.get_by_text("Настроить генерацию",exact=True).click()
                assert page.get_by_role("spinbutton",name="08.01.2030",exact=True).input_value()=="3"
                assert page.get_by_role("spinbutton",name="10.01.2030",exact=True).input_value()=="2"
                page.get_by_role("textbox",name="Пожелания редактора",exact=True).fill("Больше производственных историй")
                page.get_by_text("Разрешаю AI-расходы в пределах этого бюджета",exact=True).click()
                page.get_by_role("button",name="Запустить агента",exact=True).click()
                accepted=time.monotonic()+20
                while time.monotonic()<accepted and not store.one("SELECT id FROM pipeline_runs"):
                    page.wait_for_timeout(200)
                assert store.one("SELECT id FROM pipeline_runs"), page.locator("body").inner_text()
                print("Background run accepted",flush=True)
                page.close()  # Preparation must continue in the separate worker.
                deadline=time.monotonic()+90
                while time.monotonic()<deadline:
                    run=store.one("SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 1")
                    if run and run["status"] in ("PREPARED","PARTIAL_READY","NEEDS_ACTION","STOPPED"):
                        break
                    time.sleep(.2)
                assert run and run["status"]=="PREPARED", run
                assert len(store.list_all())==5
                assert store.one("SELECT COUNT(*) AS n FROM plan_approvals")["n"]==1
                assert store.one("SELECT COUNT(*) AS n FROM pipeline_items WHERE stage='QA' AND status='SUCCEEDED'")["n"]==5
                calls=store.costs(run_id=run["id"])["requests"]
                page=browser.new_page(viewport={"width":1440,"height":1100})
                page.goto("http://localhost:8505")
                page.get_by_role("heading",name="Материалы готовы",exact=True).wait_for(timeout=30000)
                assert not page.get_by_test_id("stException").count()
                for name in ("Аналитика","Поиск тем","Планирование","Исследование","Написание","Оформление и проверка"):
                    assert page.get_by_text(name,exact=True).count()==1
                assert not page.get_by_role("button",name="Утвердить контент-план и продолжить",exact=True).count()
                page.screenshot(path=str(folder/"ready-five.png"),full_page=True)
                for width in (1366,768):
                    page.set_viewport_size({"width":width,"height":900})
                    assert not page.evaluate("document.documentElement.scrollWidth>innerWidth+2")
                assert store.costs(run_id=run["id"])["requests"]==calls
                browser.close()
            report={"one_click":"passed","articles":5,"calendar_days":2,"tab_closed_during_execution":True,
                    "automatic_plan_approval":True,"mock_calls":calls,"production_unchanged":fingerprints()==before}
            (folder/"report.json").write_text(dumps(report),encoding="utf-8")
            print(dumps(report))
            print(folder)
        finally:
            if os.name=="nt":
                subprocess.run(["taskkill","/PID",str(process.pid),"/T","/F"],capture_output=True)
            else:
                process.terminate()
            assert fingerprints()==before


if __name__=="__main__":
    main()
