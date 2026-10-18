from collections import Counter

import pytest

from dzen_agent.browser.publisher import DzenPublisher
from dzen_agent.browser.scheduling import DzenScheduling, checkpoint
from dzen_agent.errors import UserActionRequired
from dzen_agent.models import ArticleStatus
from dzen_agent.pipeline.orchestrator import Pipeline, create_run
from dzen_agent.pipeline.run import PublishingOnlyProvider, assert_publish_ready
from dzen_agent.services import article_fingerprint, week_slots
from dzen_agent.store import Store, dumps, now_iso


class MockStudio:
    verified = True

    def __init__(self, store, fail_id=None):
        self.store, self.fail_id = store, fail_id
        self.created, self.resumed, self.checked = Counter(), Counter(), Counter()

    def evidence(self, article_id):
        article = self.store.get(article_id)
        return {"found_in_scheduled": True, "title": article.title,
                "scheduled_at": self.store.detail(article_id)["scheduled_at"],
                "content_hash": article_fingerprint(self.store, article_id),
                "url": f"https://dzen.ru/profile/editor/id/test/{article_id}/edit", "id": str(article_id)}

    def create_scheduled(self, article, detail, image):
        self.created[article.id] += 1
        evidence = self.evidence(article.id)
        checkpoint(self.store, article.id, "SAVING_SCHEDULE", url=evidence["url"], dzen_id=evidence["id"])
        if article.id == self.fail_id:
            raise UserActionRequired("Timeout after remote save; outcome unknown")
        return evidence

    def resume_scheduled(self, publication, article, detail, image):
        self.resumed[article.id] += 1
        assert publication["dzen_url"]
        # Reconciliation finds the already-created remote article, without another create.
        return self.evidence(article.id)

    def verify(self, publication):
        self.checked[publication["article_id"]] += 1
        return self.evidence(publication["article_id"])

    def update_scheduled(self, publication, article, image):
        return self.evidence(article.id)


def ready_week(tmp_path, monkeypatch, count):
    store = Store(tmp_path / "publishing.db")
    store.initialize()
    for key, value in {"pipeline_flow": "legacy", "max_articles_per_week": count,
                       "publication_days": list(range(7)), "publication_times": ["05:00", "06:00", "16:00"],
                       "max_articles_per_day": 3}.items():
        store.set_setting(key, value)
    run_id = create_run(store, "2030-01-07")
    store.execute("INSERT INTO content_plans(run_id,week_start,created_at,approved_at,approved_revision,revision) "
                  "VALUES(?,'2030-01-07',?,?,1,1)", (run_id, now_iso(), now_iso()))
    settings = store.settings()
    for index, slot in enumerate(week_slots("2030-01-07", settings)):
        article = store.create(f"Topic {index}", f"Title {index}", "Approved text")
        store.update_status(article.id, ArticleStatus.APPROVED)
        store.execute("INSERT INTO article_details(article_id,run_id,scheduled_at) VALUES(?,?,?)", (article.id, run_id, slot))
    store.execute("UPDATE pipeline_stages SET status='SUCCEEDED' WHERE run_id=? AND stage NOT IN ('REVIEW','DZEN_SCHEDULE','VERIFY')", (run_id,))
    store.execute("UPDATE pipeline_runs SET approved_at=? WHERE id=?", (now_iso(), run_id))
    monkeypatch.setattr("dzen_agent.pipeline.orchestrator.qa_article", lambda *a: {"passed": True})
    return store, run_id


@pytest.mark.parametrize("count", [1, 5, 21])
def test_review_schedules_approved_week_without_ai(tmp_path, monkeypatch, count):
    store, run_id = ready_week(tmp_path, monkeypatch, count)
    adapter = MockStudio(store)
    assert_publish_ready(store, run_id)
    pipeline = Pipeline(store, PublishingOnlyProvider(), dzen=DzenScheduling(store, adapter))
    assert pipeline.execute(run_id) == "SUCCEEDED"
    assert len(adapter.created) == count
    assert all(value == 1 for value in adapter.created.values())
    assert len(store.rows("SELECT * FROM publications WHERE status='SCHEDULED_VERIFIED'")) == count
    assert not store.rows("SELECT * FROM ai_calls")
    assert not store.rows("SELECT * FROM usage_events")
    assert pipeline.execute(run_id) == "SUCCEEDED"
    assert sum(adapter.created.values()) == count


def test_unknown_save_reconciles_without_recreating_successes(tmp_path, monkeypatch):
    store, run_id = ready_week(tmp_path, monkeypatch, 21)
    adapter = MockStudio(store, fail_id=13)
    pipeline = Pipeline(store, PublishingOnlyProvider(), dzen=DzenScheduling(store, adapter))
    assert pipeline.execute(run_id) == "NEEDS_ACTION"
    assert len(adapter.created) == 13
    assert store.one("SELECT * FROM publications WHERE article_id=13")["status"] == "SAVING_SCHEDULE"
    assert pipeline.execute(run_id) == "SUCCEEDED"
    assert adapter.resumed == {13: 1}
    assert len(adapter.created) == 21
    assert all(value == 1 for value in adapter.created.values())
    assert not store.rows("SELECT * FROM usage_events")


