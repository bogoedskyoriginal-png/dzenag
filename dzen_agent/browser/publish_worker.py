"""Send or update one approved article. This worker cannot request AI generation."""
from __future__ import annotations

import argparse

from dzen_agent.browser.publisher import DzenPublisher
from dzen_agent.browser.scheduling import DzenScheduling
from dzen_agent.config import get_settings
from dzen_agent.errors import UserActionRequired, log_exception, redact
from dzen_agent.locking import exclusive_lock
from dzen_agent.services import qa_article
from dzen_agent.store import Store, now_iso


def publish_article(store, article_id, *, update=False, adapter=None):
    with exclusive_lock(store.database_path.parent / "pipeline.lock"):
        article = store.get(article_id)
        if article.status != "APPROVED":
            raise UserActionRequired("Сначала утвердите актуальную статью.")
        check = qa_article(store, article_id)
        if not check["passed"]:
            raise UserActionRequired("QA: " + "; ".join(check["issues"]))
        dzen = DzenScheduling(store, adapter or DzenPublisher(store))
        if update:
            return dzen.update(article_id)
        return dzen.schedule(article_id, run_id=store.detail(article_id).get("run_id"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("article_id", type=int)
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    store = Store(get_settings().database_path)
    try:
        publish_article(store, args.article_id, update=args.update)
        return 0
    except Exception as error:
        log_exception(error, f"publication={args.article_id}")
        store.execute("UPDATE publications SET error=?,updated_at=? WHERE article_id=?",
                      (redact(str(error))[:2000], now_iso(), args.article_id))
        store.log("DZEN_SCHEDULE", str(error), {"article_id": args.article_id})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
