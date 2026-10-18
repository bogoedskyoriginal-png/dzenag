import copy
import io
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from dzen_agent.editorial import build_chat_context
from dzen_agent.config import PROJECT_ROOT, get_settings
from dzen_agent.editorial_contracts import DiscoveryV1, PlannerV1, ResearchV1, WriterV1, ImageV1
from dzen_agent.errors import UserActionRequired
from dzen_agent.pipeline.orchestrator import Pipeline, create_run
from dzen_agent.plan_review import apply_proposals, approve_plan
from dzen_agent.prompt_service import Prompts
from dzen_agent.provider import AITunnel
from dzen_agent.recovery import authorize_retry
from dzen_agent.services import ContentService, qa_article, week_slots
from dzen_agent.statistics import import_csv, preview_csv
from dzen_agent.store import Store, dumps
from dzen_agent.visuals import digest, make_cover, validate_visuals

EXAMPLES = json.loads((Path(__file__).parent / "fixtures/editorial_v1.json").read_text("utf-8"))
TOPICS = ["Цветная реставрация киноплёнки", "Работа каскадёров на льду", "Сценография деревянной деревни",
    "Музыкальные партитуры композиторов", "Грим фантастических существ", "Операторы подводных съёмок",
    "История анимационной студии", "Художники костюмов девятнадцатого века", "Литературные прототипы героев",
    "Звуковые эффекты грозы", "Документальные портреты космонавтов", "Режиссёрская школа комедии",
    "Детские приключения на экране", "Танцевальные сцены мюзиклов", "Реквизит железнодорожных историй",
    "Северные экспедиции кинематографистов", "Фестивальные показы немого кино", "Монтаж спортивных соревнований",
    "Зарождение телевизионных сериалов", "Инженеры комбинированных кадров", "Работа переводчиков дубляжа"]


def example(stage, index=0):
    return copy.deepcopy(EXAMPLES[stage][index])


