from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from dzen_agent.browser.selectors import BODY_SELECTORS, TITLE_SELECTORS


class DzenLoginRequired(RuntimeError):
    pass


class DzenEditorChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class PrepareResult:
    prepared: bool
    message: str


def _first_visible(page: Page, selectors: tuple[str, ...]):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.is_visible(timeout=1_500):
                return locator
        except PlaywrightTimeoutError:
            continue
    return None


def fill_editor(page: Page, title: str, body: str) -> None:
    """Fill only editor fields; this function has no publication action."""
    title_field = _first_visible(page, TITLE_SELECTORS)
    body_field = _first_visible(page, BODY_SELECTORS)
    if title_field is None or body_field is None:
        raise DzenEditorChanged(
            "Не удалось найти поля редактора. Возможно, требуется вход или интерфейс Дзена изменился."
        )
    title_field.fill(title)
    body_field.fill(body)


def prepare_in_dzen(
    title: str,
    body: str,
    editor_url: str,
    profile_dir: Path,
    *,
    headless: bool = False,
    executable_path: str | None = None,
) -> PrepareResult:
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            executable_path=executable_path,
            viewport={"width": 1440, "height": 1000},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(editor_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            fill_editor(page, title, body)
        except DzenEditorChanged as error:
            if headless:
                raise DzenLoginRequired(str(error)) from error
            input(
                "Если Дзен просит авторизацию, войдите вручную в открытом браузере. "
                "Затем нажмите Enter здесь, чтобы повторить заполнение..."
            )
            page.goto(editor_url, wait_until="domcontentloaded", timeout=60_000)
            try:
                fill_editor(page, title, body)
            except DzenEditorChanged as retry_error:
                raise DzenLoginRequired(
                    "Поля редактора всё ещё недоступны. Проверьте вход в Дзен и адрес редактора."
                ) from retry_error

        if headless:
            context.close()
            return PrepareResult(True, "Поля редактора заполнены в тестовом режиме.")

        page.bring_to_front()
        input(
            "Черновик заполнен. Проверьте его в браузере. "
            "Публикация не выполняется. Нажмите Enter здесь, чтобы завершить процесс..."
        )
        context.close()
        return PrepareResult(True, "Материал подготовлен в Дзене без публикации.")

