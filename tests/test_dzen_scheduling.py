from unittest.mock import Mock

import pytest

from dzen_agent.browser.diagnostics import validate_dzen_url
from dzen_agent.browser.scheduling import DzenScheduling
from dzen_agent.browser.statistics_download import download_statistics
from dzen_agent.errors import UserActionRequired
from dzen_agent.store import Store


def test_unverified_adapter_never_touches_dzen(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.initialize()
    adapter = Mock(verified=False)
    dzen = DzenScheduling(store, adapter)
    for method in (dzen.schedule, dzen.verify, dzen.update):
        with pytest.raises(UserActionRequired):
            method(1)
    adapter.create_scheduled.assert_not_called()
    adapter.update_scheduled.assert_not_called()
    assert not store.rows("SELECT * FROM publications")


def test_download_without_observed_selector_is_safe(tmp_path):
    page = Mock()
    with pytest.raises(UserActionRequired):
        download_statistics(page, tmp_path / "export.csv")
    page.goto.assert_not_called()


def test_dzen_url_rejects_other_domains():
    assert validate_dzen_url("https://dzen.ru/profile/editor")
    for url in ("https://dzen.ru.evil.com", "file:///etc/passwd", "http://dzen.ru",
                "https://name:password@dzen.ru"):
        with pytest.raises(ValueError):
            validate_dzen_url(url)


def test_draft_editor_url_is_read_from_observed_row_without_menu_mutation(tmp_path):
    from dzen_agent.browser.publisher import DzenPublisher
    store = Store(tmp_path / "db.sqlite")
    store.initialize()
    page, row, link = Mock(), Mock(), Mock()
    page.url = "https://dzen.ru/profile/editor/channel/publications"
    row.locator.return_value = link
    link.count.return_value = 1
    link.get_attribute.return_value = "/profile/editor/id/channel/one/edit"
    assert DzenPublisher(store).row_editor(page, row) == "https://dzen.ru/profile/editor/id/channel/one/edit"
    row.get_by_role.assert_not_called()
    link.get_attribute.return_value = "https://dzen.ru.evil.com/profile/editor/id/channel/one/edit"
    with pytest.raises(ValueError):
        DzenPublisher(store).row_editor(page, row)
