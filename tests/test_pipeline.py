from datetime import datetime

from dzen_agent.pipeline.orchestrator import Pipeline, create_run
from dzen_agent.prompt_service import Prompts
from dzen_agent.plan_review import approve_plan
from dzen_agent.scheduler import next_run
from dzen_agent.store import MSK, Store


def test_missing_statistics_records_failure_and_can_resume(tmp_path):
    store = Store(tmp_path / "test.db")
    store.initialize()
    Prompts(store).seed()
    run_id = create_run(store, "2030-01-07")
    assert create_run(store, "2030-01-07") == run_id
    assert Pipeline(store, None).execute(run_id) == "NEEDS_ACTION"
    stage = store.one("SELECT * FROM pipeline_stages WHERE run_id=? AND stage='COLLECT_STATS'", (run_id,))
    assert stage["status"] == "ERROR"
    assert stage["ended_at"]
    assert "CSV" in stage["error"]


def test_completed_stages_not_repeated_after_failure(editorial_plan):
    store, run_id, plan_id, _ = editorial_plan
    calls = []

    class ControlledPipeline(Pipeline):
        def _stage(self, stage, run, service):
            calls.append(stage)
            if stage == "IMAGE" and calls.count("IMAGE") == 1:
                raise ValueError("temporary failure after writing")

    pipeline = ControlledPipeline(store, None)
    assert pipeline.execute(run_id) == "AWAITING_PLAN_REVIEW"
    approve_plan(store, plan_id)
    assert pipeline.execute(run_id) == "NEEDS_ACTION"
    assert pipeline.execute(run_id) == "AWAITING_REVIEW"
    assert calls.count("WRITE") == 1
    assert calls.count("IMAGE") == 2
    assert pipeline.execute(run_id) == "AWAITING_REVIEW"
    assert calls.count("IMAGE") == 2


def test_kill_switch_blocks_pipeline(tmp_path):
    store = Store(tmp_path / "test.db")
    store.initialize()
    run_id = create_run(store, "2030-01-07")
    store.set_setting("stop_requested", True)
    assert Pipeline(store, None).execute(run_id) == "STOPPED"


def test_schedule_is_moscow_and_disabled_is_none():
    settings = {"scheduler_enabled": True, "stop_requested": False,
                "scheduler_day": 6, "scheduler_time": "18:00"}
    now = datetime(2026, 10, 17, 20, 0, tzinfo=MSK)
    assert next_run(settings, now) == "2026-10-18T18:00:00+03:00"
    settings["stop_requested"] = True
    assert next_run(settings, now) is None
