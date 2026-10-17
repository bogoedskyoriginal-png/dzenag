from dataclasses import replace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from dzen_agent.config import PROJECT_ROOT, get_settings


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
