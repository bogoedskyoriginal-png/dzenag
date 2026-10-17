from __future__ import annotations

from uuid import uuid4

from dzen_agent.errors import UserActionRequired
from dzen_agent.locking import exclusive_lock
from dzen_agent.prompt_service import Prompts
from dzen_agent.store import dumps, now_iso


def authorize_retry(store, call_key: str, *, confirmed: bool) -> None:
    if not confirmed:
        raise UserActionRequired("Подтвердите новый платный запрос после проверки истории AITUNNEL.")
    with exclusive_lock(store.database_path.parent / "pipeline.lock"), exclusive_lock(
            store.database_path.parent / "ai.lock"):
        archive = call_key + ":archived:" + uuid4().hex
        with store._connect() as conn:
            row = conn.execute("SELECT * FROM ai_calls WHERE call_key=?", (call_key,)).fetchone()
            if not row or ":archived:" in call_key:
                raise ValueError("Запрос не найден или уже перенесён в историю.")
            conn.execute("UPDATE ai_calls SET call_key=? WHERE call_key=?", (archive, call_key))
            conn.execute("UPDATE usage_events SET call_key=? WHERE call_key=?", (archive, call_key))
        store.log("MANUAL_RETRY", "Пользователь разрешил повтор AI-вызова", {"call_key": call_key})


def refresh_pending_config(store, run_id: int) -> None:
    """Keep week size and slots immutable; apply changed AI settings only to pending work."""
    import json

    with exclusive_lock(store.database_path.parent / "pipeline.lock"):
        run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
        if not run or run["status"] == "SUCCEEDED":
            raise ValueError("Нет незавершённого запуска.")
        old = json.loads(run["settings_json"])
        current = store.settings()
        keys = ["analytics_model", "research_model", "planner_model", "writer_model", "image_model",
                "research_use_web_tool", "max_output_tokens", "image_quality", "max_retries"]
        old.update({key: current[key] for key in keys})
        store.execute("UPDATE pipeline_runs SET settings_json=?,prompts_json=? WHERE id=?",
                      (dumps(old), dumps(Prompts(store).snapshot()), run_id))
        store.log("CONFIG", "Настройки применены к незавершённым этапам", {"run_id": run_id,
                                                                            "timestamp": now_iso()})
