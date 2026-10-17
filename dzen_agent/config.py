from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(PROJECT_ROOT / ".playwright"))


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    browser_profile_dir: Path
    openai_api_key: str | None
    openai_model: str
    dzen_editor_url: str
    dzen_headless: bool
    dzen_browser_executable: str | None
    aitunnel_api_key: str | None = None


def get_settings() -> Settings:
    data_dir = Path(os.getenv("DZEN_DATA_DIR", PROJECT_ROOT / "data")).expanduser()
    return Settings(
        data_dir=data_dir,
        database_path=Path(os.getenv("DZEN_DATABASE_PATH", data_dir / "dzen_agent.db")).expanduser(),
        browser_profile_dir=Path(
            os.getenv("DZEN_BROWSER_PROFILE_DIR", PROJECT_ROOT / "browser-profile")
        ).expanduser(),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        dzen_editor_url=os.getenv("DZEN_EDITOR_URL", "https://dzen.ru/profile/editor/id/new"),
        dzen_headless=_as_bool(os.getenv("DZEN_HEADLESS", "false")),
        dzen_browser_executable=os.getenv("DZEN_BROWSER_EXECUTABLE"),
        # Deliberately read only .env, never a database or inherited environment key.
        aitunnel_api_key=dotenv_values(PROJECT_ROOT / ".env").get("AITUNNEL_API_KEY") or None,
    )

