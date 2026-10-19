"""Shared receipt replay, error policy and financial authorization for execution."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import UTC, datetime

from dzen_agent.errors import UserActionRequired
from dzen_agent.store import dumps, now_iso


def authorize_run(store, run_id, *, budget_rub, max_calls=200, max_repairs=2, confirmed=False, drafts=False):
    if not confirmed or not math.isfinite(float(budget_rub)) or not 0 < budget_rub <= 10000:
        raise UserActionRequired("Нужно однократное разрешение на бюджет подготовки.")
    if not 1 <= max_calls <= 200 or not 0 <= max_repairs <= 3:
        raise ValueError("Некорректный предел автоматических попыток.")
    existing = store.costs(run_id=run_id)
    if existing["unknown"]:
        raise UserActionRequired("В запуске есть неопределённые расходы. Сначала сверьте квитанции.")
    policy = {"autonomous_preparation": True, "drafts": bool(drafts), "publication": False,
              "authorized_by": "user", "baseline_cost_rub": existing["total"]}
    store.execute("INSERT INTO run_authorizations VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
                  "budget_rub=excluded.budget_rub,reserve_rub=excluded.reserve_rub,max_calls=excluded.max_calls,"
                  "max_repairs=excluded.max_repairs,authorized_at=excluded.authorized_at,policy_json=excluded.policy_json",
                  (run_id, existing["total"] + budget_rub, budget_rub, max_calls, max_repairs, now_iso(), dumps(policy)))
    store.log("AUTHORIZATION", "Пользователь разрешил автономную подготовку в пределах бюджета", {"run_id": run_id, "additional_budget_rub": budget_rub, **policy})


def authorization(store, run_id):
    return store.one("SELECT * FROM run_authorizations WHERE run_id=?", (run_id,)) if run_id else None


def maximum_cost(settings, model, payload, image_path=None):
    """Conservative bound: full model context + maximum output, highest price tier, fees."""
    catalog = settings.get("public_catalog", {})
    if image_path:
        raise UserActionRequired("Для автономного бюджета используются реальные фото, а не генерация сцен.")
    item = catalog.get("chat", {}).get(model)
    if not item or any(k not in item for k in ("context_size", "prompt_cost", "completion_cost")):
        raise UserActionRequired("Неизвестна верхняя стоимость модели. Обновите каталог перед платным запуском.")
    if not isinstance(payload.get("max_tokens"), int) or payload["max_tokens"] <= 0:
        raise UserActionRequired("Для ограничения расходов нужен max_tokens.")
    tiers = [item, *item.get("price_tiers", [])]
    input_rate = max(float(t.get("prompt_cost", 0)) for t in tiers)
    output_rate = max(float(t.get("completion_cost", 0)) for t in tiers)
    fee = float(item.get("requests_cost", 0)) / 1000
    if payload.get("tools"):
        searches = payload.get("max_tool_calls")
        parameters = payload["tools"][0].get("parameters", {})
        if (type(searches) is not int or not 1 <= searches <= 3 or parameters.get("max_uses") != searches
                or parameters.get("engine") != "parallel" or parameters.get("mode") != "basic"
                or not 1 <= parameters.get("max_results", 0) <= 10):
            raise UserActionRequired("Неограниченные внешние инструменты не допускаются бюджетом.")
        fee += 1.5 * searches  # Parallel basic: documented ~1 RUB/search; headroom before the 20% margin.
    value = (int(item["context_size"]) * input_rate + payload["max_tokens"] * output_rate) / 1_000_000 + fee
    if not math.isfinite(value) or value < 0:
        raise UserActionRequired("Некорректная цена модели.")
    return math.ceil(value * 1.2 * 100) / 100


def reserve_call(store, run_id, call_key, model, payload, image_path=None):
    grant = authorization(store, run_id)
    if not grant:
        return
    cost = store.costs(run_id=run_id)
    for row in store.rows("SELECT r.call_key,a.status FROM call_reservations r LEFT JOIN ai_calls a ON a.call_key=r.call_key WHERE r.status='PENDING'"):
        if row["status"] == "SUCCEEDED":
            settle_call(store, row["call_key"])
    pending = store.rows("SELECT * FROM call_reservations WHERE status<>'SETTLED'")
    if cost["unknown"] or pending:
        raise UserActionRequired("Неопределённая стоимость либо незавершённый запрос. Новые расходы заблокированы.")
    calls = store.one("SELECT COUNT(*) AS n FROM call_reservations WHERE run_id=?", (run_id,))["n"]
    bound = maximum_cost(store.settings(), model, payload, image_path)
    if store.costs(days=30)["total"] + bound > store.settings()["monthly_soft_budget_rub"]:
        raise UserActionRequired("Месячный бюджет не позволяет зарезервировать следующий запрос.")
    if calls >= grant["max_calls"] or cost["total"] + bound > grant["budget_rub"] + 1e-9:
        raise UserActionRequired("Бюджет запуска не позволяет безопасно зарезервировать следующий запрос.")
    store.execute("INSERT INTO call_reservations VALUES(?,?,?,'PENDING',?)", (call_key, run_id, bound, now_iso()))


def settle_call(store, key, uncertain=False):
    reservation = store.one("SELECT * FROM call_reservations WHERE call_key=?", (key,))
    if not reservation:
        return
    usage = store.one("SELECT COUNT(*) AS n,SUM(cost_rub IS NULL) AS unknown,SUM(cost_rub) AS cost FROM usage_events WHERE call_key=?", (key,))
    state = "UNCERTAIN" if uncertain or not usage["n"] or usage["unknown"] else "SETTLED"
    if usage["cost"] is not None and usage["cost"] > reservation["reserved_rub"]:
        state = "OVERRUN"
    store.execute("UPDATE call_reservations SET status=? WHERE call_key=?", (state, key))


def resolved_call(store, key):
    row = store.one("SELECT * FROM response_repairs WHERE base_key=?", (key,))
    if not row:
        return key, None
    try:
        feedback = json.loads(row["feedback"])
    except ValueError:
        feedback = {"error": row["feedback"], "category": category(row["feedback"])}
    if not isinstance(feedback, dict):
        feedback = {"error": str(feedback), "category": "LOCAL_RECOVERABLE"}
    previous = key if row["attempt"] == 1 else key + f":repair:{row['attempt']-1}"
    return key + f":repair:{row['attempt']}", {"previous_key": previous, **feedback}


def perform(service, operation):
    """One bounded policy for both global operations and individual article operations."""
    service.last_call_key = None
    while True:
        try:
            return operation()
        except Exception as error:
            if getattr(error, "reframe", False):
                raise  # Change the disproved premise before buying another identical research pass.
            grant = authorization(service.store, service.run_id)
            key = getattr(service, "last_call_key", None)
            kind = category(error)
            if not grant or not key or kind not in ("LOCAL_RECOVERABLE", "EDITORIAL_BLOCKER"):
                raise
            receipt = service.store.one("SELECT * FROM ai_calls WHERE call_key=?", (key,))
            if not receipt:
                raise
            if receipt["status"] != "SUCCEEDED":
                diagnostic = json.loads(receipt["result_json"] or "{}").get("http_error", {})
                usage = service.store.one("SELECT COUNT(*) AS n,SUM(cost_rub IS NULL) AS unknown FROM usage_events WHERE call_key=?", (key,))
                if receipt["status"] != "ERROR" or diagnostic.get("http_status") != 429 or not usage["n"] or usage["unknown"]:
                    raise
                kind = "RETRYABLE_CONFIRMED"
            base = re.sub(r":repair:\d+$", "", key)
            current = service.store.one("SELECT attempt FROM response_repairs WHERE base_key=?", (base,))
            attempt = (current["attempt"] if current else 0) + 1
            if attempt > grant["max_repairs"]:
                raise
            service.store.execute("INSERT INTO response_repairs VALUES(?,?,?,?) ON CONFLICT(base_key) DO UPDATE SET "
                                  "attempt=excluded.attempt,feedback=excluded.feedback,updated_at=excluded.updated_at",
                                  (base, attempt, dumps({"error": str(error), "category": kind, "previous_key":key}), now_iso()))
            service.store.log("RECOVERY", "Разрешён ограниченный исправляющий ответ в пределах бюджета", {
                "run_id": service.run_id, "base_key": base, "attempt": attempt, "category": kind, "error": str(error)})


def decode_json(content):
    value = content.strip().lstrip("\ufeff")
    if value.startswith("```") and value.endswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(value)


def normalize_enums(value, schema):
    root, repairs = schema.model_json_schema(), []
    def visit(item, shape, path):
        if "$ref" in shape:
            shape = root["$defs"][shape["$ref"].rsplit("/", 1)[-1]]
        if isinstance(item, str) and "enum" in shape:
            choices = [v for v in shape["enum"] if isinstance(v, str) and v.casefold() == item.strip().casefold()]
            if len(choices) == 1 and item != choices[0]:
                repairs.append({"path": path, "before": item, "after": choices[0]})
                return choices[0]
        if isinstance(item, dict):
            return {k: visit(v, shape.get("properties", {}).get(k, {}), [*path, k]) for k,v in item.items()}
        if isinstance(item, list):
            return [visit(v, shape.get("items", {}), [*path, n]) for n,v in enumerate(item)]
        for branch in shape.get("anyOf", []):
            if branch.get("type") != "null":
                return visit(item, branch, path)
        return item
    return visit(value, root, []), repairs


def authoritative_fields(value, schema, context):
    """Numeric computations belong to the importer, never to the language model."""
    if schema.__name__ == "ImageV1" and isinstance(value, dict) and isinstance(value.get("placements"), list):
        # Python creates and inserts the cover. A proposed virtual cover is not a downloaded asset.
        # Inline references and the cover's real source still undergo all schema/evidence checks.
        removed = [i for i,p in enumerate(value["placements"]) if isinstance(p, dict) and p.get("role") == "cover"]
        if removed:
            return {**value, "placements": [p for i,p in enumerate(value["placements"]) if i not in removed]}, [
                {"path": ["placements", i], "before": value["placements"][i], "after": "Python-owned cover insertion"} for i in removed]
        return value, []
    if schema.__name__ != "Analytics" or schema.__module__ != "dzen_agent.analytics_contract":
        return value, []
    expected = context.get("analysis_config", {}).get("validated_funnel")
    if expected is None or not isinstance(value, dict) or not isinstance(value.get("funnel"), dict):
        return value, []
    repairs = [{"path": ["funnel", k], "before": value["funnel"].get(k), "after": v}
               for k,v in expected.items() if value["funnel"].get(k) != v]
    if repairs:
        value = {**value, "funnel": dict(expected)}
        if value.get("status") == "ok":
            value["status"] = "partial"
        quality = value.get("data_quality")
        if isinstance(quality, dict) and isinstance(quality.get("comparison_limitations"), list):
            quality["comparison_limitations"].append("Числовая воронка восстановлена из проверенных данных Python. Неизвестные и несопоставимые показатели оставлены null.")
    return value, repairs


def category(error):
    from dzen_agent.errors import EditorialBlocker
    if isinstance(error, EditorialBlocker):
        return "EDITORIAL_BLOCKER"
    text = str(error).casefold()
    if "ответ, не соответствующий структуре" in text:
        return "LOCAL_RECOVERABLE"
    if any(v in text for v in ("timeout", "5xx", "неопределённая стоимость", "неопределенные расходы",
                               "неопределённые расходы", "стоимость неизвестна", "стоимость не подтверждена",
                               "результат предыдущего ai-запроса неизвестен", "результат запроса неизвестен",
                               "незавершённый запрос")):
        return "PAYMENT_UNCERTAIN"
    if any(v in text for v in ("бюджет", "баланс", "расход")):
        return "BUDGET_BLOCKER"
    if any(v in text for v in ("войдите", "авториза", "captcha", "401", "403")):
        return "AUTH_REQUIRED"
    if any(v in text for v in ("фактур", "источник", "research", "evidence", "цитат")):
        return "EDITORIAL_BLOCKER"
    if isinstance(error, (ValueError, UserActionRequired)):
        return "LOCAL_RECOVERABLE"
    return "FATAL"


def backup(store, purpose):
    directory = store.database_path.parent / "backups"
    directory.mkdir(exist_ok=True)
    path = directory / f"{store.database_path.stem}-{purpose}-{datetime.now(UTC):%Y%m%dT%H%M%S%f}.db"
    with sqlite3.connect(store.database_path) as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    return str(path)


def replay_stage(store, run_id, stage):
    """The normal stage handler with a provider that can ONLY read successful receipts."""
    from dzen_agent.locking import exclusive_lock
    from dzen_agent.services import ContentService
    from dzen_agent.pipeline.orchestrator import Pipeline
    if stage not in ("ANALYZE", "DISCOVERY", "PLAN", "RESEARCH", "REFRESH_RESEARCH", "WRITE", "IMAGE", "QA"):
        raise ValueError("Этот этап нельзя обработать локально.")
    with exclusive_lock(store.database_path.parent / "pipeline.lock"), exclusive_lock(store.database_path.parent / "ai.lock"):
        run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
        if not run or run["status"] == "RUNNING":
            raise UserActionRequired("Запуск уже выполняется либо отсутствует.")
        saved = backup(store, f"replay-{run_id}")

        class ReceiptsOnly:
            def call(self, **kwargs):
                row = store.one("SELECT * FROM ai_calls WHERE call_key=? AND status='SUCCEEDED'", (kwargs["call_key"],))
                if not row or not kwargs["call_key"].startswith(f"run:{run_id}:"):
                    raise UserActionRequired("Нет сохранённой квитанции. Платный запрос не выполнялся.")
                return json.loads(row["result_json"])

        provider = ReceiptsOnly()
        service = ContentService(store, provider, settings=json.loads(run["settings_json"]), prompts=json.loads(run["prompts_json"]), run_id=run_id)
        worker = Pipeline(store, provider)
        worker._seen_research, worker._item_errors = set(), []
        worker._stage(stage, run, service)
        status = "ERROR" if worker._item_errors else "SUCCEEDED"
        message = "; ".join(worker._item_errors) or None
        store.execute("UPDATE pipeline_stages SET status=?,error=?,ended_at=? WHERE run_id=? AND stage=?", (status, message, now_iso(), run_id, stage))
        store.execute("UPDATE pipeline_runs SET status='NEEDS_ACTION',error=?,ended_at=? WHERE id=?", (message, now_iso(), run_id))
        report = {"run_id": run_id, "stage": stage, "status": status, "errors": worker._item_errors, "backup_path": saved, "paid_calls": 0}
        store.log("LOCAL_REPLAY", "Сохранённые квитанции обработаны обычным обработчиком этапа", report)
        return report
