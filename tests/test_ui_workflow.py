import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from dzen_agent.config import PROJECT_ROOT, get_settings
from dzen_agent.pipeline.orchestrator import create_run
from dzen_agent.prompt_service import Prompts
from dzen_agent.store import Store, dumps
from dzen_agent.ui_workflow import current_stage, error_explanation, failed_receipt, readiness


def configured_store(tmp_path):
    store = Store(tmp_path / "workflow.db")
    store.initialize()
    Prompts(store).seed()
    return store


@pytest.mark.parametrize("count", [1, 21])
def test_week_wizard_snapshot_keeps_defaults_old_runs_and_prompts(tmp_path, count):
    store = configured_store(tmp_path)
    old = create_run(store, "2030-01-07")
    before_run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (old,))
    before_settings = store.settings()
    before_prompts = store.rows("SELECT * FROM prompt_versions")
    overrides = {"max_articles_per_week": count, "publication_days": list(range(7)), "publication_time": "05:00",
                 "publication_times": ["05:00", "06:00", "16:00"], "max_articles_per_day": 3, "publication_slots": {}}
    run = create_run(store, "2030-01-14", settings_override=overrides)
    settings = json.loads(store.one("SELECT settings_json FROM pipeline_runs WHERE id=?", (run,))["settings_json"])
    assert all(settings[k] == v for k, v in overrides.items())
    assert store.settings() == before_settings
    assert store.rows("SELECT * FROM prompt_versions") == before_prompts
    assert store.one("SELECT * FROM pipeline_runs WHERE id=?", (old,)) == before_run
    assert create_run(store, "2030-01-14", settings_override=overrides) == run


def test_wizard_refuses_model_changes_in_schedule_override(tmp_path):
    store = configured_store(tmp_path)
    with pytest.raises(ValueError, match="только число"):
        create_run(store, "2030-01-07", settings_override={"analytics_model": "other"})
    assert not store.rows("SELECT * FROM pipeline_runs")


def test_readiness_does_not_contact_ai_and_reports_missing_data_and_schedule(tmp_path):
    store = configured_store(tmp_path)
    config = replace(get_settings(), database_path=store.database_path, aitunnel_api_key=None)
    settings = {**store.settings(), "max_articles_per_week": 21}
    with patch("dzen_agent.provider.AITunnel._request", side_effect=AssertionError("No network")):
        problems, _ = readiness(store, config, settings, "2030-01-07")
    assert any("статистику" in v for v in problems)
    assert any("ключ" in v for v in problems)
    assert any("Discovery" in v for v in problems)
    assert not store.rows("SELECT * FROM ai_calls")


def test_existing_discovery_permission_never_selects_paid_analytics_for_retry(tmp_path):
    store = configured_store(tmp_path)
    run_id = create_run(store, "2030-01-07")
    store.execute("UPDATE pipeline_stages SET status='SUCCEEDED' WHERE run_id=? AND stage IN ('COLLECT_STATS','ANALYZE')", (run_id,))
    store.execute("UPDATE pipeline_stages SET status='ERROR' WHERE run_id=? AND stage='DISCOVERY'", (run_id,))
    store.execute("INSERT INTO ai_calls(call_key,status,created_at,updated_at) VALUES('run:1:analytics','SUCCEEDED','2030','2030')")
    store.execute("INSERT INTO ai_calls(call_key,status,created_at,updated_at) VALUES('run:1:discovery:archived:old','ERROR','2030','2030')")
    run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    assert current_stage(store, run)["stage"] == "DISCOVERY"
    assert failed_receipt(store, run, "DISCOVERY") is None


def test_unknown_planner_result_has_clear_reason_and_no_automatic_resume(tmp_path):
    store = configured_store(tmp_path)
    run = create_run(store, "2030-01-07")
    store.execute("UPDATE pipeline_stages SET status='SUCCEEDED' WHERE run_id=? AND stage IN ('COLLECT_STATS','ANALYZE','DISCOVERY')", (run,))
    store.execute("UPDATE pipeline_stages SET status='ERROR',error='Результат запроса неизвестен' WHERE run_id=? AND stage='PLAN'", (run,))
    store.execute("UPDATE pipeline_runs SET status='NEEDS_ACTION',error='Результат запроса неизвестен' WHERE id=?", (run,))
    store.execute("INSERT INTO ai_calls(call_key,status,created_at,updated_at) VALUES('run:1:plan','UNCERTAIN','2030','2030')")
    config = replace(get_settings(), database_path=store.database_path, aitunnel_api_key="mock-key")
    with patch("dzen_agent.config.get_settings", return_value=config), patch("dzen_agent.scheduler.launch_pipeline") as launch:
        app = AppTest.from_file(str(PROJECT_ROOT / "dzen_agent/ui.py"), default_timeout=20).run()
        next(x for x in app.button if x.label == "Разобраться с ошибкой").click().run()
        assert not app.exception
        assert any("Ответ AI-сервиса не подтверждён" in x.value for x in app.markdown)
        assert next(x for x in app.button if x.label == "Повторить этап и продолжить (платно)").disabled
        launch.assert_not_called()


def test_error_copy_uses_finish_reason_in_saved_response():
    receipt = {"result_json": dumps({"choices": [{"finish_reason": "length"}]})}
    assert "лимита ответа" in error_explanation({"error": "старое сообщение"}, {"stage": "ANALYZE"}, receipt)
