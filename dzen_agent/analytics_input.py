"""Build honest, bounded Analytics input; unknown metrics/windows remain unknown."""
from __future__ import annotations

import json
from dzen_agent.analytics_contract import Period, parse_analytics, timestamp
from dzen_agent.analytics_statistics import prepare_publications
from dzen_agent.store import Store, now_iso


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


def prepare_analytics_input(store: Store, statistics_id: int, *, run_id=None) -> tuple[dict, dict]:
    current = store.one("SELECT * FROM statistics_imports WHERE id=?", (statistics_id,))
    if not current:
        raise ValueError("Импорт статистики не найден.")
    metadata = validate_metadata(read_metadata(current))
    raw_rows = json.loads(current["rows_json"])
    columns = json.loads(current["columns_json"])
    articles, summary, audit = prepare_publications(raw_rows, statistics_id, metadata, columns)
    population = audit["articles"]
    sampled = len(articles) < len(population)
    windows = {a["measurement_window"] for a in population}
    comparable = (not audit["data_quality"]["duplicate_id_count"] and len(windows) == 1
                  and next(iter(windows), "unknown") in ("calendar_period", "first_24h", "first_72h", "first_7d"))
    totals = {}
    for key in ("impressions", "opens", "completions", "subscriptions"):
        values = [a["metrics"][key] for a in population]
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
    if not audit["ctr_compatible"]:
        totals["ctr"] = None
    previous = store.one("SELECT * FROM statistics_imports WHERE id<? ORDER BY id DESC LIMIT 1", (statistics_id,))
    previous_meta = read_metadata(previous)
    previous_analytics = store.one("SELECT payload_json FROM analytics_results WHERE statistics_id=? AND superseded=0 "
                                   "ORDER BY id DESC LIMIT 1", (previous["id"],)) if previous else None
    previous_result = json.loads(previous_analytics["payload_json"]) if previous_analytics else None
    if previous_result:
        parse_analytics(previous_result)  # Do not silently reinterpret corrupt historic results.
    # Merely having a previous CSV is not proof of comparable cohorts or measurement definitions.
    comparison_available = False
    if previous_result and previous_result.get("contract_version") == "1.0":
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
        "articles": articles, "channel_metrics": summary,
        "metric_definitions": {"provided": metadata.get("metric_definitions", {}),
            "csv_columns": columns, "ctr_compatible": audit["ctr_compatible"],
            "warnings": ["views не равны opens; reads и subscribers сохранены без переименования.",
                         "ctr в выгрузке может быть процентом, а не долей.",
                         "При неподтверждённых определениях opens/impressions — не CTR. reads — отдельный счётчик; "
                         "'Дочитывания и просмотры' не равно completions. Не подменять views, opens, reads."]},
        "editorial_context": {"channel_topic": store.settings()["channel_topic"],
            "recent_publications": store.rows("SELECT title,topic,status FROM articles WHERE status='PUBLISHED' "
                                               "ORDER BY updated_at DESC,id DESC LIMIT 20")},
        "previous_analytics": previous_result, "experiment_history": None,
        "analysis_config": {"measurement_window": metadata["measurement_window"],
            "comparison_available": comparison_available, "previous_import_id": previous["id"] if previous else None,
            "previous_measurement_window": previous_meta.get("measurement_window", "unknown"),
            "original_row_count": len(raw_rows), "excluded_unpublished": audit["data_quality"]["unpublished_rows"],
            "excluded_aggregates": audit["data_quality"]["aggregate_rows"],
            "population_article_count": len(population), "example_article_count": len(articles),
            "processed_all_publications": True, "preprocessing_version": "2.0",
            "population_sha256": audit["population_sha256"],
            "sampled": sampled, "sampling_method": "metric_extremes_and_diversity" if sampled else "all_eligible_rows",
            "output_requirements": {"articles_total": len(population), "period": metadata["period"],
                "note": "Все публикации обработаны Python. articles — только примеры; анализируйте полную сводку. "
                        "Не заменяйте null в period датой анализа. Все обязательные разделы JSON должны присутствовать."},
            "maturity_threshold": None, "kpi_priorities": None, "validated_funnel": totals,
            "limitations": ([] if comparison_available else ["Временное сравнение не подтверждено; trend должен быть unknown."])
                + ["Суммы lifetime, повторных замеров, смешанных и неизвестных окон не рассчитываются."]}}
    return context, audit


def build_analytics_input(store: Store, statistics_id: int, *, run_id=None) -> dict:
    return prepare_analytics_input(store, statistics_id, run_id=run_id)[0]
