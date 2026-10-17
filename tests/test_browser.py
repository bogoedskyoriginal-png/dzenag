from unittest.mock import Mock, patch

from dzen_agent.browser.automation import fill_editor


def test_fill_editor_only_fills_title_and_body() -> None:
    page = Mock()
    title = Mock()
    body = Mock()
    with patch("dzen_agent.browser.automation._first_visible", side_effect=[title, body]):
        fill_editor(page, "Заголовок", "Текст")

    title.fill.assert_called_once_with("Заголовок")
    body.fill.assert_called_once_with("Текст")

