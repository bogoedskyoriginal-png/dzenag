"""Bounded, explicitly authorized incremental Discovery. Never restart a paid batch."""
from __future__ import annotations

import json
import math

from dzen_agent.editorial_contracts import DiscoveryV1
from dzen_agent.errors import UserActionRequired, redact
from dzen_agent.locking import exclusive_lock
from dzen_agent.store import dumps, now_iso


def scope(store, run_id):
    run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    if not run:
        raise ValueError("Запуск не найден.")
    return run, json.loads(run["settings_json"])


def supply_status(store, run_id):
    run, settings = scope(store, run_id)
    revision = settings.get("analysis_revision", 0)
    row = store.one("SELECT * FROM discovery_results WHERE run_id=?", (run_id,))
    result = DiscoveryV1.model_validate_json(row["payload_json"]) if row and row["analysis_revision"] == revision else None
    ready = sum(i.viability_status == "ready_for_planning" for i in result.candidates) if result else 0
    requests = store.rows("SELECT * FROM discovery_requests WHERE run_id=? AND analysis_revision=? ORDER BY created_at,call_key", (run_id, revision))
    committed = sum(r["cost_rub"] if r["cost_rub"] is not None else r["reserved_rub"] for r in requests)
    grant = store.one("SELECT * FROM discovery_topups WHERE run_id=? AND analysis_revision=?", (run_id, revision))
    if not grant:
        from dzen_agent.execution import authorization
        policy = authorization(store, run_id)
        if policy:
            grant = {"budget_rub": policy["budget_rub"], "reserve_rub": 0,
                     "max_calls": policy["max_repairs"] + 1, "authorized_at": policy["authorized_at"],
                     "run_budget_managed": True}
    return {"ready": ready, "target": settings["max_articles_per_week"],
            "missing": max(0, settings["max_articles_per_week"] - ready), "grant": grant,
            "requests": requests, "committed_rub": committed, "revision": revision, "result": result}


def authorize_topup(store, run_id, *, budget_rub, max_calls, reserve_rub, confirmed):
    if not confirmed:
        raise UserActionRequired("Подтвердите дополнительный платный поиск Discovery.")
    if (any(not math.isfinite(float(v)) or float(v) <= 0 for v in (budget_rub, reserve_rub))
            or reserve_rub > budget_rub or type(max_calls) is not int or not 1 <= max_calls <= 5):
        raise ValueError("Нужны положительный бюджет, резерв на запрос не выше бюджета и 1–5 запросов.")
    with exclusive_lock(store.database_path.parent / "pipeline.lock"):
        status = supply_status(store, run_id)
        if store.one("SELECT id FROM content_plans WHERE run_id=?", (run_id,)):
            raise UserActionRequired("План уже создан. Дозаполнение не заменяет его идеи.")
        # A new explicit consent extends the lifetime allowance; prior spend never disappears.
        budget = max(status["committed_rub"], (status["grant"] or {}).get("budget_rub", 0)) + float(budget_rub)
        calls = max(len(status["requests"]), (status["grant"] or {}).get("max_calls", 0)) + max_calls
        store.execute("INSERT INTO discovery_topups VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,analysis_revision) "
                      "DO UPDATE SET budget_rub=excluded.budget_rub,max_calls=excluded.max_calls,reserve_rub=excluded.reserve_rub,authorized_at=excluded.authorized_at",
                      (run_id, status["revision"], budget, calls, reserve_rub, now_iso()))
        store.log("DISCOVERY_BUDGET", "Пользователь разрешил ограниченное дозаполнение Discovery",
                  {"run_id": run_id, "additional_budget_rub": budget_rub, "additional_calls": max_calls, "reserve_rub": reserve_rub})


