import copy
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from dzen_agent.ai_response import require_complete_response
from dzen_agent.analytics_contract import Analytics
from dzen_agent.analytics_input import prepare_analytics_input
from dzen_agent.analytics_statistics import prepare_publications
from dzen_agent.errors import UserActionRequired
from dzen_agent.pipeline.orchestrator import Pipeline, create_run
from dzen_agent.prompt_service import Prompts
from dzen_agent.provider import AITunnel
from dzen_agent.recovery import authorize_retry
from dzen_agent.services import ContentService
from dzen_agent.statistics import detect_mapping, import_csv, preview_csv
from dzen_agent.store import Store, dumps


def population(rows, **metadata):
    return prepare_publications(rows, 7, {"measurement_window": "unknown", **metadata}, {})


def test_streamlit_script_directory_does_not_shadow_statistics_dependency():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'dzen_agent'); "
        "from dzen_agent.analytics_statistics import prepare_publications; "
        "assert prepare_publications([{'views': 3}, {'views': 5}], 1, {'measurement_window':'unknown'}, {})"
        "[1]['metric_statistics']['views']['median'] == 4"], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_all_299_rows_extremes_distributions_and_source_preserved():
    rows = [{"date": "Всего", "title": "", "impressions": 999999999}]
    rows += [{"article_id": f"a{i}", "title": f"Article {i}", "date": "2026-01-01",
              "impressions": i * 100, "opens": 300 - i, "reads": i % 17,
              "Время просмотра в минутах": i / 2} for i in range(298)]
    before = copy.deepcopy(rows)
    examples, summary, audit = population(rows)
    assert rows == before
    assert summary["processed_all_rows"]
    assert summary["data_quality"]["unique_articles"] == 298
    assert summary["data_quality"]["aggregate_rows"] == 1
    assert len(examples) < 40
    assert len(audit["rankings"]["impressions"]) == 298
    ids = {a["article_id"] for a in examples}
    for name, metric in summary["metric_statistics"].items():
        assert set(metric["top_ids"] + metric["bottom_ids"]) <= ids, name
    assert summary["metric_statistics"]["impressions"]["sum_of_export_rows"] == sum(i * 100 for i in range(298))
    assert summary["metric_statistics"]["impressions"]["median"] == 14850
    assert summary["metric_statistics"]["Время просмотра в минутах"]["max"] == 148.5
    assert "computed" not in examples[0]  # Unknown scope cannot produce CTR.
    assert summary["data_quality"]["missing_metric_counts"]["completions"] == 298


def test_total_named_article_with_url_retained_and_identical_anonymous_duplicates_detected():
    rows = [{"title": "Всего", "url": "https://example.com/total", "views": 20},
            {"title": "Article", "date": "2026-01-01", "views": 10}]
    rows.append(dict(rows[-1]))
    _, summary, audit = population(rows)
    assert len(audit["articles"]) == 2
    assert summary["data_quality"]["aggregate_rows"] == 0
    assert summary["data_quality"]["duplicate_rows"] == 1
    assert audit["articles"][1]["article_id"] == "csv:7:row:2"
    assert summary["metric_statistics"]["views"]["sum_of_export_rows"] is None


def test_zero_article_id_is_preserved():
    examples, summary, _ = population([{"article_id": 0, "title": "Всего", "views": 1}])
    assert examples[0]["article_id"] == "0"
    assert summary["data_quality"]["aggregate_rows"] == 0


def test_conflicting_snapshots_missing_zero_negative_and_outliers():
    rows = [{"article_id": "a", "views": 10}, {"article_id": "a", "views": 20},
            {"article_id": "b", "views": 0, "opens": -1}, {"article_id": "c", "views": None}]
    rows += [{"article_id": str(i), "views": 1} for i in range(10)]
    rows.append({"article_id": "outlier", "views": 1000000})
    _, summary, audit = population(rows)
    assert audit["articles"][0]["metrics"]["views"] is None
    assert audit["conflicts"] == [{"article_id": "a", "metric": "views"}]
    metric = summary["metric_statistics"]["views"]
    assert metric["missing"] == 2 and metric["zeros"] == 1
    assert metric["high_outlier_count"] == 1
    assert summary["data_quality"]["invalid_metric_counts"] == {"opens": 1}
    assert "outlier" in [a["article_id"] for a in audit["articles"]]


