from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dzen_agent.errors import log_exception
from dzen_agent.store import Store


def capture(page, store: Store, stage: str, error: Exception) -> dict:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    directory = store.database_path.parent / "browser-diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    screenshot = directory / f"{stamp}.png"
    try:
        page.screenshot(path=str(screenshot), full_page=True)
    except Exception:
        screenshot = None
    parts = urlsplit(page.url)
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    details = {"timestamp": stamp, "stage": stage, "url": url,
               "screenshot_path": str(screenshot) if screenshot else None,
               "log_path": str(log_exception(error, stage))}
    # Do not dump HTML, form values, cookies or authentication responses.
    store.log(stage, str(error), details)
    return details


def validate_dzen_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ("dzen.ru", "www.dzen.ru"):
        raise ValueError("Нужен HTTPS-адрес страницы на dzen.ru.")
    if parts.username or parts.password:
        raise ValueError("Адрес не должен содержать логин или пароль.")
    return url


def profile_lock_path(profile_dir: Path) -> Path:
    return profile_dir.parent / "data" / "browser.lock"
