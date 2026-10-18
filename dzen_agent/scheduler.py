from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

from dzen_agent.config import PROJECT_ROOT
from dzen_agent.store import MSK, Store


def task_name() -> str:
    suffix = hashlib.sha256(str(PROJECT_ROOT).lower().encode()).hexdigest()[:10]
    return f"DzenAgent-{suffix}"


def next_run(settings: dict, now: datetime | None = None) -> str | None:
    if not settings["scheduler_enabled"] or settings["stop_requested"]:
        return None
    now = (now or datetime.now(MSK)).astimezone(MSK)
    hour, minute = map(int, settings["scheduler_time"].split(":"))
    candidate = (now + timedelta(days=(settings["scheduler_day"] - now.weekday()) % 7)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate.isoformat()


def install(store: Store) -> None:
    if os.name != "nt":
        raise RuntimeError("Установка расписания поддерживается на Windows.")
    settings = store.settings()
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(PROJECT_ROOT / "scripts" / "install_scheduler.ps1"),
               "-TaskName", task_name(), "-Day", str(settings["scheduler_day"]),
               "-At", settings["scheduler_time"]]
    result = subprocess.run(command, capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Windows не смог установить расписание. Проверьте Планировщик заданий.")
    store.set_setting("scheduler_enabled", True)


def remove(store: Store) -> None:
    # Disable the application guard even if Task Scheduler is temporarily unavailable.
    store.set_setting("scheduler_enabled", False)
    if os.name == "nt":
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(PROJECT_ROOT / "scripts" / "remove_scheduler.ps1"),
                                 "-TaskName", task_name()], capture_output=True, timeout=45,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError("В приложении расписание выключено; удалите задание Windows вручную.")


def windows_task_status() -> dict:
    if os.name != "nt":
        return {"state": "Недоступно: требуется Windows"}
    # Name is locally generated, not user input; no shell interpolation of paths/secrets.
    command = "$t=Get-ScheduledTask -TaskName '" + task_name() + "' -ErrorAction SilentlyContinue; " \
              "if ($t) { $i=$t | Get-ScheduledTaskInfo; " \
              "@{state=[string]$t.State;next=[string]$i.NextRunTime;" \
              "last_result=$i.LastTaskResult} | ConvertTo-Json -Compress } " \
              "else { '{\"state\":\"Не установлено\"}' }"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        return json.loads(result.stdout.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeError):
        return {"state": "Не удалось проверить Windows Task Scheduler"}


def launch_pipeline(run_id: int, *, send=False, analytics_only=False) -> int:
    if send and analytics_only:
        raise ValueError("Выберите только Analytics или отправку в Дзен.")
    directory = PROJECT_ROOT / "logs"
    directory.mkdir(exist_ok=True)
    command = [sys.executable, "-m", "dzen_agent.pipeline.run", "--run-id", str(run_id)]
    if send:
        command.append("--send")
    if analytics_only:
        command.append("--analytics-only")
    with (directory / f"pipeline-{run_id}.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=PROJECT_ROOT, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return process.pid