def test_metric_headers_keep_distinct_meanings_and_relationships_need_definitions():
    assert detect_mapping(["Показы", "Открытия", "Просмотры", "Дочитывания и просмотры"]) == {
        "Показы": "impressions", "Открытия": "opens", "Просмотры": "views", "Дочитывания и просмотры": "reads"}
    rows = [{"article_id": "a", "impressions": 10, "opens": 20, "reads": 7, "views": 8},
            {"article_id": "b", "impressions": 100, "opens": 10}]
    examples, summary, _ = population(rows)
    quality = summary["data_quality"]
    assert quality["invalid_metric_counts"] == {}
    assert quality["relationship_review"]["classification"] == "definitions_unconfirmed"
    assert examples[0]["metrics"]["reads"] == 7 and examples[0]["metrics"]["views"] == 8
    examples, _, _ = population(rows, metric_definitions={"ctr_compatible": True})
    assert examples[0]["computed"]["ctr"] is None
    assert examples[1]["computed"]["ctr"] == .1


@pytest.fixture
def imported(tmp_path):
    store = Store(tmp_path / "test.db")
    store.initialize()
    Prompts(store).seed()
    store.set_setting("analytics_model", "deepseek-v4-flash-0731")
    key = import_csv(store, "stats_2026-01-01_2026-02-01.csv",
                     preview_csv(b"article_id;title;views\na;A;300\nb;B;200"))
    return store, key


def test_population_count_and_unknown_period_validated_against_summary(imported, analytics_v1):
    store, key = imported
    rows = [{"article_id": f"id{i}", "title": f"Article {i}", "views": i} for i in range(298)]
    store.execute("UPDATE statistics_imports SET rows_json=? WHERE id=?", (dumps(rows), key))
    context, _ = prepare_analytics_input(store, key)
    assert context["run_context"]["analysis_at"]
    assert context["run_context"]["period"] == {"start": None, "end": None, "as_of": None, "timezone": "Europe/Moscow"}
    analytics_v1["data_quality"].update(articles_total=298, articles_usable=298)
    analytics_v1["observations"][0]["article_ids"] = [context["articles"][0]["article_id"]]
    Analytics.model_validate(analytics_v1).validate_input(context)
    analytics_v1["data_quality"].update(articles_total=len(context["articles"]), articles_usable=len(context["articles"]))
    with pytest.raises(ValueError, match="количество публикаций"):
        Analytics.model_validate(analytics_v1).validate_input(context)
    analytics_v1["data_quality"].update(articles_total=298, articles_usable=298)
    analytics_v1["period"]["as_of"] = context["run_context"]["analysis_at"]
    with pytest.raises(ValueError, match="период"):
        Analytics.model_validate(analytics_v1).validate_input(context)


@pytest.mark.parametrize("content", ['{"broken":', '{}'])
@pytest.mark.parametrize("reason_field", ["finish_reason", "native_finish_reason"])
def test_length_diagnosed_before_json_and_even_for_parseable_json(content, reason_field):
    response = {"choices": [{reason_field: "length", "message": {"content": content}}],
                "usage": {"completion_tokens": 16000, "completion_tokens_details": {"reasoning_tokens": 11902}}}
    with pytest.raises(UserActionRequired, match="обрезан по лимиту токенов.*length") as caught:
        require_complete_response(response)
    assert "11902" in str(caught.value)


def provider_for(store, reply, captures):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 500})
        captures.append(json.loads(request.content))
        return httpx.Response(200, json=reply)
    return AITunnel(store, "mock-key", httpx.Client(transport=httpx.MockTransport(handler)))


def test_truncated_paid_response_never_recharged_after_config_change(imported):
    store, key = imported
    captures = []
    api = provider_for(store, {"choices": [{"finish_reason": "length", "message": {"content": '{"partial":'}}],
                              "usage": {"cost_rub": 1.19, "completion_tokens": 16000}}, captures)
    for limit in (16000, 32000):
        settings = {**store.settings(), "analytics_max_output_tokens": limit}
        with pytest.raises(UserActionRequired, match="обрезан"):
            ContentService(store, api, settings=settings).analyze(key, "run:1:analytics")
    assert len(captures) == 1
    assert store.costs()["total"] == 1.19
    assert not store.rows("SELECT * FROM analytics_results")
    assert store.one("SELECT status FROM ai_calls")["status"] == "SUCCEEDED"


def test_missing_sections_report_paths_and_never_fill_or_retry(imported, analytics_v1):
    store, key = imported
    for field in ("discovery_brief", "planner_brief", "missing_data_requests"):
        del analytics_v1[field]
    captures = []
    api = provider_for(store, {"choices": [{"message": {"content": dumps(analytics_v1)}}],
                              "usage": {"cost_rub": .5}}, captures)
    for _ in range(2):
        with pytest.raises(UserActionRequired) as caught:
            ContentService(store, api).analyze(key, "invalid-json")
        for field in ("discovery_brief", "planner_brief", "missing_data_requests"):
            assert field in str(caught.value)
    assert len(captures) == 1 and store.costs()["total"] == .5


