"""Build honest, bounded Analytics input; unknown metrics/windows remain unknown."""
from __future__ import annotations

import json
import math
from dzen_agent.analytics_contract import Period, parse_analytics, timestamp
from dzen_agent.store import Store, now_iso

COUNTERS = ("impressions", "opens", "views", "completions", "subscriptions", "unsubscribes", "likes", "comments")
UNPUBLISHED = {"DRAFT", "RESEARCHED", "WRITTEN", "IMAGE_READY", "READY_FOR_REVIEW", "APPROVED",
               "PREPARING_IN_DZEN", "SCHEDULED", "SCHEDULED_VERIFIED", "ЧЕРНОВИК", "ОТЛОЖЕНО"}


def read_metadata(row: dict | None) -> dict:
    return json.loads(row.get("metadata_json") or "{}") if row else {}


def validate_metadata(value: dict) -> dict:
    allowed = {"measurement_window", "period", "metric_definitions", "subscription_attribution_confirmed", "source"}
    if set(value) - allowed:
        raise ValueError("Неизвестные настройки периода статистики.")
    window = value.get("measurement_window", "unknown")
    if window not in ("unknown", "lifetime", "calendar_period", "first_24h", "first_72h", "first_7d"):
        raise ValueError("Неизвестный период измерения.")
    period = Period.model_validate(value.get("period") or {"start": None, "end": None, "as_of": None,
                                                           "timezone": "Europe/Moscow"})
    if window == "calendar_period" and not (period.start and period.end):
        raise ValueError("Укажите начало и окончание календарного периода.")
    return {**value, "measurement_window": window, "period": period.model_dump()}


