from __future__ import annotations

import re
import traceback
from pathlib import Path
from uuid import uuid4

from dzen_agent.config import PROJECT_ROOT, get_settings


class UserActionRequired(RuntimeError):
    pass


class EditorialBlocker(UserActionRequired):
    def __init__(self, message, *, reframe=False):
        self.reframe = reframe
        super().__init__(message)


def redact(text: str) -> str:
    for secret in (get_settings().aitunnel_api_key, get_settings().openai_api_key):
        if secret:
            text = text.replace(secret, "[СКРЫТО]")
    text = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [СКРЫТО]", text)
    return re.sub(r"sk-[\w-]{8,}", "[СКРЫТО]", text)


def log_exception(error: Exception, stage: str) -> Path:
    directory = PROJECT_ROOT / "logs"
    directory.mkdir(exist_ok=True)
    path = directory / f"error-{uuid4().hex}.log"
    path.write_text(redact(stage + "\n" + "".join(traceback.format_exception(error))), "utf-8")
    return path
