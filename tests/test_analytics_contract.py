import copy
import json

import httpx
import pytest
from pydantic import ValidationError

from dzen_agent.analytics_contract import Analytics, LegacyAnalytics, briefs, parse_analytics
from dzen_agent.analytics_input import build_analytics_input
from dzen_agent.errors import UserActionRequired
from dzen_agent.prompt_service import Prompts
from dzen_agent.provider import AITunnel
from dzen_agent.services import ContentService
from dzen_agent.statistics import import_csv, preview_csv
from dzen_agent.store import Store, dumps


@pytest.fixture
def imported(tmp_path):
    store = Store(tmp_path / "analytics.db")
    store.initialize()
    Prompts(store).seed()
    store.set_setting("analytics_model", "analytics-test")
    key = import_csv(store, "stats.csv", preview_csv(b"article_id;title;views\na;Animation;300\nb;Filming;200"))
    return store, key


def test_entire_contract_and_null_roundtrip(analytics_v1):
    result = Analytics.model_validate(analytics_v1)
    assert set(Analytics.model_fields) == set(analytics_v1)
    assert result.model_dump(mode="json") == analytics_v1
    assert result.funnel.ctr is None
    assert result.period.as_of is None
    assert result.observations[0].value == 300


@pytest.mark.parametrize("field", ["contract_version", "stage", "status", "period", "data_quality",
    "channel_diagnosis", "funnel", "observations", "interpretations", "cluster_signals", "experiments",
    "discovery_brief", "planner_brief", "missing_data_requests"])
def test_all_top_level_fields_are_required(analytics_v1, field):
    del analytics_v1[field]
    with pytest.raises(ValidationError):
        Analytics.model_validate(analytics_v1)


@pytest.mark.parametrize("section,key,value", [
    ("data_quality", "articles_total", -1), ("data_quality", "articles_total", "2"),
    ("data_quality", "articles_usable", 3), ("channel_diagnosis", "trend", "viral"),
    ("channel_diagnosis", "main_bottleneck", "income"), ("period", "timezone", "Bad/Timezone"),
    ("period", "start", "not-a-date"), ("funnel", "ctr", 12.5),
    ("funnel", "opens", float('nan')), ("planner_brief", "priority_experiment_ids", ["E404"]),
    ("discovery_brief", "made_up_field", []),
])
def test_types_enums_ranges_and_extra_fields(analytics_v1, section, key, value):
    analytics_v1[section][key] = value
    with pytest.raises(ValidationError):
        Analytics.model_validate(analytics_v1)


@pytest.mark.parametrize("section,field", [("interpretations", "observation_ids"),
    ("cluster_signals", "evidence_ids"), ("experiments", "evidence_ids")])
def test_dangling_observation_references_rejected(analytics_v1, section, field):
    analytics_v1[section][0][field] = ["O404"]
    with pytest.raises(ValidationError, match="наблюдение"):
        Analytics.model_validate(analytics_v1)


def test_unique_ids_and_sample_consistency(analytics_v1):
    analytics_v1["observations"].append(copy.deepcopy(analytics_v1["observations"][0]))
    with pytest.raises(ValidationError, match="Повторяются"):
        Analytics.model_validate(analytics_v1)


def test_weighted_ctr_and_zero_denominator(analytics_v1):
    analytics_v1["funnel"].update(impressions=1000, opens=125, completions=100, subscriptions=2,
                                ctr=0.125, completion_rate=0.8, completion_per_impression=0.1,
                                subscriptions_per_1000_opens=16)
    Analytics.model_validate(analytics_v1)
    analytics_v1["funnel"]["ctr"] = 0.5
    with pytest.raises(ValidationError, match="отношение"):
        Analytics.model_validate(analytics_v1)
    analytics_v1["funnel"].update(impressions=0, opens=0, completions=None, subscriptions=None,
                                ctr=None, completion_rate=None, completion_per_impression=None,
                                subscriptions_per_1000_opens=None)
    Analytics.model_validate(analytics_v1)
    analytics_v1["funnel"]["ctr"] = 0
    with pytest.raises(ValidationError):
        Analytics.model_validate(analytics_v1)


def test_input_missing_values_are_not_zero_or_opens(imported, analytics_v1):
    store, key = imported
    context = build_analytics_input(store, key)
    assert context["articles"][0]["metrics"]["views"] == 300
    assert context["articles"][0]["metrics"]["opens"] is None
    assert context["analysis_config"]["validated_funnel"]["ctr"] is None
    assert context["run_context"]["period"]["as_of"] is None
    assert not context["analysis_config"]["comparison_available"]
    Analytics.model_validate(analytics_v1).validate_input(context)
    analytics_v1["funnel"]["opens"] = 0
    with pytest.raises(ValueError, match="не подтверждена"):
        Analytics.model_validate(analytics_v1).validate_input(context)


@pytest.mark.parametrize("mutation", ["foreign_article", "invented_period", "invented_trend"])
def test_output_checked_against_actual_input(imported, analytics_v1, mutation):
    store, key = imported
    if mutation == "foreign_article":
        analytics_v1["observations"][0]["article_ids"] = ["foreign"]
    elif mutation == "invented_period":
        analytics_v1["period"]["start"] = "2026-10-01"
    else:
        analytics_v1["channel_diagnosis"]["trend"] = "growing"
    with pytest.raises(ValueError):
        Analytics.model_validate(analytics_v1).validate_input(build_analytics_input(store, key))


