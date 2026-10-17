"""Headed manual login/diagnostics and V0 preparation, with no terminal input required."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from dzen_agent.browser.automation import fill_editor
from dzen_agent.browser.diagnostics import capture, profile_lock_path, validate_dzen_url
from dzen_agent.config import PROJECT_ROOT, get_settings
from dzen_agent.locking import exclusive_lock
from dzen_agent.models import ArticleStatus
from dzen_agent.store import Store


def launch_session(article_id=None) -> int:
    command = [sys.executable, "-m", "dzen_agent.browser.session"]
    if article_id is not None:
        command.extend(["--article-id", str(article_id)])
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    with (log_dir / "browser-session.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=PROJECT_ROOT, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return process.pid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--article-id", type=int)
    args = parser.parse_args()
    config = get_settings()
    store = Store(config.database_path)
    store.initialize()
    with exclusive_lock(profile_lock_path(config.browser_profile_dir)), sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(config.browser_profile_dir), headless=False,
            executable_path=config.dzen_browser_executable, viewport={"width": 1440, "height": 1000})
        page = context.pages[0] if context.pages else context.new_page()
        try:
            url = (store.settings().get("dzen_editor_url") or config.dzen_editor_url) \
                if args.article_id else "https://dzen.ru/"
            page.goto(validate_dzen_url(url), wait_until="domcontentloaded", timeout=60000)
            page.bring_to_front()
            if args.article_id:
                article = store.get(args.article_id)
                fill_editor(page, article.title, article.body)
                store.update_status(article.id, ArticleStatus.PREPARED_IN_DZEN)
                store.log("DZEN_PREPARE", f"Статья №{article.id}: заполнены поля редактора.")
            else:
                store.log("DZEN_LOGIN", "Браузер открыт. Войдите вручную и закройте окно после входа.")
        except Exception as error:
            capture(page, store, "DZEN_PREPARE" if args.article_id else "DZEN_LOGIN", error)
        try:
            context.wait_for_event("close", timeout=3600000)
        except PlaywrightTimeoutError:
            context.close()


if __name__ == "__main__":
    main()