class NativeAPI:
    def __init__(self):
        self.calls = []
        self.fail_research = None
        self.fail_json = None
        self.no_images = False
        self.no_images_for = None
        self.fail_download = False
        self.outputs = {}

    def handler(self, request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 500})
        payload = json.loads(request.content)
        user = payload["messages"][1]["content"]
        inspecting = isinstance(user, list)
        context = json.loads(user[0]["text"] if inspecting else user)
        stage = payload["model"].removesuffix("-test")
        if stage == "visual":
            stage = "image"
        self.calls.append((stage, context, inspecting))
        count = context.get("editorial_config", {}).get("target_article_count", 1)
        run = context["run_context"].get("run_id", context["run_context"].get("pipeline_run_id"))
        if stage == "analytics":
            output = json.loads((Path(__file__).parent / "fixtures/analytics_v1.json").read_text("utf-8"))
        elif stage == "discovery":
            assert context["analytics"]["discovery_brief"]["research_questions"]
            output = example(stage)
            output.update(run_id=run, planning_window=context["run_context"]["planning_window"])
            for i in range(count):
                candidate = example(stage, 1)
                candidate.update(idea_id=f"D{i:03}", topic=TOPICS[i], content_format="making_of",
                    working_headline=TOPICS[i], angle=TOPICS[i], reader_promise=TOPICS[i],
                    sources=[{**example(stage, 3), "url": f"https://example.com/d{i}"}],
                    anchor_facts=[{**example(stage, 2), "claim": TOPICS[i], "source_ids": ["S001"]}],
                    research_questions=["Какие архивы подтверждают " + TOPICS[i] + "?"])
                output["candidates"].append(candidate)
        elif stage == "planner":
            assert context["analytics"]["planner_brief"]["priority_clusters"]
            output = example(stage)
            candidates = context["discovery"]["candidates"]
            n = len(candidates)
            output.update(run_id=run, period=context["run_context"]["planning_window"])
            output["target"].update(article_count=n, selected_count=n)
            for i, (candidate, slot) in enumerate(zip(candidates, context["planning_config"]["allowed_slots"], strict=True)):
                item = example(stage, 1)
                item.update(concept_id=f"P{i:03}", source_idea_id=candidate["idea_id"], scheduled_at=slot,
                    topic=candidate["topic"], angle=candidate["angle"], content_format="making_of",
                    working_headline=candidate["topic"], key_thesis=candidate["topic"],
                    reader_promise=candidate["topic"], initial_sources=[{**example(stage, 2), "url": candidate["sources"][0]["url"]}])
                item["research_brief"].update(objective=candidate["topic"], questions=candidate["research_questions"],
                                              initial_source_ids=["S001"])
                output["items"].append(item)
            output["handoff"]["ready_for_editorial_review"] = [x["concept_id"] for x in output["items"]]
        elif stage == "research":
            concept = context["concept"]
            assert concept["approval_status"] == "approved"
            assert concept["research_brief"]["objective"]
            output = example(stage)
            output.update(run_id=run, concept_id=concept["concept_id"], concept_revision=concept["revision"])
            fact = {**example(stage, 3), "claim": concept["topic"], "source_ids": ["S001"]}
            work = {**example(stage, 1), "title": concept["topic"], "year": 1975, "director_names": ["Режиссёр"],
                    "lead_cast": ["Актёр"], "source_ids": ["S001"], "fact_ids": ["F001"]}
            output.update(facts=[fact], works=[work], sources=[{**example(stage, 9), "url": "https://example.com/r1",
                "supports": ["F001"]}])
            output["research_summary"]["main_findings"] = [concept["topic"]]
            output["writer_handoff"]["approved_fact_ids"] = ["F001"]
            if concept["concept_id"] == self.fail_research:
                output["research_status"] = "needs_more_research"
                output["status"] = "insufficient_data"
                output["writer_handoff"]["sufficiency"]["coverage"] = "insufficient"
        elif stage == "writer":
            concept = context["concept"]
            assert concept["revision"] == context["research"]["concept_revision"]
            assert context["research"]["facts"][0]["claim"] == concept["topic"]
            assert "tools" not in payload
            topic = concept["topic"]
            output = example(stage)
            output.update(run_id=run, concept_id=concept["concept_id"], concept_revision=concept["revision"])
            body = (topic + ". " + topic + ".\n\n" + context["editorial_config"]["subscription_heading"]
                + "\n\nСпасибо, дорогие читатели!\n\n## " + topic
                + "\n\n> Режиссер: Режиссёр\n>\n> В главных ролях: Актёр\n\n" + (topic + ". ") * 10
                + "\n\nЧто вам запомнилось?\n\n" + context["editorial_config"]["final_heading"])
            output["content"].update(headline=topic, article_markdown=body, comments_question="Что вам запомнилось?")
            output["handoff"]["ready_for_qa"] = True
            output["evidence_map"] = [{"fact_id": "F001", "anchor_text": topic}]
        else:
            assert context["writer"]["content"]["article_markdown"]
            output = example("image")
            concept = context["concept"]
            output.update(run_id=run, concept_id=concept["concept_id"], concept_revision=concept["revision"])
            asset = example("image", 1)
            asset.update(source_page_url="https://example.com/photos", source_image_url="https://example.com/still.jpg",
                         related_work_id="W001", source_name="Фотогалерея", authenticity_basis="Страница и просмотр кадра")
            if inspecting:
                assert any(x.get("type") == "image_url" for x in user)
                asset.update(context["tool_results"]["assets"][0])
                asset["authenticity_status"] = "verified"
                placement = example("image", 2)
                placement.update(target_section_heading=concept["topic"], anchor_text="В главных ролях: Актёр")
                output["placements"] = [placement]
            no_images = self.no_images or concept["concept_id"] == self.no_images_for
            output["assets"] = [] if no_images else [asset]
            output["cover"].update(source_asset_id=None if no_images else "IMG001", cover_text=concept["topic"])
        self.outputs[(stage, inspecting)] = copy.deepcopy(output)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}" if stage == self.fail_json else dumps(output)}}],
            "citations": ["https://example.com/r1", "https://example.com/photos"] + [f"https://example.com/d{i}" for i in range(21)],
            "usage": {"cost_rub": 0.05}})