def build_analytics_input(store: Store, statistics_id: int, *, run_id=None) -> dict:
    current = store.one("SELECT * FROM statistics_imports WHERE id=?", (statistics_id,))
    if not current:
        raise ValueError("Импорт статистики не найден.")
    metadata = validate_metadata(read_metadata(current))
    raw_rows = json.loads(current["rows_json"])
    eligible = [(i, r) for i, r in enumerate(raw_rows) if str(r.get("status", "")).upper() not in UNPUBLISHED]
    sampled = len(eligible) > 200
    if sampled:
        eligible = [eligible[i * len(eligible) // 200] for i in range(200)]
    articles = []
    for index, row in eligible:
        identifier = row.get("article_id") or row.get("url") or f"csv:{statistics_id}:row:{index + 1}"
        metrics = {}
        for key in COUNTERS + ("read_time_seconds", "reads", "subscribers", "ctr"):
            value = row.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                metrics[key] = int(value) if key in COUNTERS and float(value).is_integer() else value
            else:
                metrics[key] = None
        articles.append({"article_id": identifier, "id_source": "csv_or_url" if row.get("article_id") or row.get("url") else "local_row_id",
            "title": str(row.get("title") or "")[:400], "published_at": row.get("date", row.get("published_at")),
            "status": row.get("status") or "unknown", "topic": str(row.get("topic") or "")[:300],
            **{key: row.get(key) for key in ("cluster", "format", "headline_type", "cover_type", "length_chars") if key in row},
            "measurement_window": row.get("measurement_window") or metadata["measurement_window"], "metrics": metrics})
    ids = [a["article_id"] for a in articles]
    windows = {a["measurement_window"] for a in articles}
    comparable = (not sampled and len(ids) == len(set(ids)) and len(windows) == 1
                  and next(iter(windows), "unknown") in ("calendar_period", "first_24h", "first_72h", "first_7d"))
    totals = {}
    for key in ("impressions", "opens", "completions", "subscriptions"):
        values = [a["metrics"][key] for a in articles]
        totals[key] = sum(values) if comparable and values and all(
            isinstance(v, int) and v >= 0 for v in values) else None
    # Article-level subscription attribution must be explicitly supplied, never inferred from channel totals.
    if not metadata.get("subscription_attribution_confirmed"):
        totals["subscriptions"] = None
    for name, numerator, denominator, factor in (
        ("ctr", "opens", "impressions", 1), ("completion_rate", "completions", "opens", 1),
        ("completion_per_impression", "completions", "impressions", 1),
        ("subscriptions_per_1000_opens", "subscriptions", "opens", 1000),
    ):
        n, d = totals[numerator], totals[denominator]
        ratio = n / d * factor if n is not None and d else None
        totals[name] = ratio if ratio is None or factor == 1000 or ratio <= 1 else None
    previous = store.one("SELECT * FROM statistics_imports WHERE id<? ORDER BY id DESC LIMIT 1", (statistics_id,))
    previous_meta = read_metadata(previous)
    previous_analytics = store.one("SELECT payload_json FROM analytics_results WHERE statistics_id=? AND superseded=0 "
                                   "ORDER BY id DESC LIMIT 1", (previous["id"],)) if previous else None
    previous_result = json.loads(previous_analytics["payload_json"]) if previous_analytics else None
    if previous_result:
        parse_analytics(previous_result)  # Do not silently reinterpret corrupt historic results.
    # Merely having a previous CSV is not proof of comparable cohorts or measurement definitions.
    comparison_available = False
    if previous_result and previous_result.get("contract_version") == "1.0" and not sampled:
        old_period = previous_meta.get("period", {})
        new_period = metadata["period"]
        if (metadata["measurement_window"] == previous_meta.get("measurement_window") == "calendar_period"
                and all(old_period.get(k) and new_period.get(k) for k in ("start", "end"))
                and metadata.get("metric_definitions", {}) == previous_meta.get("metric_definitions", {})
                and json.loads(current["columns_json"]) == json.loads(previous["columns_json"])):
            old_start, old_end = (timestamp(old_period[k], old_period.get("timezone", "Europe/Moscow"))
                                  for k in ("start", "end"))
            new_start, new_end = (timestamp(new_period[k], new_period["timezone"]) for k in ("start", "end"))
            comparison_available = (old_end - old_start == new_end - new_start and old_end < new_start
                                    and previous_result["period"] == old_period
                                    and previous_result["status"] != "insufficient_data")
    context = {"run_context": {"pipeline_run_id": run_id, "analysis_at": now_iso(),
        "import_id": statistics_id, "imported_at": current["created_at"], "period": metadata["period"]},
        "articles": articles, "channel_metrics": None,
        "metric_definitions": {"provided": metadata.get("metric_definitions", {}),
            "csv_columns": json.loads(current["columns_json"]),
            "warnings": ["views не равны opens; reads и subscribers сохранены без переименования.",
                         "ctr в CSV может быть процентом; используйте opens/impressions при наличии оснований."]},
        "editorial_context": {"channel_topic": store.settings()["channel_topic"],
            "recent_publications": store.rows("SELECT title,topic,status FROM articles WHERE status='PUBLISHED' "
                                               "ORDER BY updated_at DESC,id DESC LIMIT 20")},
        "previous_analytics": previous_result, "experiment_history": None,
        "analysis_config": {"measurement_window": metadata["measurement_window"],
            "comparison_available": comparison_available, "previous_import_id": previous["id"] if previous else None,
            "previous_measurement_window": previous_meta.get("measurement_window", "unknown"),
            "original_row_count": len(raw_rows), "excluded_unpublished": len(raw_rows) - len([
                r for r in raw_rows if str(r.get("status", "")).upper() not in UNPUBLISHED]),
            "sampled": sampled, "sampling_method": "uniform_rows" if sampled else "all_eligible_rows",
            "maturity_threshold": None, "kpi_priorities": None, "validated_funnel": totals,
            "limitations": ([] if comparison_available else ["Временное сравнение не подтверждено; trend должен быть unknown."])
                + ["Суммы lifetime, повторных замеров, смешанных и неизвестных окон не рассчитываются."]}}
    return context