def fill_discovery(service, analytics, base_key):
    store, run_id = service.store, service.run_id
    while True:
        status = supply_status(store, run_id)
        # Recover a crash after atomic batch/aggregate commit without another model call.
        for request in status["requests"]:
            if request["status"] != "DONE" and store.one("SELECT id FROM discovery_batches WHERE call_key=?", (request["call_key"],)):
                usage = store.one("SELECT SUM(cost_rub) AS cost,COUNT(*) AS n,SUM(cost_rub IS NULL) AS unknown FROM usage_events WHERE call_key=?",
                                  (request["call_key"],))
                cost = usage["cost"] if usage["n"] and not usage["unknown"] else None
                store.execute("UPDATE discovery_requests SET status='DONE',cost_rub=?,error=NULL,updated_at=? WHERE call_key=?",
                              (cost, now_iso(), request["call_key"]))
        status = supply_status(store, run_id)
        if not status["missing"]:
            return status["result"].model_dump(mode="json")
        if store.settings()["stop_requested"]:
            raise UserActionRequired("Дозаполнение остановлено пользователем.")
        # Resume a received/uncertain response before considering another paid request.
        unfinished = next((r for r in status["requests"] if r["status"] != "DONE"), None)
        grant = status["grant"]
        if not grant:
            raise UserActionRequired(f"Не хватает идей Discovery: {status['ready']} из {status['target']}. "
                                     "Требуется разрешение на дополнительный платный поиск либо уменьшение плана.")
        if unfinished:
            receipt = store.one("SELECT * FROM ai_calls WHERE call_key=?", (unfinished["call_key"],))
            if not receipt or receipt["status"] != "SUCCEEDED":
                raise UserActionRequired("Результат дополнительного поиска неизвестен или завершился ошибкой. "
                                         "Новый платный запрос заблокирован; проверьте сохранённую квитанцию.")
            request = unfinished
        else:
            if any(r["cost_rub"] is None for r in status["requests"]):
                raise UserActionRequired("Стоимость предыдущего дозаполнения неизвестна. Новые расходы заблокированы.")
            if not grant.get("run_budget_managed") and any(r["cost_rub"] > r["reserved_rub"] and r["created_at"] >= grant["authorized_at"] for r in status["requests"]):
                raise UserActionRequired("Превышен резерв предыдущего поиска. Для новых расходов нужно отдельное разрешение.")
            if (len(status["requests"]) >= grant["max_calls"] or
                    status["committed_rub"] + grant["reserve_rub"] > grant["budget_rub"] + 1e-9):
                raise UserActionRequired("Лимит дозаполнения исчерпан. Можно уменьшить объём плана или отдельно разрешить новый бюджет.")
            number = len(status["requests"]) + 1
            key = f"{base_key}:topup:{number}"
            additional = {"mode": "missing_candidates_only", "missing_count": status["missing"],
                          "exclude_previously_found_and_rejected": True}
            # Reserve BEFORE dispatch. A crash before a receipt is reconciled manually, never repeated blindly.
            store.execute("INSERT INTO discovery_requests VALUES(?,?,?,'RESERVED',?,NULL,?,NULL,?,?)",
                          (key, run_id, status["revision"], grant["reserve_rub"], dumps(additional), now_iso(), now_iso()))
            request = store.one("SELECT * FROM discovery_requests WHERE call_key=?", (key,))
        try:
            from dzen_agent.execution import perform
            perform(service, lambda: service.discover(analytics, request["call_key"], additional=json.loads(request["context_json"])))
            store.execute("UPDATE discovery_requests SET status='DONE',error=NULL,updated_at=? WHERE call_key=?", (now_iso(), request["call_key"]))
        except Exception as error:
            processed = store.one("SELECT id FROM discovery_batches WHERE call_key=?", (request["call_key"],))
            if isinstance(error, UserActionRequired) and processed:
                store.execute("UPDATE discovery_requests SET status='DONE',error=NULL,updated_at=? WHERE call_key=?",
                              (now_iso(), request["call_key"]))
            else:
                store.execute("UPDATE discovery_requests SET status='ERROR',error=?,updated_at=? WHERE call_key=?",
                              (redact(str(error)), now_iso(), request["call_key"]))
                raise
        finally:
            usage = store.one("SELECT SUM(cost_rub) AS cost,COUNT(*) AS n,SUM(cost_rub IS NULL) AS unknown "
                              "FROM usage_events WHERE call_key=? OR call_key LIKE ?", (request["call_key"], request["call_key"] + ":repair:%"))
            cost = usage["cost"] if usage["n"] and not usage["unknown"] else None
            store.execute("UPDATE discovery_requests SET cost_rub=?,updated_at=? WHERE call_key=?", (cost, now_iso(), request["call_key"]))
        updated = supply_status(store, run_id)
        actual = store.one("SELECT cost_rub FROM discovery_requests WHERE call_key=?", (request["call_key"],))["cost_rub"]
        if not grant.get("run_budget_managed") and updated["missing"] and actual is not None and (actual > request["reserved_rub"] or updated["committed_rub"] > grant["budget_rub"]):
            raise UserActionRequired("Фактическая стоимость превысила локальный резерв. Дополнительные поиски остановлены; проверьте расходы.")


def reduce_plan(store, run_id, count, *, confirmed):
    """Before planning only: preserve existing search results and select the first valid configured slots."""
    if not confirmed:
        raise UserActionRequired("Подтвердите уменьшение объёма и новое расписание.")
    from dzen_agent.services import week_slots
    with exclusive_lock(store.database_path.parent / "pipeline.lock"):
        run, settings = scope(store, run_id)
        status = supply_status(store, run_id)
        prefix = f"run:{run_id}" + (f":analysis-v{status['revision']}" if status["revision"] else "")
        if (store.one("SELECT id FROM content_plans WHERE run_id=?", (run_id,)) or
                store.one("SELECT call_key FROM ai_calls WHERE call_key=?", (prefix + ":plan",))):
            raise UserActionRequired("Planner уже запускался. Существующий план и квитанции не изменены.")
        if type(count) is not int or not 1 <= count <= status["ready"] or count >= status["target"]:
            raise ValueError("Выберите меньший объём в пределах числа подтверждённых идей.")
        old = dict(settings)
        settings["max_articles_per_week"] = count
        slots = week_slots(run["week_start"], settings)
        occupied = store.rows("SELECT scheduled_at FROM concepts UNION SELECT scheduled_at FROM article_details WHERE scheduled_at IS NOT NULL")
        from datetime import datetime
        if any(datetime.fromisoformat(s) == datetime.fromisoformat(r["scheduled_at"]) for s in slots for r in occupied):
            raise UserActionRequired("Новое расписание конфликтует с существующими статьями.")
        store.execute("UPDATE pipeline_runs SET settings_json=?,status='DISCOVERY_READY',error=NULL WHERE id=?", (dumps(settings), run_id))
        store.execute("UPDATE pipeline_stages SET status='PENDING',error=NULL WHERE run_id=? AND stage='PLAN'", (run_id,))
        store.log("PLAN_REDUCTION", "Пользователь уменьшил объём до запуска Planner", {"run_id": run_id, "before": old, "after": settings, "slots": slots})
        return slots
