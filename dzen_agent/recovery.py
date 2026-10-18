from __future__ import annotations

from uuid import uuid4

from dzen_agent.errors import UserActionRequired
from dzen_agent.locking import exclusive_lock
from dzen_agent.prompt_service import Prompts
from dzen_agent.store import dumps, now_iso


def reprocess_discovery(store, run_id: int):
    """Replay only a successful saved Discovery receipt. No API client or pipeline is started."""
    import json
    import sqlite3
    from datetime import UTC, datetime

    from dzen_agent.editorial_contracts import DiscoveryV1
    from dzen_agent.editorial_pipeline import native
    from dzen_agent.services import ContentService, search_evidence_summary

    with exclusive_lock(store.database_path.parent / "pipeline.lock"), exclusive_lock(store.database_path.parent / "ai.lock"):
        run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
        if not run or run["status"] == "RUNNING":
            raise UserActionRequired("Нужен остановленный существующий запуск.")
        settings, prompts = json.loads(run["settings_json"]), json.loads(run["prompts_json"])
        if not native(prompts.get("DISCOVERY"), "discovery"):
            raise UserActionRequired("Локальная обработка поддерживает Discovery 1.0 из снимка этого запуска.")
        revision = settings.get("analysis_revision", 0)
        prefix = f"run:{run_id}" + (f":analysis-v{revision}" if revision else "")
        call_key = prefix + ":discovery"
        receipt = store.one("SELECT * FROM ai_calls WHERE call_key=?", (call_key,))
        if not receipt or receipt["status"] != "SUCCEEDED" or not receipt["result_json"]:
            raise UserActionRequired("Нет сохранённого успешного ответа Discovery. Новый запрос не выполнялся.")
        if store.one("SELECT id FROM content_plans WHERE run_id=?", (run_id,)) or store.one(
                "SELECT call_key FROM ai_calls WHERE call_key=?", (prefix + ":plan",)):
            raise UserActionRequired("Planner уже запускался. Его состояние нельзя изменять локальной обработкой Discovery.")
        analytics = store.one("SELECT payload_json FROM analytics_results WHERE run_id=? AND superseded=0 ORDER BY id DESC", (run_id,))
        if not analytics:
            raise UserActionRequired("Нужен сохранённый результат Analytics. Новый анализ не запускается.")
        existing = store.one("SELECT analysis_revision FROM discovery_results WHERE run_id=?", (run_id,))
        if existing and existing["analysis_revision"] != revision:
            raise UserActionRequired("Есть Discovery другой редакции Analytics. Его нельзя перезаписывать локальной обработкой.")
        response = json.loads(receipt["result_json"])

        class SavedResponseOnly:
            def call(self, **kwargs):
                if kwargs["call_key"] != call_key or kwargs["operation"] != "DISCOVERY":
                    raise AssertionError("Локальная обработка не может выполнять другие AI-операции.")
                return json.loads(receipt["result_json"])

        directory = store.database_path.parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        backup = directory / f"{store.database_path.stem}-discovery-run{run_id}-{datetime.now(UTC):%Y%m%dT%H%M%S%f}.db"
        with sqlite3.connect(store.database_path) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
        service = ContentService(store, SavedResponseOnly(), settings=settings, prompts=prompts, run_id=run_id)
        result_id = service.discover(json.loads(analytics["payload_json"]), call_key)
        result = DiscoveryV1.model_validate_json(store.one("SELECT payload_json FROM discovery_results WHERE id=?", (result_id,))["payload_json"])
        ready = sum(i.viability_status == "ready_for_planning" for i in result.candidates)
        target = int(settings["max_articles_per_week"])
        message = (f"Discovery восстановлен из оплаченного ответа: готовых идей {ready} из {target}. "
                   "Planner не запускался. Для полного плана недостаточно подтверждённых идей.") if ready < target else None
        with store._connect() as conn:
            conn.execute("UPDATE pipeline_stages SET status='SUCCEEDED',error=NULL,ended_at=? WHERE run_id=? AND stage='DISCOVERY'",
                         (now_iso(), run_id))
            if message:
                conn.execute("UPDATE pipeline_stages SET status='WAITING',error=? WHERE run_id=? AND stage='PLAN'",
                             (message, run_id))
            conn.execute("UPDATE pipeline_runs SET status=?,error=?,ended_at=? WHERE id=?",
                         ("NEEDS_ACTION" if message else "DISCOVERY_READY", message, now_iso(), run_id))
        report = {"run_id": run_id, "call_key": call_key, "result_id": result_id,
                  "accepted_ids": [i.idea_id for i in result.candidates],
                  "ready_ids": [i.idea_id for i in result.candidates if i.viability_status == "ready_for_planning"],
                  "rejected_count": len(result.rejected_ideas), "target_count": target,
                  "planner_ready": ready >= target, "planner_started": False, "backup_path": str(backup),
                  "previous_run_error": run["error"], "search_evidence": search_evidence_summary(response)}
        store.log("DISCOVERY_REPLAY", "Оплаченный ответ обработан локально. Planner и AI-запросы не запускались.", report)
        return report


