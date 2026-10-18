"""Deterministic analysis of every imported publication; only editorial interpretation uses AI."""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict

from dzen_agent.store import dumps

COUNTERS = ("impressions", "opens", "views", "completions", "subscriptions", "unsubscribes", "likes", "comments")
METRICS = COUNTERS + ("read_time_seconds", "reads", "subscribers", "ctr")
UNPUBLISHED = {"DRAFT", "RESEARCHED", "WRITTEN", "IMAGE_READY", "READY_FOR_REVIEW", "APPROVED",
               "PREPARING_IN_DZEN", "SCHEDULED", "SCHEDULED_VERIFIED", "ЧЕРНОВИК", "ОТЛОЖЕНО"}
TOTAL_LABELS = {"всего", "итого", "итог", "total", "grand total", "всего за период"}


def source_identity(row):
    for key in ("article_id", "url"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def aggregate_row(row):
    # Require an explicit total marker plus no publication ID/URL: never discard an article named "Всего".
    return source_identity(row) is None and any(
        str(row.get(key) or "").strip().casefold().rstrip(":") in TOTAL_LABELS
        for key in ("title", "date", "published_at", "Тип публикации"))


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def percentile(values, fraction):
    position = (len(values) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def prepare_publications(raw_rows, statistics_id, metadata, columns):
    grouped, aggregate, unpublished = defaultdict(list), [], 0
    anonymous = {}
    invalid = defaultdict(list)
    for index, row in enumerate(raw_rows, 1):
        if aggregate_row(row):
            aggregate.append({"source_row": index, "metrics": {k: v for k, v in row.items() if numeric(v)}})
            continue
        if str(row.get("status", "")).upper() in UNPUBLISHED:
            unpublished += 1
            continue
        identity = source_identity(row)
        if not identity:
            fingerprint = dumps(row)
            identity = anonymous.setdefault(fingerprint, f"csv:{statistics_id}:row:{index}")
        identity = str(identity)
        grouped[identity].append((index, row))
    articles, conflicts, duplicate_ids = [], [], []
    for identity, records in grouped.items():
        first = records[0][1]
        if len(records) > 1:
            duplicate_ids.append(identity)
        metrics = {}
        extra = {key for _, row in records for key, value in row.items() if isinstance(value, (int, float))
                 and not isinstance(value, bool)
                 and key not in {"article_id", "length_chars"}}
        for key in dict.fromkeys((*METRICS, *sorted(extra))):
            values = [row.get(key) for _, row in records]
            for (index, _), value in zip(records, values, strict=True):
                if value not in (None, "", "-", "—") and not numeric(value):
                    invalid[key].append({"article_id": identity, "source_row": index})
            valid = [v for v in values if numeric(v)]
            distinct = set(valid)
            if len(distinct) > 1:
                conflicts.append({"article_id": identity, "metric": key})
            # Different snapshots cannot be summed or arbitrarily called the latest.
            metrics[key] = valid[0] if len(distinct) == 1 and len(valid) == len(values) else None
            if key in COUNTERS and metrics[key] is not None and float(metrics[key]).is_integer():
                metrics[key] = int(metrics[key])
        articles.append({"article_id": identity,
            "id_source": "csv_or_url" if source_identity(first) is not None else "local_row_id",
            "title": str(first.get("title") or "")[:500], "published_at": first.get("date", first.get("published_at")),
            "status": first.get("status") or "unknown", "topic": str(first.get("topic") or "")[:300],
            **{k: first[k] for k in ("cluster", "format", "headline_type", "cover_type", "length_chars") if k in first},
            "publication_type": first.get("publication_type") or first.get("Тип публикации"),
            "measurement_window": first.get("measurement_window") or metadata["measurement_window"], "metrics": metrics})
    definitions = metadata.get("metric_definitions", {})
    # Names alone (in any language) do not prove a common traffic scope or denominator.
    ctr_compatible = definitions.get("ctr_compatible") is True
    relation_ids = [a["article_id"] for a in articles if numeric(a["metrics"].get("opens"))
        and numeric(a["metrics"].get("impressions")) and a["metrics"]["opens"] > a["metrics"]["impressions"]]
    if ctr_compatible:
        for article in articles:
            n, d = article["metrics"].get("opens"), article["metrics"].get("impressions")
            article["computed"] = {"ctr": n / d if numeric(n) and numeric(d) and d > 0 and n <= d else None}
    rankings, statistics, selected = {}, {}, set()
    metric_names = list(dict.fromkeys((*METRICS, *(k for a in articles for k in a["metrics"]))))
    by_id = {a["article_id"]: a for a in articles}
    for metric in metric_names:
        ranked = sorted([a for a in articles if numeric(a["metrics"].get(metric))],
                        key=lambda a: (-a["metrics"][metric], str(a["article_id"])))
        if not ranked:
            continue
        values = sorted(a["metrics"][metric] for a in ranked)
        top, bottom = ranked[:2], list(reversed(ranked[-2:]))
        # Every numeric source column retains both extremes without inventing its units.
        selected.update(a["article_id"] for a in top + bottom)
        rankings[metric] = [{"article_id": a["article_id"], "title": a["title"], "value": a["metrics"][metric]}
                            for a in ranked]
        statistics[metric] = {"available": len(values), "missing": len(articles) - len(values),
            "zeros": values.count(0), "min": values[0], "p25": percentile(values, .25),
            "median": percentile(values, .5), "p75": percentile(values, .75), "max": values[-1],
            "mean": round(sum(values) / len(values), 6), "sum_of_export_rows": sum(values) if not duplicate_ids else None}
        iqr = percentile(values, .75) - percentile(values, .25)
        statistics[metric].update(top_ids=[a["article_id"] for a in top], bottom_ids=[a["article_id"] for a in bottom],
            high_outlier_count=sum(v > percentile(values, .75) + 1.5 * iqr for v in values))
    if ctr_compatible:
        ranked = sorted([a for a in articles if a.get("computed", {}).get("ctr") is not None],
                        key=lambda a: (-a["computed"]["ctr"], str(a["article_id"])))
        selected.update(a["article_id"] for a in ranked[:2] + ranked[-2:])
        rankings["computed_ctr"] = [{"article_id": a["article_id"], "title": a["title"], "value": a["computed"]["ctr"]}
                                    for a in ranked]
    # Uniform middle examples + explicit category representatives supplement, never replace, metric extremes.
    for i in range(min(8, len(articles))):
        selected.add(articles[i * len(articles) // min(8, len(articles))]["article_id"])
    categories = defaultdict(list)
    for article in articles:
        for field in ("publication_type", "cluster", "format"):
            if article.get(field):
                categories[f"{field}:{article[field]}"].append(article["article_id"])
    for ids in list(categories.values())[:8]:
        selected.add(ids[0])
    selected.update(relation_ids[:3])
    examples = [a for a in articles if a["article_id"] in selected]
    quality = {"source_rows": len(raw_rows), "aggregate_rows": len(aggregate), "unpublished_rows": unpublished,
        "publication_rows": sum(len(v) for v in grouped.values()), "unique_articles": len(articles),
        "duplicate_rows": sum(len(v) - 1 for v in grouped.values()), "duplicate_id_count": len(duplicate_ids),
        "duplicate_examples": duplicate_ids[:4], "conflicting_metric_count": len(conflicts),
        "invalid_metric_counts": {k: len(v) for k, v in invalid.items()},
        "missing_metric_counts": {k: count for k in METRICS
            if (count := sum(not numeric(a["metrics"].get(k)) for a in articles))},
        "missing_titles": sum(not a["title"] for a in articles),
        "missing_publication_dates": sum(not a["published_at"] for a in articles),
        "usable_articles": sum(any(numeric(v) for v in a["metrics"].values()) for a in articles),
        "relationship_review": {"opens_gt_impressions": len(relation_ids), "example_ids": relation_ids[:3],
            "classification": "scope_conflict" if ctr_compatible else "definitions_unconfirmed",
            "note": "Не объявлять ошибкой до проверки источников трафика, периода и определений счётчиков."}}
    summary = {"scope": "all_imported_publications", "processed_all_rows": True, "data_quality": quality,
        "metric_statistics": statistics, "category_counts": {k: len(v) for k, v in categories.items()},
        "outlier_note": "IQR-флаг — повод проверить значение, не ошибка и не основание исключать публикацию.",
        "reported_aggregates": aggregate[:3],
        "aggregation_note": "sum_of_export_rows — описательная сумма строк, не воронка/динамика канала; "
                            "разные lifetime-снимки не складываются. Сводные строки не прибавляются к публикациям."}
    audit = {"articles": articles, "rankings": rankings, "aggregate_rows": aggregate, "conflicts": conflicts,
             "invalid_values": dict(invalid), "data_quality": quality, "ctr_compatible": ctr_compatible}
    assert set(selected).issubset(by_id)
    # Stable local fingerprint binds interpretation to the complete population, not only its examples.
    audit["population_sha256"] = hashlib.sha256(dumps(articles).encode()).hexdigest()
    return examples, summary, audit