@pytest.fixture
def native_run(tmp_path, monkeypatch):
    store = Store(tmp_path / "native.db")
    store.initialize()
    prompts = Prompts(store)
    prompts.seed()
    for stage in ("discovery", "planner", "research", "writer", "image"):
        prompts.save(stage.upper(), "Author contract", dumps({"contract_version": "1.0", "stage": stage}))
    for role in ("analytics", "discovery", "planner", "research", "writer", "visual"):
        store.set_setting(role + "_model", role + "-test")
    store.set_setting("max_output_tokens", 64000)
    store.set_setting("publication_days", list(range(7)))
    store.set_setting("publication_times", ["05:00", "06:00", "16:00"])
    store.set_setting("max_articles_per_day", 3)
    store.set_setting("minimum_length_chars", 1)
    import_csv(store, "stats.csv", preview_csv(b"article_id;title;views\na;First;300\nb;Second;200"))
    api = NativeAPI()
    picture = io.BytesIO()
    Image.new("RGB", (800, 450), "#267baa").save(picture, "PNG")
    def fetch(url, limit=None):
        if api.fail_download:
            raise httpx.ReadTimeout("download failed")
        return picture.getvalue() if url.endswith(".jpg") else b'<img src="https://example.com/still.jpg">'
    monkeypatch.setattr("dzen_agent.visuals.fetch_public", fetch)
    provider = AITunnel(store, "test-key", httpx.Client(transport=httpx.MockTransport(api.handler)))
    return store, api, provider


@pytest.mark.parametrize("count", [1, 5, 21])
def test_native_entire_chain_and_real_file_assembly(native_run, count):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", count)
    run = create_run(store, "2030-01-07")
    pipeline = Pipeline(store, provider)
    assert pipeline.execute(run) == "AWAITING_PLAN_REVIEW", store.one("SELECT error FROM pipeline_runs")
    assert [r[0] for r in api.calls] == ["analytics", "discovery", "planner"]
    plan = store.one("SELECT * FROM content_plans WHERE run_id=?", (run,))
    assert len(build_chat_context(store, plan["id"])["plan"]) == count
    scheduled = [x["scheduled_at"] for x in store.rows("SELECT scheduled_at FROM concepts ORDER BY scheduled_at")]
    assert len(set(scheduled)) == count
    assert scheduled[0].endswith("05:00:00+03:00")
    approve_plan(store, plan["id"])
    assert pipeline.execute(run) == "AWAITING_REVIEW", store.one("SELECT error FROM pipeline_runs")
    assert len(store.list_all()) == count
    assert len([x for x in api.calls if x[0] == "research"]) == count
    for article in store.list_all():
        assert qa_article(store, article.id)["passed"]
        assert validate_visuals(store, article.id) == []
        with Image.open(store.active_image(article.id)["path"]) as cover:
            assert cover.size == (1600, 900)
    paid = len(api.calls)
    assert pipeline.execute(run) == "AWAITING_REVIEW"
    assert len(api.calls) == paid
    assert store.costs(run_id=run)["total"] == pytest.approx(paid * 0.05)
    assert [x["scheduled_at"] for x in store.rows("SELECT scheduled_at FROM concepts ORDER BY scheduled_at")] == scheduled


def test_one_research_failure_keeps_previous_paid_work_and_blocks_writer(native_run):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 5)
    run = create_run(store, "2030-01-07")
    pipeline = Pipeline(store, provider)
    assert pipeline.execute(run) == "AWAITING_PLAN_REVIEW"
    approve_plan(store, store.one("SELECT id FROM content_plans")["id"])
    api.fail_research = "P001"
    assert pipeline.execute(run) == "NEEDS_ACTION"
    assert len([x for x in api.calls if x[0] == "research"]) == 2
    assert not [x for x in api.calls if x[0] == "writer"]
    paid = len(api.calls)
    assert pipeline.execute(run) == "NEEDS_ACTION"
    assert len(api.calls) == paid
    assert store.one("SELECT status FROM research_jobs ORDER BY id")["status"] == "sufficient"


@pytest.mark.parametrize("failure", ["json", "no_images", "download"])
def test_invalid_or_missing_visuals_preserve_text_and_receipts(native_run, failure):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 1)
    run = create_run(store, "2030-01-07")
    pipeline = Pipeline(store, provider)
    assert pipeline.execute(run) == "AWAITING_PLAN_REVIEW"
    approve_plan(store, store.one("SELECT id FROM content_plans")["id"])
    if failure == "json":
        api.fail_json = "writer"
    elif failure == "no_images":
        api.no_images = True
    else:
        api.fail_download = True
    assert pipeline.execute(run) == "NEEDS_ACTION"
    paid = len(api.calls)
    assert pipeline.execute(run) == "NEEDS_ACTION"
    assert len(api.calls) == paid
    if failure != "json":
        assert store.list_all()[0].body
        result = json.loads(store.one("SELECT payload_json FROM visual_results")["payload_json"])
        assert result["status"] == "partial"
        assert result["publishing_handoff"]["execution_status"] == "not_attempted"