def test_same_window_totals_duplicates_and_unpublished_rows(imported):
    store, key = imported
    rows = [{"article_id": "a", "title": "A", "impressions": 100, "opens": 10, "completions": 5,
             "subscriptions": 1, "status": "PUBLISHED"},
            {"article_id": "b", "title": "B", "impressions": 900, "opens": 180, "completions": 100,
             "subscriptions": 2, "status": "PUBLISHED"},
            {"article_id": "draft", "title": "Draft", "impressions": 999999, "status": "DRAFT"}]
    metadata = {"measurement_window": "first_7d", "subscription_attribution_confirmed": True,
                "metric_definitions": {"ctr_compatible": True}}
    store.execute("UPDATE statistics_imports SET rows_json=?,metadata_json=? WHERE id=?", (dumps(rows), dumps(metadata), key))
    context = build_analytics_input(store, key)
    assert len(context["articles"]) == 2
    assert context["analysis_config"]["excluded_unpublished"] == 1
    assert context["analysis_config"]["validated_funnel"]["ctr"] == 0.19
    assert context["analysis_config"]["validated_funnel"]["subscriptions"] == 3
    rows[1]["article_id"] = "a"  # Two cumulative snapshots are not additive.
    store.execute("UPDATE statistics_imports SET rows_json=? WHERE id=?", (dumps(rows), key))
    assert build_analytics_input(store, key)["analysis_config"]["validated_funnel"]["impressions"] is None


def test_legacy_results_stay_legacy_and_unknown_versions_never_fall_back(editorial_plan, analytics_v1):
    store, _, _, _ = editorial_plan
    raw = store.one("SELECT payload_json FROM analytics_results")["payload_json"]
    old = json.loads(raw)
    assert isinstance(parse_analytics(old), LegacyAnalytics)
    discovery, planner = briefs(old)
    assert discovery["promising_directions"] == old["opportunities"] + old["best_topics"]
    assert planner["headline_insights"] == old["headline_patterns"]
    assert store.one("SELECT payload_json FROM analytics_results")["payload_json"] == raw
    analytics_v1["contract_version"] = "2.0"
    with pytest.raises(ValidationError):
        parse_analytics(analytics_v1)


def test_comparison_uses_matching_import_and_timezone(imported, analytics_v1):
    store, old_id = imported
    old_period = {"start": "2026-10-01", "end": "2026-10-07", "as_of": None, "timezone": "Europe/Moscow"}
    metadata = {"measurement_window": "calendar_period", "period": old_period}
    store.execute("UPDATE statistics_imports SET metadata_json=? WHERE id=?", (dumps(metadata), old_id))
    analytics_v1["period"] = old_period
    result_id = store.execute("INSERT INTO analytics_results(statistics_id,payload_json,created_at) VALUES(?,?,?)",
                             (old_id, dumps(analytics_v1), "2026-10-07"))
    new_id = import_csv(store, "new.csv", preview_csv(b"article_id;title;views\na;Animation;400\nb;Filming;250"))
    metadata["period"] = {"start": "2026-10-08T00:00:00+03:00", "end": "2026-10-14T00:00:00+03:00",
                          "as_of": None, "timezone": "Europe/Moscow"}
    store.execute("UPDATE statistics_imports SET metadata_json=? WHERE id=?", (dumps(metadata), new_id))
    assert build_analytics_input(store, new_id)["analysis_config"]["comparison_available"]
    store.execute("UPDATE analytics_results SET superseded=1 WHERE id=?", (result_id,))
    assert not build_analytics_input(store, new_id)["analysis_config"]["comparison_available"]
    store.execute("UPDATE analytics_results SET superseded=0 WHERE id=?", (result_id,))
    # An intervening import without analysis cannot borrow the older report's period.
    third_id = import_csv(store, "third.csv", preview_csv(b"article_id;title;views\na;Animation;500\nb;Filming;300"))
    context = build_analytics_input(store, third_id)
    assert context["previous_analytics"] is None
    assert not context["analysis_config"]["comparison_available"]


def test_active_prompt_kept_and_native_v1_input_used(imported, analytics_v1):
    store, key = imported
    prompt = "Авторский Analytics 1.0\nСоблюдай мой контракт."
    prompts = Prompts(store)
    prompts.save("ANALYTICS", "Авторская версия", prompt)
    active = prompts.active("ANALYTICS")
    captures = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 500})
        captures.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": dumps(analytics_v1)}}], "usage": {"cost_rub": 0.2}})
    service = ContentService(store, AITunnel(store, "test", httpx.Client(transport=httpx.MockTransport(handler))))
    result_id = service.analyze(key, "analytics-native")
    payload = captures[0]
    assert payload["messages"][0]["content"].startswith(prompt)
    assert "заменяет прежние требования" not in payload["messages"][0]["content"]
    assert "best_topics" not in payload["messages"][0]["content"]
    assert "tools" not in payload
    context = json.loads(payload["messages"][1]["content"])
    assert set(context) == {"run_context", "articles", "channel_metrics", "metric_definitions",
                            "editorial_context", "previous_analytics", "experiment_history", "analysis_config"}
    assert json.loads(store.one("SELECT payload_json FROM analytics_results WHERE id=?", (result_id,))["payload_json"]) == analytics_v1
    assert prompts.active("ANALYTICS") == active


def test_invalid_paid_result_is_not_written_or_paid_twice(imported, analytics_v1):
    store, key = imported
    analytics_v1["observations"][0]["article_ids"] = ["foreign"]
    calls = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 500})
        calls.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": dumps(analytics_v1)}}], "usage": {"cost_rub": 0.2}})
    service = ContentService(store, AITunnel(store, "test", httpx.Client(transport=httpx.MockTransport(handler))))
    for _ in range(2):
        with pytest.raises(UserActionRequired):
            service.analyze(key, "same-invalid")
    assert len(calls) == 1
    assert not store.rows("SELECT * FROM analytics_results")
    assert store.costs()["total"] == 0.2
