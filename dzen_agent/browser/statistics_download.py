from __future__ import annotations

from dzen_agent.errors import UserActionRequired

STATUS = "Требуется проверка кнопки экспорта в вашем кабинете. Используйте загрузку CSV."


def download_statistics(page, destination, *, statistics_url=None, verified_selector=None):
    """Only an observed, unambiguous export control can enable this optional path."""
    from dzen_agent.browser.diagnostics import validate_dzen_url

    if not statistics_url or not verified_selector:
        raise UserActionRequired(STATUS)
    page.goto(validate_dzen_url(statistics_url), wait_until="domcontentloaded")
    button = page.locator(verified_selector)
    if button.count() != 1 or not button.is_visible():
        raise UserActionRequired("Кнопка экспорта не определена однозначно. Загрузите CSV вручную.")
    with page.expect_download(timeout=30000) as download_info:
        button.click()
    download = download_info.value
    if download.failure():
        raise UserActionRequired("Дзен не смог выгрузить статистику. Используйте ручной CSV.")
    download.save_as(destination)
    return destination