def test_manual_waits_for_send_and_stop_keeps_results(tmp_path, monkeypatch):
    store, run_id = ready_week(tmp_path, monkeypatch, 5)
    store.set_setting("mode", "MANUAL")
    adapter = MockStudio(store)
    pipeline = Pipeline(store, PublishingOnlyProvider(), dzen=DzenScheduling(store, adapter))
    assert pipeline.execute(run_id) == "AWAITING_SEND"
    assert not adapter.created
    store.set_setting("stop_requested", True)
    assert pipeline.execute(run_id, allow_dzen=True) == "STOPPED"
    assert not adapter.created
    store.set_setting("stop_requested", False)
    assert pipeline.execute(run_id, allow_dzen=True) == "SUCCEEDED"


def test_send_cannot_resume_an_unfinished_paid_stage(tmp_path, monkeypatch):
    store, run_id = ready_week(tmp_path, monkeypatch, 1)
    store.execute("UPDATE pipeline_stages SET status='ERROR' WHERE run_id=? AND stage='PLAN'", (run_id,))
    with pytest.raises(UserActionRequired, match="платные"):
        assert_publish_ready(store, run_id)
    assert not store.rows("SELECT * FROM publications")


def test_no_authorization_or_live_proof_means_no_browser(tmp_path, monkeypatch):
    store, run_id = ready_week(tmp_path, monkeypatch, 1)
    adapter = DzenPublisher(store)
    monkeypatch.setattr(adapter, "browser", lambda: pytest.fail("Browser must not open"))
    assert not adapter.verified
    with pytest.raises(UserActionRequired):
        DzenScheduling(store, adapter).schedule(1)
    assert not store.rows("SELECT * FROM publications")


def test_autopilot_needs_separate_publication_authorization(tmp_path, monkeypatch):
    store, run_id = ready_week(tmp_path, monkeypatch, 1)
    store.set_setting("mode", "AUTOPILOT")
    adapter = MockStudio(store)
    assert Pipeline(store, PublishingOnlyProvider(), dzen=DzenScheduling(store, adapter)).execute(run_id) == "AWAITING_SEND"
    assert not adapter.created


def test_remote_wrong_id_cannot_replace_stored_url(tmp_path, monkeypatch):
    store, _ = ready_week(tmp_path, monkeypatch, 1)
    adapter = MockStudio(store)
    dzen = DzenScheduling(store, adapter)
    dzen.schedule(1)
    before = store.one("SELECT * FROM publications WHERE article_id=1")
    with pytest.raises(UserActionRequired):
        checkpoint(store, 1, "RENDERING", url="https://dzen.ru/profile/editor/id/test/other/edit")
    after = store.one("SELECT * FROM publications WHERE article_id=1")
    assert before == after
    assert dumps(before)


def test_verified_handoff_preserves_visual_authoring_data(tmp_path, monkeypatch):
    import json
    store, _ = ready_week(tmp_path, monkeypatch, 1)
    report = {"assets": [{"source_page_url": "https://example.com/photo"}], "assembly": {"text": "unchanged"},
              "publishing_handoff": {"execution_status": "not_attempted", "verified_in_dzen": False}}
    store.execute("INSERT INTO visual_results(article_id,call_key,payload_json,created_at) VALUES(1,'test',?,?)", (dumps(report), now_iso()))
    DzenScheduling(store, MockStudio(store)).schedule(1)
    result = json.loads(store.one("SELECT payload_json FROM visual_results")["payload_json"])
    assert result["assets"] == report["assets"]
    assert result["assembly"] == report["assembly"]
    assert result["publishing_handoff"]["verified_in_dzen"]
    assert result["publishing_handoff"]["execution_status"] == "succeeded"


def test_auth_failure_before_create_can_resume_without_manual_rebinding(tmp_path, monkeypatch):
    store, _ = ready_week(tmp_path, monkeypatch, 1)
    adapter = DzenPublisher(store)
    calls = []
    monkeypatch.setattr(adapter, "create_scheduled", lambda *a: calls.append(a) or "reconciled")
    result = adapter.resume_scheduled({"status": "PREPARING_IN_DZEN", "dzen_url": None},
                                      store.get(1), store.detail(1), None)
    assert result == "reconciled"
    assert len(calls) == 1


def test_changed_time_updates_existing_publication_and_checks_equivalent_slots(tmp_path, monkeypatch):
    store, _ = ready_week(tmp_path, monkeypatch, 5)
    adapter = MockStudio(store)
    dzen = DzenScheduling(store, adapter)
    dzen.schedule(1)
    store.update_status(1, ArticleStatus.APPROVED)
    store.execute("UPDATE article_details SET scheduled_at='2030-01-07T08:00:00+03:00' WHERE article_id=1")
    dzen.update(1)
    assert store.one("SELECT scheduled_at FROM publications WHERE article_id=1")["scheduled_at"] == "2030-01-07T08:00:00+03:00"
    assert len(store.rows("SELECT * FROM publications")) == 1
    assert adapter.created == {1: 1}
    store.execute("UPDATE article_details SET scheduled_at='2030-01-07T05:00:00+00:00' WHERE article_id=2")
    with pytest.raises(UserActionRequired, match="занято"):
        dzen.schedule(2)
