"""Run isolated end-to-end editor journeys: python tests/browser_scenarios.py.

The fixture server permanently blocks outbound HTTP and child processes, including
Streamlit fragment callbacks. The real database is fingerprinted before/after.
"""
import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import Workbook
from playwright.sync_api import sync_playwright

from dzen_agent.config import PROJECT_ROOT, get_settings
from dzen_agent.prompt_service import Prompts
from dzen_agent.store import Store, dumps


def fingerprints():
    with sqlite3.connect(get_settings().database_path) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: hashlib.sha256(dumps(conn.execute('SELECT * FROM "' + t + '" ORDER BY rowid').fetchall()).encode()).hexdigest() for t in tables}


def main():
    before = fingerprints()
    directory = PROJECT_ROOT / "data/ux-redesign-audit" / ("browser-run-" + uuid4().hex[:8])
    directory.mkdir(parents=True)
    database = directory / "ux-test-editor.db"
    store = Store(database)
    store.initialize()
    prompts = Prompts(store)
    prompts.seed()
    for role in ("discovery", "planner", "research", "writer", "image"):
        prompts.save(role.upper(), "Mock contract", dumps({"contract_version": "1.0", "stage": role}))
    for role in ("analytics", "discovery", "planner", "research", "writer", "visual"):
        store.set_setting(role + "_model", role + "-test")
    for key, value in {"max_output_tokens": 64000, "publication_days": list(range(7)),
        "publication_times": ["05:00", "06:00", "16:00"], "max_articles_per_day": 3, "minimum_length_chars": 1}.items():
        store.set_setting(key, value)
    csv = directory / "stats.csv"
    csv.write_text("article_id;title;views\na;First;300\nb;Second;200", encoding="utf-8")
    workbook = Workbook()
    workbook.active.title = "Справка"
    workbook.active.append(["Описание выгрузки"])
    sheet = workbook.create_sheet("Статистика")
    for row in (["Выгрузка канала"], ["ID публикации", "Публикация", "Просмотры"], ["a", "First", 300], ["b", "Second", 200]):
        sheet.append(row)
    xlsx = directory / "stats.xlsx"
    workbook.save(xlsx)
    flags = database.with_suffix(".flags.json")
    env = {**os.environ, "DZEN_UI_TEST_DATABASE": str(database)}
    output = (directory / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "tests/browser_mock_app.py",
        "--server.port=8504", "--server.address=127.0.0.1", "--server.headless=true", "--browser.gatherUsageStats=false"],
        cwd=PROJECT_ROOT, env=env, stdout=output, stderr=output,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    passed = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            # Browser navigation retries only the local server during startup.
            for _ in range(30):
                try:
                    page.goto("http://localhost:8504", timeout=2000)
                    break
                except Exception:
                    page.wait_for_timeout(300)
            page.get_by_role("heading", name="Рабочий стол", exact=True).wait_for(timeout=30000)

            def check(name):
                assert not page.locator('[data-testid="stException"]').count(), page.locator("body").inner_text()
                assert not errors, errors
                assert fingerprints() == before, "The production database changed"
                passed.append(name)
                print("PASS", name, flush=True)

            def nav(name):
                page.get_by_test_id("stSidebar").get_by_text(name, exact=True).click()
                page.get_by_role("heading", name=name, exact=True).wait_for()

            def select(label, option):
                page.get_by_test_id("stSelectbox").filter(has=page.get_by_text(label, exact=True)).get_by_role("combobox").click()
                page.get_by_role("option", name=option, exact=True).click()

            def number(label, value):
                widget = page.get_by_role("spinbutton", name=label, exact=True)
                widget.fill(str(value))
                widget.press("Enter")

            def wait_db(predicate):
                for _ in range(100):
                    if predicate():
                        return
                    page.wait_for_timeout(100)
                raise AssertionError("Expected database change did not finish: " + page.locator("body").inner_text()[-1500:])

            def start(week, count):
                nav("Рабочий стол")
                label = "Подготовить материалы" if not store.rows("SELECT id FROM pipeline_runs") else "Подготовить другую неделю"
                page.get_by_role("button", name=label, exact=True).click()
                page.get_by_role("heading", name="Новая неделя", exact=True).wait_for()
                date = page.get_by_test_id("stDateInput").get_by_role("textbox").first
                date.fill(week.replace("-", "/"))
                date.press("Tab")
                number("Публикаций за неделю", count)
                page.get_by_text("Разрешаю AI-расходы на подготовку этой недели", exact=True).click()
                page.get_by_role("button", name="Начать подготовку", exact=True).click()

            check("empty workspace without statistics")
            page.get_by_role("button", name="Подготовить материалы", exact=True).click()
            assert page.get_by_role("button", name="Начать подготовку", exact=True).is_disabled()
            check("missing statistics disables paid launch")
            page.get_by_role("button", name="Отменить создание недели", exact=True).click()
            nav("Аналитика")
            page.locator('input[type="file"]').first.set_input_files(csv)
            page.get_by_role("button", name="Сохранить импорт", exact=True).click()
            page.get_by_text("Импорт №1 сохранён.", exact=True).wait_for()
            check("CSV preview mapping and import")
            page.locator('input[type="file"]').first.set_input_files(xlsx)
            select("Лист Excel", "Статистика")
            page.get_by_role("button", name="Сохранить импорт", exact=True).click()
            page.get_by_text("Импорт №2 сохранён.", exact=True).wait_for()
            check("XLSX sheet selection preview and import")
            start("2030-01-07", 1)
            page.get_by_role("button", name="Проверить контент-план", exact=True).wait_for(timeout=30000)
            check("one article to plan approval without Research")
            assert not store.rows("SELECT id FROM usage_events WHERE operation='RESEARCH'")
            page.get_by_role("button", name="Проверить контент-план", exact=True).click()
            chat = page.get_by_placeholder("Обсудить идеи или предложить изменения…", exact=True)
            chat.fill("Уточни ракурс первой темы")
            chat.press("Enter")
            page.get_by_role("button", name="Применить", exact=True).wait_for()
            before_concept = store.one("SELECT revision FROM concepts ORDER BY id LIMIT 1")["revision"]
            assert before_concept == 1
            page.get_by_role("button", name="Применить", exact=True).click()
            page.get_by_text("Изменение применено.", exact=True).wait_for()
            assert store.one("SELECT revision FROM concepts ORDER BY id LIMIT 1")["revision"] == 2
            check("grooming before-after explicit apply")
            page.get_by_role("button", name="Утвердить контент-план и продолжить", exact=True).click()
            page.get_by_role("button", name="Посмотреть подготовку материалов", exact=True).wait_for(timeout=45000)
            page.get_by_role("button", name="Посмотреть подготовку материалов", exact=True).click()
            page.get_by_role("button", name="Проверить материалы", exact=True).click()
            page.get_by_role("tab", name="Статья", exact=True).wait_for()
            assert page.locator('[data-testid="stImage"]').count() >= 1
            page.screenshot(path=str(directory / "materials.png"), full_page=True)
            check("Research Writer real local photo files and article preview")
            page.get_by_role("tab", name="Проверка", exact=True).click()
            page.get_by_text("Проверка пройдена", exact=True).wait_for()
            page.get_by_role("button", name="Утвердить материалы недели", exact=True).click()
            wait_db(lambda: store.one("SELECT approved_at FROM pipeline_runs WHERE id=1")["approved_at"])
            assert store.one("SELECT approved_at FROM pipeline_runs WHERE id=1")["approved_at"]
            check("QA and final approval without Dzen")
            from dzen_agent.browser.publisher import ADAPTER_VERSION, profile_identity
            store.set_setting("dzen_adapter_verification", {"adapter_version": ADAPTER_VERSION,
                "profile_identity": profile_identity(get_settings()), "scheduled_verified": True, "edit_verified": True,
                "studio_url": "https://dzen.ru/profile/editor/test/publications", "test_editor_url": "https://dzen.ru/profile/editor/test/1/edit"})
            paid_before_send = store.costs()["requests"]
            nav("Рабочий стол")
            page.get_by_role("button", name="Продолжить отправку в Дзен", exact=True).click()
            wait_db(lambda: (store.one("SELECT status FROM publications WHERE article_id=1") or {}).get("status") == "SCHEDULED_VERIFIED")
            assert store.costs()["requests"] == paid_before_send
            page.get_by_text("В расписании Дзена", exact=False).first.wait_for()
            check("approved article sent to mock Studio without AI and with visible status")
            page.get_by_role("button", name="Проверить материалы", exact=True).click()
            page.get_by_role("tab", name="Редактировать", exact=True).click()
            page.get_by_role("textbox", name="Заголовок", exact=True).fill("Редакторская версия материала")
            page.get_by_role("button", name="Сохранить изменения", exact=True).click()
            wait_db(lambda: store.rows("SELECT id FROM article_revisions"))
            assert store.rows("SELECT id FROM article_revisions")
            check("manual editing and revision history")
            start("2030-01-14", 21)
            page.get_by_role("button", name="Проверить контент-план", exact=True).wait_for(timeout=45000)
            page.get_by_role("button", name="Проверить контент-план", exact=True).click()
            assert len(store.rows("SELECT id FROM concepts WHERE plan_id=2")) == 21
            page.get_by_text("Все концепты", exact=True).click()
            page.screenshot(path=str(directory / "plan-21.png"), full_page=True)
            check("21 concepts with three slots per day")
            flags.write_text(dumps({"fail_json": "discovery"}), encoding="utf-8")
            start("2030-01-21", 1)
            page.get_by_role("button", name="Разобраться с ошибкой", exact=True).wait_for()
            calls = len(store.rows("SELECT id FROM usage_events WHERE operation='ANALYTICS'"))
            page.get_by_role("button", name="Разобраться с ошибкой", exact=True).click()
            assert page.get_by_role("button", name="Повторить этап и продолжить (платно)", exact=True).is_disabled()
            flags.write_text("{}", encoding="utf-8")
            page.get_by_text("Я проверил расходы и разрешаю новый платный запрос", exact=True).click()
            page.get_by_role("button", name="Повторить этап и продолжить (платно)", exact=True).click()
            page.get_by_role("button", name="Проверить контент-план", exact=True).wait_for()
            assert len(store.rows("SELECT id FROM usage_events WHERE operation='ANALYTICS'")) == calls
            check("error recovery consent and no repeated Analytics")
            flags.write_text(dumps({"partial_discovery": True}), encoding="utf-8")
            start("2030-02-04", 2)
            page.get_by_text("Найти только недостающие идеи", exact=True).click()
            page.get_by_role("button", name="Разрешить дозаполнение и продолжить", exact=True).wait_for()
            assert page.get_by_role("button", name="Разрешить дозаполнение и продолжить", exact=True).is_disabled()
            assert not store.rows("SELECT * FROM discovery_requests")
            page.screenshot(path=str(directory / "discovery-shortfall.png"), full_page=True)
            check("partial Discovery retained and extra spend requires consent")
            page.get_by_text("Разрешаю дополнительные платные поиски в этих пределах", exact=True).click()
            page.get_by_role("button", name="Разрешить дозаполнение и продолжить", exact=True).click()
            page.get_by_role("button", name="Проверить контент-план", exact=True).wait_for(timeout=30000)
            assert len(store.rows("SELECT * FROM discovery_requests")) == 1
            assert store.one("SELECT status FROM discovery_requests")["status"] == "DONE"
            check("authorized incremental Discovery automatically continues to Planner")
            start("2030-02-11", 2)
            page.get_by_text("Подготовить меньший план без нового поиска", exact=True).click()
            paid_discovery = len(store.rows("SELECT id FROM usage_events WHERE operation='DISCOVERY'"))
            page.get_by_role("button", name="Сократить до 1 и продолжить Planner (платно)", exact=True).click()
            page.get_by_role("button", name="Проверить контент-план", exact=True).wait_for()
            assert len(store.rows("SELECT id FROM usage_events WHERE operation='DISCOVERY'")) == paid_discovery
            assert len(store.rows("SELECT * FROM concepts WHERE plan_id=5")) == 1
            check("smaller plan recomputes slots without repeated Sonar")
            flags.write_text(dumps({"fail_research": "P001"}), encoding="utf-8")
            nav("Рабочий стол")
            # Return to the two-concept week and approve it; one Research fails in the mock.
            options = store.rows("SELECT id FROM pipeline_runs ORDER BY id")
            assert len(options) == 5
            page.get_by_test_id("stSelectbox").filter(has=page.get_by_text("Рабочая неделя", exact=True)).get_by_role("combobox").click()
            page.get_by_role("option").filter(has_text="04.02").click()
            page.get_by_role("button", name="Проверить контент-план", exact=True).click()
            page.get_by_role("button", name="Утвердить контент-план и продолжить", exact=True).click()
            wait_db(lambda: store.one("SELECT status FROM pipeline_runs WHERE id=4")["status"] == "NEEDS_ACTION")
            page.get_by_role("button", name="Посмотреть подготовку материалов", exact=True).click()
            page.get_by_text("Готовность отдельных статей", exact=True).wait_for()
            assert len(store.rows("SELECT * FROM pipeline_items WHERE run_id=4 AND stage='QA' AND status='SUCCEEDED'")) == 1
            page.get_by_role("button", name="Разобраться с ошибкой", exact=True).click()
            page.get_by_role("button", name="Повторить этап и продолжить (платно)", exact=True).wait_for()
            assert page.get_by_role("button", name="Повторить этап и продолжить (платно)", exact=True).is_disabled()
            page.screenshot(path=str(directory / "independent-article-status.png"), full_page=True)
            check("failed article isolated while another reaches QA; retry consent remains required")
            flags.write_text(dumps({"simulate_running": True}), encoding="utf-8")
            start("2030-01-28", 1)
            page.get_by_text("Агент работает.", exact=False).wait_for()
            page.get_by_role("button", name="Остановить агента", exact=True).click()
            page.get_by_text("Остановка запрошена.", exact=False).first.wait_for()
            wait_db(lambda: store.settings()["stop_requested"])
            check("running state and emergency stop")
            nav("Рабочий стол")
            page.get_by_role("tab", name="Расходы", exact=True).click()
            check("expense history")
            nav("Настройки")
            page.get_by_role("tab", name="Промпты", exact=True).click()
            page.get_by_text("Discovery", exact=True).first.click()
            check("settings and prompt editor")
            nav("Система")
            page.get_by_role("tab", name="Автоматизация Windows", exact=True).click()
            check("Windows automation separate from publication slots")
            for width in (1366, 1920, 768):
                page.set_viewport_size({"width": width, "height": 900})
                if not page.get_by_role("heading", name="Рабочий стол", exact=True).is_visible():
                    nav("Рабочий стол")
                overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 2")
                assert not overflow, f"Overflow at {width}"
                check(f"layout {width}px")
            browser.close()
    except BaseException:
        try:
            if "page" in locals() and not page.is_closed():
                page.screenshot(path=str(directory / "failure.png"), full_page=True)
                (directory / "failure.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
        except Exception:
            pass  # Preserve the original failure if Playwright has already closed its event loop.
        raise
    finally:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        else:
            process.terminate()
        output.close()
        assert fingerprints() == before, "Production DB must remain unchanged by browser scenarios"
        (directory / "report.json").write_text(dumps({"passed": passed, "production_db_unchanged": True}), encoding="utf-8")
        print(directory, flush=True)


if __name__ == "__main__":
    main()
