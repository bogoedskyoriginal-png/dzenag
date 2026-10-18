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
    run_id = args.run_id or create_run(store, args.week)
    result = Pipeline(store, AITunnel(store, config.aitunnel_api_key)).execute(
        run_id, allow_dzen=args.send, analytics_only=args.analytics_only)
    print(f"Pipeline {run_id}: {result}")
    return 1 if result in ("NEEDS_ACTION", "STOPPED") else 0


if __name__ == "__main__":
    raise SystemExit(main())
