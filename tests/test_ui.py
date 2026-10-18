from dataclasses import replace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from dzen_agent.config import PROJECT_ROOT, get_settings
from dzen_agent.store import dumps
from dzen_agent.store import Store
from dzen_agent.statistics import import_csv, preview_csv
from dzen_agent.pipeline.orchestrator import create_run
from dzen_agent.prompt_service import Prompts


def test_all_sections_render_without_key(tmp_path):
    config = replace(get_settings(), database_path=tmp_path / "ui.db", aitunnel_api_key=None)
    with patch("dzen_agent.config.get_settings", return_value=config):
        app = AppTest.from_file(str(PROJECT_ROOT / "dzen_agent" / "ui.py"), default_timeout=20).run()
        assert not app.exception
        for section in ["Статистика", "Контент-план", "Статьи", "Промпты", "Настройки / Модели",
                        "Автопилот", "Расходы", "Главная"]:
            app.sidebar.radio[0].set_value(section).run()
            assert not app.exception, section
            assert not app.error, (section, [e.value for e in app.error])


def test_manual_article_and_prompt_edit(tmp_path):
    config = replace(get_settings(), database_path=tmp_path / "ui.db", aitunnel_api_key=None)
    with patch("dzen_agent.config.get_settings", return_value=config):
        app = AppTest.from_file(str(PROJECT_ROOT / "dzen_agent" / "ui.py"), default_timeout=20).run()
        app.sidebar.radio[0].set_value("Статьи").run()
        next(x for x in app.text_input if x.label == "Заголовок своего текста").set_value("Мой текст")
        next(x for x in app.text_area if x.label == "Свой текст").set_value("Содержимое статьи")
        next(x for x in app.button if x.label == "Сохранить свой черновик").click().run()
        assert not app.exception
        assert not app.error
        assert any(x.value == "Мой текст" for x in app.text_input)
        app.sidebar.radio[0].set_value("Промпты").run()
        next(x for x in app.text_area if x.label == "Текст промпта").set_value("Проверка {{statistics}}")
        next(x for x in app.button if x.label == "Сохранить новую версию").click().run()
        assert not app.exception
        assert not app.error
        assert next(x for x in app.text_area if x.label == "Текст промпта").value == "Проверка {{statistics}}"


def test_editorial_chat_apply_and_approve_plan(editorial_plan):
    store, run_id, plan_id, ids = editorial_plan
    config = replace(get_settings(), database_path=store.database_path, aitunnel_api_key=None)
    reply = {"choices": [{"message": {"content": dumps({"message": "Предлагаю новый ракурс",
             "proposals": [{"type": "update_concept", "concept_id": ids[0],
                            "angle": "Как ремесленники решали технические задачи", "reason": "Уйти от подборки"}]})}}]}
    with patch("dzen_agent.config.get_settings", return_value=config) as config_mock:
        app = AppTest.from_file(str(PROJECT_ROOT / "dzen_agent" / "ui.py"), default_timeout=20).run()
        app.sidebar.radio[0].set_value("Контент-план").run()
        config_mock.return_value = replace(config, aitunnel_api_key="test-key")
        app.run()
        before = store.rows("SELECT * FROM concepts ORDER BY id")
        with patch("dzen_agent.provider.AITunnel.call", return_value=reply) as call:
            app.chat_input[0].set_value("Уточни ракурс первой идеи").run()
            assert call.call_count == 1
        assert not app.exception and not app.error
        assert store.rows("SELECT * FROM concepts ORDER BY id") == before
        assert len(store.rows("SELECT * FROM editorial_chat_messages WHERE plan_id=?", (plan_id,))) == 2
        next(x for x in app.button if x.label == "Применить").click().run()
        assert not app.exception and not app.error
        assert store.one("SELECT needs_research FROM concepts WHERE id=?", (ids[0],))["needs_research"] == 1
        assert any("Изменение применено" in x.value for x in app.caption)
        with patch("dzen_agent.scheduler.launch_pipeline", return_value=123) as launch:
            next(x for x in app.button if x.label == "Утвердить контент-план и продолжить").click().run()
            launch.assert_called_once_with(run_id)
        assert not app.exception and not app.error
        assert store.one("SELECT approved_revision FROM content_plans WHERE id=?", (plan_id,))["approved_revision"] == 2
        app.sidebar.radio[0].set_value("Промпты").run()
        next(x for x in app.radio if x.label == "Назначение").set_value("EDITOR").run()
        assert not app.exception and not app.error
        assert "редакторский copilot" in next(x for x in app.text_area if x.label == "Текст промпта").value