def test_grooming_updates_one_native_concept_before_research(native_run, proposal_message):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 5)
    run = create_run(store, "2030-01-07")
    pipeline = Pipeline(store, provider)
    assert pipeline.execute(run) == "AWAITING_PLAN_REVIEW"
    plan = store.one("SELECT id FROM content_plans")["id"]
    row = store.one("SELECT * FROM concepts ORDER BY id LIMIT 1")
    message = proposal_message(store, plan, [{"type": "replace_concept", "concept_id": row["id"],
        "new_topic": "Съёмки морских путешествий", "working_title": "Морские путешествия",
        "angle": "Работа операторов в океане", "reason": "Другой угол"}])
    apply_proposals(store, message, [0])
    assert not [x for x in api.calls if x[0] == "research"]
    approve_plan(store, plan)
    assert pipeline.execute(run) == "AWAITING_REVIEW", store.one("SELECT error FROM pipeline_runs")
    first = next(c for stage, c, _ in api.calls if stage == "research")
    assert first["concept"]["topic"] == "Съёмки морских путешествий"
    assert first["concept"]["revision"] == 2
    assert first["concept"]["initial_sources"] == []


def test_week_slots_reject_duplicates_and_short_schedule():
    settings = {"publication_days": list(range(7)), "publication_time": "05:00", "max_articles_per_week": 21,
                "publication_times": ["05:00", "06:00", "16:00"], "max_articles_per_day": 3}
    assert len(week_slots("2030-01-07", settings)) == 21
    with pytest.raises(ValueError):
        week_slots("2030-01-07", {**settings, "publication_times": ["05:00", "05:00", "16:00"]})
    with pytest.raises(ValueError):
        week_slots("2030-01-07", {**settings, "publication_days": [0]})
    custom = {**settings, "max_articles_per_week": 2, "publication_days": [0, 1],
              "publication_slots": {"0": ["07:30"], "1": ["18:45"]}}
    assert [x[11:16] for x in week_slots("2030-01-07", custom)] == ["07:30", "18:45"]


def test_native_contracts_match_author_json_shapes(native_run):
    # The fixtures are literal JSON examples extracted from the user's saved prompts.
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 1)
    run = create_run(store, "2030-01-07")
    pipeline = Pipeline(store, provider)
    pipeline.execute(run)
    approve_plan(store, store.one("SELECT id FROM content_plans")["id"])
    assert pipeline.execute(run) == "AWAITING_REVIEW"
    for stage, schema in [("discovery", DiscoveryV1), ("planner", PlannerV1), ("research", ResearchV1),
                          ("writer", WriterV1), ("image", ImageV1)]:
        payload = api.outputs[(stage, False)]
        assert set(payload) == set(EXAMPLES[stage][0])
        assert schema.model_validate(payload).model_dump(mode="json") == payload
    wrong = copy.deepcopy(api.outputs[("research", False)])
    wrong["writer_handoff"]["approved_fact_ids"] = ["F404"]
    with pytest.raises(ValidationError):
        ResearchV1.model_validate(wrong)


def test_native_writer_rejects_stale_dossier_before_payment(native_run):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 1)
    run = create_run(store, "2030-01-07")
    pipeline = Pipeline(store, provider)
    pipeline.execute(run)
    approve_plan(store, store.one("SELECT id FROM content_plans")["id"])
    api.fail_json = "writer"
    assert pipeline.execute(run) == "NEEDS_ACTION"
    row = store.one("SELECT * FROM concepts")
    payload = json.loads(store.one("SELECT payload_json FROM research_jobs")["payload_json"])
    payload["editorial_contract"]["concept_revision"] = 99
    store.execute("UPDATE research_jobs SET payload_json=?", (dumps(payload),))
    service = ContentService(store, provider, run_id=run)
    paid = len(api.calls)
    with pytest.raises(UserActionRequired, match="версии"):
        service.write(row["topic"], row["research_id"], "stale", concept_row=row)
    assert len(api.calls) == paid


def test_cover_keeps_real_photo_and_does_not_change_source(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (800, 450), "blue").save(source)
    before = digest(source.read_bytes())
    target = make_cover(source, "Настоящие фотографии", tmp_path / "cover.png")
    assert digest(source.read_bytes()) == before
    with Image.open(target) as picture:
        assert picture.size == (1600, 900)