def test_only_analytics_stops_before_discovery_preserves_old_run_and_receipts(imported, analytics_v1, monkeypatch):
    store, key = imported
    # Existing failed run has an old global token limit and no new Analytics-specific settings.
    run_id = create_run(store, "2030-01-07")
    run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    settings = json.loads(run["settings_json"])
    settings.pop("analytics_max_output_tokens")
    settings.pop("analytics_reasoning_effort")
    settings["max_output_tokens"] = 16000
    store.execute("UPDATE pipeline_runs SET statistics_id=?,settings_json=? WHERE id=?", (key, dumps(settings), run_id))
    before = store.one("SELECT settings_json,prompts_json,statistics_id FROM pipeline_runs WHERE id=?", (run_id,))
    old = {"choices": [{"finish_reason": "length", "message": {"content": '{"old":'}}], "usage": {"cost_rub": 1.19}}
    captures = []
    api = provider_for(store, old, captures)
    pipeline = Pipeline(store, api)
    def forbidden_downstream_preflight(*args):
        raise AssertionError("Analytics-only must not require future stages to be ready")
    monkeypatch.setattr("dzen_agent.editorial_pipeline.preflight", forbidden_downstream_preflight)
    assert pipeline.execute(run_id, analytics_only=True) == "NEEDS_ACTION"
    receipt = store.one("SELECT * FROM ai_calls")
    with pytest.raises(UserActionRequired):
        authorize_retry(store, receipt["call_key"], confirmed=False, analytics_run_id=run_id)
    authorize_retry(store, receipt["call_key"], confirmed=True, analytics_run_id=run_id)
    archived = store.one("SELECT * FROM ai_calls")
    assert archived["result_json"] == receipt["result_json"]
    assert ":archived:" in archived["call_key"]
    api = provider_for(store, {"choices": [{"finish_reason": "stop", "message": {"content": dumps(analytics_v1)}}],
                              "usage": {"cost_rub": .2}}, captures)
    pipeline = Pipeline(store, api)
    assert pipeline.execute(run_id, analytics_only=True) == "ANALYTICS_READY"
    assert pipeline.execute(run_id, analytics_only=True) == "ANALYTICS_READY"
    assert len(captures) == 2  # One explicitly authorized retry, never a Discovery request.
    assert captures[-1]["max_tokens"] == 32000
    assert captures[-1]["reasoning"] == {"effort": "low"}
    assert "tools" not in captures[-1]
    assert store.one("SELECT settings_json,prompts_json,statistics_id FROM pipeline_runs WHERE id=?", (run_id,)) == before
    assert store.one("SELECT status,attempts FROM pipeline_stages WHERE run_id=? AND stage='DISCOVERY'", (run_id,)) == {
        "status": "PENDING", "attempts": 0}
    assert store.one("SELECT attempts FROM pipeline_stages WHERE run_id=? AND stage='COLLECT_STATS'", (run_id,))["attempts"] == 1
    assert len(store.rows("SELECT * FROM analytics_results")) == 1
    assert store.costs()["total"] == pytest.approx(1.39)
    receipt_new = store.one("SELECT * FROM ai_calls WHERE call_key=?", (receipt["call_key"],))
    request = json.loads(receipt_new["result_json"])["analytics_request"]
    assert request == captures[-1]
    assert "mock-key" not in receipt_new["result_json"]
    with pytest.raises(ValueError, match="неудавшегося"):
        authorize_retry(store, receipt["call_key"], confirmed=True, analytics_run_id=run_id)


@pytest.mark.parametrize("model,limit,reasoning", [
    ("deepseek-v4-flash-0731", 32000, True), ("deepseek/deepseek-v4-flash-0731", 12000, True),
    ("other-model", 32000, False)])
def test_supported_model_parameters_and_catalog_limits(imported, analytics_v1, model, limit, reasoning):
    store, key = imported
    settings = {**store.settings(), "analytics_model": model,
                "public_catalog": {"chat": {model: {"max_output": limit}}}}
    captures = []
    api = provider_for(store, {"choices": [{"message": {"content": dumps(analytics_v1)}}]}, captures)
    ContentService(store, api, settings=settings).analyze(key, "model-check")
    assert captures[0]["max_tokens"] == limit
    assert ("reasoning" in captures[0]) is reasoning
    assert "max_tokens" not in captures[0].get("reasoning", {})