def authorize_retry(store, call_key: str, *, confirmed: bool, analytics_run_id: int | None = None) -> None:
    if not confirmed:
        raise UserActionRequired("Подтвердите новый платный запрос после проверки истории AITUNNEL.")
    with exclusive_lock(store.database_path.parent / "pipeline.lock"), exclusive_lock(
            store.database_path.parent / "ai.lock"):
        archive = call_key + ":archived:" + uuid4().hex
        with store._connect() as conn:
            if analytics_run_id is not None:
                import json
                run = conn.execute("SELECT settings_json FROM pipeline_runs WHERE id=?", (analytics_run_id,)).fetchone()
                stage = conn.execute("SELECT status FROM pipeline_stages WHERE run_id=? AND stage='ANALYZE'",
                                     (analytics_run_id,)).fetchone()
                revision = json.loads(run["settings_json"]).get("analysis_revision", 0) if run else 0
                expected = f"run:{analytics_run_id}" + (f":analysis-v{revision}" if revision else "") + ":analytics"
                downstream = conn.execute("SELECT 1 FROM pipeline_stages WHERE run_id=? AND stage NOT IN "
                    "('COLLECT_STATS','ANALYZE') AND status<>'PENDING'", (analytics_run_id,)).fetchone()
                result = conn.execute("SELECT 1 FROM analytics_results WHERE run_id=? AND superseded=0",
                                      (analytics_run_id,)).fetchone()
                if not run or not stage or stage["status"] != "ERROR" or expected != call_key or downstream or result:
                    raise ValueError("Повтор только Analytics доступен для неудавшегося ANALYZE до следующих этапов.")
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
        keys = ["analytics_model", "discovery_model", "research_model", "planner_model", "writer_model", "image_model", "visual_model",
                "research_use_web_tool", "max_output_tokens", "analytics_max_output_tokens",
                "analytics_reasoning_effort", "image_quality", "max_retries"]
        old.update({key: current[key] for key in keys})
        store.execute("UPDATE pipeline_runs SET settings_json=?,prompts_json=? WHERE id=?",
                      (dumps(old), dumps(Prompts(store).snapshot()), run_id))
        store.log("CONFIG", "Настройки применены к незавершённым этапам", {"run_id": run_id,
                                                                            "timestamp": now_iso()})


def restart_analytics(store, run_id: int, *, confirmed: bool) -> None:
    """Explicitly retry changed input before planning; keep historical results and receipts."""
    import json
    if not confirmed:
        raise UserActionRequired("Подтвердите новый платный анализ изменённых данных.")
    with exclusive_lock(store.database_path.parent / "pipeline.lock"), store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute("SELECT * FROM pipeline_runs WHERE id=?", (run_id,)).fetchone()
        latest = conn.execute("SELECT id FROM statistics_imports ORDER BY id DESC LIMIT 1").fetchone()
        if not run or not latest:
            raise ValueError("Нужны существующий запуск и импорт статистики.")
        if conn.execute("SELECT 1 FROM content_plans WHERE run_id=?", (run_id,)).fetchone():
            raise ValueError("Контент-план уже создан. Его основание не заменяется автоматически.")
        settings = json.loads(run["settings_json"])
        settings["analysis_revision"] = int(settings.get("analysis_revision", 0)) + 1
        conn.execute("UPDATE analytics_results SET superseded=1 WHERE run_id=?", (run_id,))
        conn.execute("UPDATE pipeline_runs SET statistics_id=?,settings_json=?,status='PENDING',error=NULL,ended_at=NULL WHERE id=?",
                     (latest["id"], dumps(settings), run_id))
        conn.execute("UPDATE pipeline_stages SET status='PENDING',error=NULL,started_at=NULL,ended_at=NULL WHERE run_id=?",
                     (run_id,))