def test_grooming_researches_only_changed_concept(native_run, proposal_message):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 5)
    run = create_run(store, "2030-01-07")
    pipeline = Pipeline(store, provider)
    assert pipeline.execute(run) == "AWAITING_PLAN_REVIEW"
    plan_id = store.one("SELECT id FROM content_plans")["id"]
    approve_plan(store, plan_id)
    service = ContentService(store, provider, run_id=run)
    pipeline._research_concepts(run, {}, f"run:{run}", service)
    store.execute("UPDATE pipeline_stages SET status='SUCCEEDED' WHERE stage='RESEARCH'")
    row = store.one("SELECT * FROM concepts ORDER BY id LIMIT 1")
    message = proposal_message(store, plan_id, [{"type": "update_concept", "concept_id": row["id"],
        "angle": "Обновлённый ракурс реставрации", "reason": "Уточнение"}])
    apply_proposals(store, message, [0])
    assert pipeline.execute(run) == "AWAITING_PLAN_REVIEW"
    approve_plan(store, plan_id)
    assert pipeline.execute(run) == "AWAITING_REVIEW", store.one("SELECT error FROM pipeline_runs")
    research_calls = [c for stage, c, _ in api.calls if stage == "research"]
    assert len(research_calls) == 6
    assert research_calls[-1]["concept"]["angle"] == "Обновлённый ракурс реставрации"


def test_image_failure_does_not_repeat_other_articles(native_run):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 5)
    run = create_run(store, "2030-01-07")
    pipeline = Pipeline(store, provider)
    pipeline.execute(run)
    approve_plan(store, store.one("SELECT id FROM content_plans")["id"])
    api.no_images_for = "P001"
    assert pipeline.execute(run) == "NEEDS_ACTION"
    assert len(store.list_all()) == 5
    assert len(store.rows("SELECT * FROM images")) == 1
    paid = len(api.calls)
    assert pipeline.execute(run) == "NEEDS_ACTION"
    assert len(api.calls) == paid


def test_visual_qa_detects_text_and_file_tampering(native_run):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 1)
    run = create_run(store, "2030-01-07")
    pipeline = Pipeline(store, provider)
    pipeline.execute(run)
    approve_plan(store, store.one("SELECT id FROM content_plans")["id"])
    assert pipeline.execute(run) == "AWAITING_REVIEW"
    article = store.list_all()[0]
    store.update_content(article.id, article.title, article.body + "\nИзменение редактора")
    assert any("Текст изменился" in x for x in validate_visuals(store, article.id))
    assert not qa_article(store, article.id)["passed"]
    cover = Path(store.active_image(article.id)["path"])
    Image.new("RGB", (1600, 900), "red").save(cover)
    assert any("Файл обложки изменён" in x for x in validate_visuals(store, article.id))


def test_twenty_one_concepts_and_custom_slots_render_in_streamlit(native_run):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 21)
    run = create_run(store, "2030-01-07")
    assert Pipeline(store, provider).execute(run) == "AWAITING_PLAN_REVIEW"
    config = replace(get_settings(), database_path=store.database_path, aitunnel_api_key=None)
    with patch("dzen_agent.config.get_settings", return_value=config):
        app = AppTest.from_file(str(PROJECT_ROOT / "dzen_agent/ui.py"), default_timeout=20).run()
        app.sidebar.radio[0].set_value("Контент-план").run()
        assert not app.exception and not app.error
        next(x for x in app.radio if x.label == "Представление плана").set_value("Все концепты").run()
        assert any(len(frame.value) == 21 for frame in app.dataframe)
        app.sidebar.radio[0].set_value("Настройки").run()
        assert not app.exception and not app.error
        assert next(x for x in app.number_input if x.label == "Публикаций за неделю").value == 21
        assert next(x for x in app.text_input if x.label == "Время публикаций по Москве").value == "05:00, 06:00, 16:00"


