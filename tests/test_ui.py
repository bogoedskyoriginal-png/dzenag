from dataclasses import replace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from dzen_agent.config import PROJECT_ROOT, get_settings
from dzen_agent.store import dumps


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