def test_v1_report_period_metadata_and_missing_discovery_prompt(tmp_path, analytics_v1):
    config = replace(get_settings(), database_path=tmp_path / "ui-v1.db", aitunnel_api_key=None)
    store = Store(config.database_path)
    store.initialize()
    import_id = import_csv(store, "stats.csv", preview_csv(b"article_id;title;views\na;A;300\nb;B;200"))
    store.execute("INSERT INTO analytics_results(statistics_id,payload_json,created_at) VALUES(?,?,?)",
                   (import_id, dumps(analytics_v1), "2026-10-18"))
    with patch("dzen_agent.config.get_settings", return_value=config):
        app = AppTest.from_file(str(PROJECT_ROOT / "dzen_agent" / "ui.py"), default_timeout=20).run()
        app.sidebar.radio[0].set_value("Статистика").run()
        assert not app.exception and not app.error
        assert any("Analytics 1.0" in x.value for x in app.caption)
        next(x for x in app.selectbox if x.label == "Окно измерения").set_value("first_7d").run()
        next(x for x in app.button if x.label == "Сохранить сведения о периоде").click().run()
        assert not app.exception and not app.error
        assert 'first_7d' in store.one("SELECT metadata_json FROM statistics_imports")["metadata_json"]
        app.sidebar.radio[0].set_value("Промпты").run()
        next(x for x in app.radio if x.label == "Назначение").set_value("DISCOVERY").run()
        assert not app.exception and not app.error
        assert any("ещё не задан" in x.value for x in app.warning)
        assert not store.one("SELECT id FROM prompt_versions WHERE prompt_type='DISCOVERY'")


def test_analytics_retry_ui_requires_consent_and_launches_only_one_stage(tmp_path):
    config = replace(get_settings(), database_path=tmp_path / "ui-retry.db", aitunnel_api_key=None)
    store = Store(config.database_path)
    store.initialize()
    Prompts(store).seed()
    import_id = import_csv(store, "stats.csv", preview_csv(b"title;views\nA;300"))
    run_id = create_run(store, "2030-01-07")
    key = f"run:{run_id}:analytics"
    store.execute("UPDATE pipeline_runs SET status='NEEDS_ACTION',statistics_id=? WHERE id=?", (import_id, run_id))
    store.execute("UPDATE pipeline_stages SET status='ERROR' WHERE run_id=? AND stage='ANALYZE'", (run_id,))
    response = {"choices": [{"finish_reason": "length", "message": {"content": '{"old":'}}],
                "usage": {"cost_rub": 1.19}}
    store.execute("INSERT INTO ai_calls(call_key,status,result_json,created_at,updated_at) VALUES(?,'SUCCEEDED',?,'2030','2030')",
                  (key, dumps(response)))
    with patch("dzen_agent.config.get_settings", return_value=config), patch("dzen_agent.scheduler.launch_pipeline") as launch:
        app = AppTest.from_file(str(PROJECT_ROOT / "dzen_agent" / "ui.py"), default_timeout=20).run()
        app.sidebar.radio[0].set_value("Статистика").run()
        assert not app.exception and not app.error
        assert any("публикаций обработано: 1" in x.value for x in app.markdown)
        app.sidebar.radio[0].set_value("Автопилот").run()
        assert not app.exception and not app.error
        assert any("обрезан по лимиту токенов" in x.value for x in app.warning)
        next(x for x in app.button if x.label == "Повторить только Analytics (платно)").click().run()
        launch.assert_not_called()
        assert not app.exception
        assert any("Подтвердите новый платный запрос" in x.value for x in app.error)
        assert store.one("SELECT call_key FROM ai_calls")["call_key"] == key
        next(x for x in app.checkbox if x.label == "Я проверил историю AITUNNEL и разрешаю оплату нового запроса").check().run()
        next(x for x in app.button if x.label == "Повторить только Analytics (платно)").click().run()
        launch.assert_called_once_with(run_id, analytics_only=True)
        assert not app.exception and not app.error
        assert ":archived:" in store.one("SELECT call_key FROM ai_calls")["call_key"]