def test_twenty_one_long_concepts_are_not_silently_omitted(native_run):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 21)
    run = create_run(store, "2030-01-07")
    Pipeline(store, provider).execute(run)
    plan_id = store.one("SELECT id FROM content_plans")["id"]
    for row in store.rows("SELECT * FROM concepts"):
        payload = json.loads(row["payload_json"])
        for key in ("working_title", "angle", "why_now", "audience_value", "thesis"):
            payload[key] = "Описание " * 100
        payload["key_points"] = ["Подробность " * 60] * 6
        store.execute("UPDATE concepts SET payload_json=? WHERE id=?", (dumps(payload), row["id"]))
    context = build_chat_context(store, plan_id)
    assert len(context["plan"]) == 21
    assert len(dumps(context)) < 60000


def test_native_autopilot_still_requires_editorial_approval(native_run):
    store, api, provider = native_run
    store.set_setting("mode", "AUTOPILOT")
    store.set_setting("max_articles_per_week", 1)
    run = create_run(store, "2030-01-07")
    assert Pipeline(store, provider).execute(run) == "AWAITING_PLAN_REVIEW"
    assert not [x for x in api.calls if x[0] == "research"]


def test_article_rescheduling_checks_conflicts_and_preserves_other_dates(native_run):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 5)
    run = create_run(store, "2030-01-07")
    pipeline = Pipeline(store, provider)
    pipeline.execute(run)
    approve_plan(store, store.one("SELECT id FROM content_plans")["id"])
    assert pipeline.execute(run) == "AWAITING_REVIEW"
    details = store.rows("SELECT * FROM article_details ORDER BY article_id")
    service = ContentService(store, provider)
    with pytest.raises(ValueError, match="занято"):
        service.set_schedule(details[0]["article_id"], details[1]["scheduled_at"])
    with pytest.raises(ValueError, match="число публикаций"):
        service.set_schedule(details[3]["article_id"], "2030-01-07T18:45:00+03:00")
    service.set_schedule(details[0]["article_id"], "2030-01-08T18:45:00+03:00")
    assert store.one("SELECT scheduled_at FROM article_details WHERE article_id=?", (details[0]["article_id"],))["scheduled_at"].endswith("18:45:00+03:00")
    assert store.one("SELECT scheduled_at FROM article_details WHERE article_id=?", (details[1]["article_id"],))["scheduled_at"] == details[1]["scheduled_at"]


def test_public_download_rejects_private_network_and_credentials(monkeypatch):
    from dzen_agent.visuals import public_url
    monkeypatch.setattr("dzen_agent.visuals.socket.getaddrinfo", lambda *args: [(2, 1, 6, "", ("127.0.0.1", 80))])
    with pytest.raises(ValueError, match="локальной"):
        public_url("http://example.com/photo.jpg")
    with pytest.raises(ValueError, match="без паролей"):
        public_url("https://user:password@example.com/photo.jpg")


def test_recovery_repeats_only_explicitly_authorized_failed_research(native_run):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 5)
    run = create_run(store, "2030-01-07")
    pipeline = Pipeline(store, provider)
    pipeline.execute(run)
    approve_plan(store, store.one("SELECT id FROM content_plans")["id"])
    api.fail_research = "P001"
    assert pipeline.execute(run) == "NEEDS_ACTION"
    receipt = store.rows("SELECT call_key FROM usage_events WHERE operation='RESEARCH' ORDER BY id")[1]["call_key"]
    authorize_retry(store, receipt, confirmed=True)
    api.fail_research = None
    assert pipeline.execute(run) == "AWAITING_REVIEW", store.one("SELECT error FROM pipeline_runs")
    calls = [c["concept"]["concept_id"] for stage, c, _ in api.calls if stage == "research"]
    assert calls.count("P000") == 1 and calls.count("P001") == 2 and len(calls) == 6
    assert len(store.list_all()) == 5


def test_download_recovery_reuses_paid_search_response(native_run):
    store, api, provider = native_run
    store.set_setting("max_articles_per_week", 1)
    run = create_run(store, "2030-01-07")
    pipeline = Pipeline(store, provider)
    pipeline.execute(run)
    approve_plan(store, store.one("SELECT id FROM content_plans")["id"])
    api.fail_download = True
    assert pipeline.execute(run) == "NEEDS_ACTION"
    api.fail_download = False
    assert pipeline.execute(run) == "AWAITING_REVIEW", store.one("SELECT error FROM pipeline_runs")
    assert len([1 for stage, _, inspecting in api.calls if stage == "image" and not inspecting]) == 1
    assert len([1 for stage, _, inspecting in api.calls if stage == "image" and inspecting]) == 1
