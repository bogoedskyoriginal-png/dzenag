from __future__ import annotations

import argparse

from dzen_agent.config import get_settings
from dzen_agent.pipeline.orchestrator import Pipeline, create_run
from dzen_agent.prompt_service import Prompts
from dzen_agent.provider import AITunnel
from dzen_agent.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description="Подготовка недели Dzen Agent")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--week", help="Понедельник недели, YYYY-MM-DD")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--analytics-only", action="store_true", help="Остановиться после Analytics, не запускать Discovery")
    args = parser.parse_args()
    config = get_settings()
    store = Store(config.database_path)
    store.initialize()
    Prompts(store).seed()
    if args.scheduled and (not store.settings()["scheduler_enabled"]
                           or store.settings()["stop_requested"]):
        print("Запуск по расписанию выключен.")
        return 0
    if args.scheduled and store.settings().get("scheduled_autonomous_policy"):
        from datetime import datetime
        from dzen_agent.store import MSK
        from dzen_agent.autonomy import start_run
        policy = store.settings()["scheduled_autonomous_policy"]
        calendar = {d:n for d,n in store.settings().get("generation_calendar", {}).items() if d > datetime.now(MSK).date().isoformat()}
        if not calendar:
            print("В календаре нет будущих дат. Расходов не было.")
            return 0
        earliest = min(calendar)
        from datetime import timedelta
        first = datetime.fromisoformat(earliest).date()
        week = (first - timedelta(days=first.weekday())).isoformat()
        existing = store.one("SELECT id,status FROM pipeline_runs WHERE week_start=?", (week,))
        if existing:
            if existing["status"] in ("PREPARED", "SUCCEEDED", "PARTIAL_READY"):
                return 0
            run_id = existing["id"]
        else:
            run_id = start_run(store, calendar, store.settings().get("editorial_wishes", ""), policy["budget_rub"], confirmed=True, drafts=policy.get("drafts", False))
    else:
        run_id = args.run_id or create_run(store, args.week)
    from dzen_agent.browser.publisher import DzenPublisher
    from dzen_agent.browser.scheduling import DzenScheduling
    adapter = DzenPublisher(store, config)
    if args.send:
        # A send click must never enter a paid generation stage, even after an error.
        assert_publish_ready(store, run_id)
        provider = PublishingOnlyProvider()
    else:
        provider = AITunnel(store, config.aitunnel_api_key)
    result = Pipeline(store, provider, dzen=DzenScheduling(store, adapter) if adapter.verified else None).execute(
        run_id, allow_dzen=args.send, analytics_only=args.analytics_only)
    print(f"Pipeline {run_id}: {result}")
    return 1 if result in ("NEEDS_ACTION", "STOPPED") else 0


class PublishingOnlyProvider:
    def __getattr__(self, name):
        raise RuntimeError("Отправка в Дзен не может выполнять AI-запросы.")


def assert_publish_ready(store, run_id):
    from dzen_agent.errors import UserActionRequired
    from dzen_agent.pipeline.orchestrator import stages_for
    import json
    run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    if not run or not run["approved_at"]:
        raise UserActionRequired("Сначала утвердите готовые материалы недели.")
    stages = stages_for(json.loads(run["settings_json"]))
    required = stages[:stages.index("REVIEW")]
    actual = {r["stage"]: r["status"] for r in store.rows("SELECT * FROM pipeline_stages WHERE run_id=?", (run_id,))}
    if any(actual.get(stage) != "SUCCEEDED" for stage in required):
        raise UserActionRequired("Подготовка недели не завершена. Отправка не запускает платные этапы.")


if __name__ == "__main__":
    raise SystemExit(main())
