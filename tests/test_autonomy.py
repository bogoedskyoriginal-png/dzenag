import json
import copy
from datetime import date, timedelta
from dataclasses import replace
from unittest.mock import patch

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from test_editorial_contracts_v1 import native_run as _native_run
from dzen_agent.autonomy import start_run
from dzen_agent.config import PROJECT_ROOT, get_settings
from dzen_agent.evidence import locate
from dzen_agent.execution import decode_json, maximum_cost
from dzen_agent.pipeline.orchestrator import Pipeline
from dzen_agent.services import qa_article

native_run = _native_run


def price_models(store):
    store.set_setting("public_catalog", {"chat": {name + "-test": {"context_size": 100000, "prompt_cost": 1,
        "completion_cost": 1, "max_output": 64000, "modalities": {"input": ["text", "image"]}}
        for name in ("analytics", "discovery", "planner", "research", "writer", "visual")}})


def calendar_for(count):
    first = date(2030, 1, 8)  # Tuesday, crossing the week boundary for 21 articles.
    result = {}
    while count:
        result[first.isoformat()] = min(3, count)
        count -= min(3, count)
        first += timedelta(days=1)
    return result


@pytest.mark.parametrize("count", [1, 3, 5, 21])
def test_one_action_full_chain_no_review_no_repeated_cost(native_run, count):
    store, api, provider = native_run
    price_models(store)
    calendar = calendar_for(count)
    run = start_run(store, calendar, "Больше историй о кинопроизводстве", 100, confirmed=True)
    before_prompts = store.rows("SELECT * FROM prompt_versions")
    worker = Pipeline(store, provider)
    assert worker.execute(run) == "PREPARED", store.one("SELECT error FROM pipeline_runs WHERE id=?", (run,))
    assert len(store.list_all()) == count
    for article in store.list_all():
        assert qa_article(store, article.id)["passed"]
        assert store.active_image(article.id)
    assert store.one("SELECT actor,mode FROM plan_approvals") == {"actor": "orchestrator", "mode": "autonomous"}
    assert store.rows("SELECT * FROM prompt_versions") == before_prompts
    assert api.calls[0][0] == "analytics"
    assert [c[0] for c in api.calls][:3] == ["analytics", "discovery", "planner"]
    stages = [c[0] for c in api.calls]
    assert stages[3:7] == ["research", "writer", "image", "image"]
    discovery = next(c for stage,c,_ in api.calls if stage == "discovery")
    assert discovery["editorial_wishes"]
    writer = next(c for stage,c,_ in api.calls if stage == "writer")
    assert writer["editorial_materials"]["writing_mode"] == "from_scratch"
    paid = len(api.calls)
    dates = store.rows("SELECT scheduled_at FROM concepts ORDER BY scheduled_at")
    assert worker.execute(run) == "PREPARED"
    assert len(api.calls) == paid
    assert store.rows("SELECT scheduled_at FROM concepts ORDER BY scheduled_at") == dates


def test_bad_research_does_not_block_other_articles_and_has_finite_attempts(native_run):
    store, api, provider = native_run
    price_models(store)
    run = start_run(store, calendar_for(5), "", 100, confirmed=True)
    api.fail_research = "P001"
    worker = Pipeline(store, provider)
    assert worker.execute(run) == "PARTIAL_READY"
    assert len(store.list_all()) == 4
    research = [c["concept"]["concept_id"] for stage,c,_ in api.calls if stage == "research"]
    assert research.count("P001") == 3
    assert len(store.rows("SELECT * FROM pipeline_items WHERE stage='QA' AND status='SUCCEEDED'")) == 4
    paid = len(api.calls)
    worker.execute(run)
    assert len(api.calls) == paid


def test_absent_images_are_honest_transfer_constraint_not_factual_failure(native_run):
    store, api, provider = native_run
    price_models(store)
    run = start_run(store, calendar_for(1), "", 100, confirmed=True)
    api.no_images = True
    assert Pipeline(store, provider).execute(run) == "PREPARED"
    article = store.list_all()[0]
    result = qa_article(store, article.id)
    assert result["passed"] and not result["transfer_ready"] and result["warnings"]
    assert not store.active_image(article.id)
    assert not store.rows("SELECT * FROM publications")


@pytest.mark.parametrize("fragment", ["**в 1964 году**", "в\n1964\tгоду", "в 1964\u00a0году"])
def test_evidence_formatting(fragment):
    assert locate("Подготовка началась " + fragment + ".", "Подготовка началась в 1964 году.")


def test_context_year_is_explicit_and_no_numbers_or_negations_are_guessed():
    body = "Зимой 1964 года снимали Москву. А в августе того же года начали павильонные съемки."
    assert locate(body, "В августе 1964 года начали павильонные съемки")
    assert not locate(body, "В августе 1965 года начали павильонные съемки")
    assert not locate("В 1964 и 1965 годах снимали. В августе того же года начали съемки.", "В августе 1965 года начали съемки")
    assert not locate("В 1964 году не снимали Москву.", "В 1964 году снимали Москву.")
    assert not locate("Т_34", "Т34")
    assert not locate("3*4", "34")


def test_response_fences_and_unknown_types_are_not_invented():
    assert decode_json('```json\n{"status":"ok"}\n```') == {"status":"ok"}
    with pytest.raises(json.JSONDecodeError):
        decode_json('{"status":')


def test_budget_reservation_unknown_and_uncertain_never_repeat(native_run):
    store, api, provider = native_run
    price_models(store)
    run = start_run(store, calendar_for(1), "", 100, confirmed=True)
    calls = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance":500})
        calls.append(1)
        raise httpx.ReadTimeout("mock")
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    worker = Pipeline(store, provider)
    assert worker.execute(run) == "NEEDS_ACTION"
    assert worker.execute(run) == "NEEDS_ACTION"
    assert len(calls) == 1
    assert store.one("SELECT status FROM call_reservations")["status"] == "UNCERTAIN"


def test_budget_and_cost_bound_are_checked_before_dispatch(native_run):
    store, api, provider = native_run
    price_models(store)
    run = start_run(store, calendar_for(1), "", 0.01, confirmed=True)
    assert Pipeline(store, provider).execute(run) == "NEEDS_ACTION"
    assert not api.calls
    with pytest.raises(RuntimeError):
        maximum_cost({}, "missing", {"max_tokens": 1})


def test_explicit_contract_metadata_survives_author_wording_changes(native_run):
    from dzen_agent.prompt_service import Prompts
    from dzen_agent.editorial_pipeline import native
    store, _, _ = native_run
    prompts = Prompts(store)
    prompts.save("WRITER", "Новая редакция", "Авторский текст без технического JSON.")
    assert native(prompts.active("WRITER"), "writer")
    prompt = {"content": '{"contract_version":"1.0","stage":"writer"}'}
    assert not native(prompt, "writer")


def test_enum_repairs_are_unambiguous_and_unknown_values_remain_invalid():
    from dzen_agent.editorial_contracts import WriterV1
    from dzen_agent.execution import normalize_enums
    value, repairs = normalize_enums({"status": " OK "}, WriterV1)
    assert value["status"] == "ok" and repairs
    value, repairs = normalize_enums({"status": "almost_ok"}, WriterV1)
    assert value["status"] == "almost_ok" and not repairs


def test_analytics_numeric_fields_are_owned_by_python():
    from dzen_agent.analytics_contract import Analytics
    from dzen_agent.execution import authoritative_fields
    original = {"funnel": {"impressions": 999}, "status": "ok", "data_quality": {"comparison_limitations": []}}
    value, repairs = authoritative_fields(original, Analytics, {"analysis_config": {"validated_funnel": {"impressions": None}}})
    assert value["funnel"] == {"impressions": None} and value["status"] == "partial" and repairs
    assert original["funnel"] == {"impressions": 999}


@pytest.mark.parametrize("cost", [None, 1000])
def test_unknown_success_cost_or_reservation_overrun_blocks_next_operation(native_run, cost):
    store, api, provider = native_run
    price_models(store)
    run = start_run(store, calendar_for(1), "", 100, confirmed=True)
    original = api.handler
    def handler(request):
        result = original(request)
        if request.method == "POST":
            data = result.json()
            data["usage"] = {} if cost is None else {"cost_rub": cost}
            return httpx.Response(200, json=data)
        return result
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert Pipeline(store, provider).execute(run) == "NEEDS_ACTION"
    assert len(api.calls) == 1
    assert store.one("SELECT status FROM call_reservations")["status"] == ("UNCERTAIN" if cost is None else "OVERRUN")
    assert Pipeline(store, provider).execute(run) == "NEEDS_ACTION"
    assert len(api.calls) == 1


def test_draft_handoff_resumes_same_id_without_ai_or_publishing(native_run):
    from dzen_agent.browser.scheduling import DzenScheduling, checkpoint
    store, api, provider = native_run
    price_models(store)
    run = start_run(store, calendar_for(1), "", 100, confirmed=True, drafts=True)
    class Studio:
        verified = True
        created = 0
        attempts = 0
        def create_draft(self, article, detail, image, publication):
            self.attempts += 1
            if not publication["dzen_url"]:
                self.created += 1
                checkpoint(store, article.id, "RENDERING", url="https://dzen.ru/profile/editor/id/test/one/edit", dzen_id="one")
                raise RuntimeError("mock interrupted browser")
            return {"dzen_url": publication["dzen_url"], "dzen_id": "one", "publicly_published": False}
    adapter = Studio()
    worker = Pipeline(store, provider, dzen=DzenScheduling(store, adapter))
    assert worker.execute(run) == "PREPARED"
    paid = len(api.calls)
    assert store.one("SELECT status FROM publications")["status"] == "RENDERING"
    assert worker.execute(run) == "PREPARED"
    assert store.one("SELECT status FROM publications")["status"] == "DRAFT_VERIFIED"
    assert adapter.created == 1 and adapter.attempts == 2 and len(api.calls) == paid
    store.execute("UPDATE publications SET status='CREATING_DRAFT',dzen_url=NULL")
    worker.execute(run)
    assert adapter.created == 1 and len(api.calls) == paid


def test_search_engine_has_a_finite_cost_bound(native_run):
    store, _, _ = native_run
    price_models(store)
    plain = maximum_cost(store.settings(), "research-test", {"max_tokens": 16000})
    bounded = {"max_tokens": 16000, "max_tool_calls": 3, "tools": [{"parameters": {
        "engine":"parallel", "mode":"basic", "max_uses":3, "max_results":5}}]}
    assert maximum_cost(store.settings(), "research-test", bounded) >= plain + 5
    bounded["max_tool_calls"] = 100
    with pytest.raises(RuntimeError):
        maximum_cost(store.settings(), "research-test", bounded)


def test_uncertain_call_blocks_new_runs_not_just_its_original_run(native_run):
    from dzen_agent.execution import reserve_call
    store, _, _ = native_run
    price_models(store)
    first = start_run(store, {"2030-01-08":1}, "", 100, confirmed=True)
    second = start_run(store, {"2030-02-05":1}, "", 100, confirmed=True)
    store.execute("INSERT INTO call_reservations VALUES(?,?,1,'UNCERTAIN','now')", ("uncertain", first))
    with pytest.raises(RuntimeError, match="Неопределённая"):
        reserve_call(store, second, "second", "research-test", {"max_tokens":16000})
    assert not store.one("SELECT * FROM call_reservations WHERE call_key='second'")


def test_monthly_budget_checks_full_reservation_before_new_dispatch(native_run):
    from dzen_agent.execution import reserve_call
    store, _, _ = native_run
    price_models(store)
    run = start_run(store, calendar_for(1), "", 100, confirmed=True)
    store.set_setting("monthly_soft_budget_rub", 0.01)
    with pytest.raises(RuntimeError, match="Месячный бюджет"):
        reserve_call(store, run, "next", "research-test", {"max_tokens":16000})
    assert not store.rows("SELECT * FROM call_reservations")


def test_unknown_editorial_identifier_is_not_an_uncertain_payment():
    from dzen_agent.execution import category
    assert category(ValueError("Неизвестный enum")) == "LOCAL_RECOVERABLE"
    assert category(ValueError("Неизвестный источник S42")) == "EDITORIAL_BLOCKER"
    assert category(RuntimeError("Стоимость неизвестна")) == "PAYMENT_UNCERTAIN"


def test_saved_writer_replays_offline_without_next_stage_or_changed_receipt(native_run, monkeypatch):
    from dzen_agent.editorial_contracts import WriterV1
    from dzen_agent.execution import replay_stage
    store, api, provider = native_run
    price_models(store)
    run = start_run(store, calendar_for(1), "", 100, confirmed=True)
    store.execute("UPDATE run_authorizations SET max_repairs=0 WHERE run_id=?", (run,))
    original = WriterV1.validate_research
    def failed(*args):
        raise ValueError("mock interrupted validation after a paid Writer receipt")
    monkeypatch.setattr(WriterV1, "validate_research", failed)
    worker = Pipeline(store, provider)
    assert worker.execute(run) == "NEEDS_ACTION"
    assert not store.list_all()
    saved = store.rows("SELECT * FROM ai_calls")
    before = store.costs(run_id=run)
    calls = len(api.calls)
    monkeypatch.setattr(WriterV1, "validate_research", original)
    report = replay_stage(store, run, "WRITE")
    assert report["status"] == "SUCCEEDED" and report["paid_calls"] == 0
    assert len(store.list_all()) == 1 and len(api.calls) == calls
    assert store.rows("SELECT * FROM ai_calls") == saved
    assert store.costs(run_id=run) == before
    assert not store.rows("SELECT * FROM visual_results")
    assert worker.execute(run) == "PREPARED"
    assert len([s for s,_,_ in api.calls if s == "writer"]) == 1


def test_partial_thesis_is_reframed_and_researched_as_new_revision(native_run):
    store, api, provider = native_run
    price_models(store)
    run = start_run(store, calendar_for(1), "", 100, confirmed=True)
    original = api.handler
    def handler(request):
        response = original(request)
        if request.method == "GET":
            return response
        payload = json.loads(request.content)
        if payload["model"] != "research-test":
            return response
        context = json.loads(payload["messages"][1]["content"])
        if context["concept"]["revision"] > 1:
            assert context["previous_research"]["research_status"] == "needs_more_research"
            reused = response.json()
            reused["citations"] = []  # Existing real search provenance must survive an incremental dossier.
            return httpx.Response(200, json=reused)
        data = response.json()
        value = json.loads(data["choices"][0]["message"]["content"])
        value.update(status="partial", research_status="needs_more_research")
        value["concept_validation"].update(thesis_status="partially_supported", proposed_change="Сосредоточиться на подтверждённой работе художников")
        value["research_summary"]["angle"] = "Работа художников по подтверждённым документам"
        fact = {**value["facts"][0], "fact_id":"F002", "source_ids":["S002"]}
        value["facts"].append(fact)
        value["sources"].append({**copy.deepcopy(value["sources"][0]), "source_id":"S002", "url":"https://example.com/photos", "supports":["F002"]})
        value["writer_handoff"]["approved_fact_ids"].append("F002")
        value["writer_handoff"]["sufficiency"]["coverage"] = "limited"
        data["choices"][0]["message"]["content"] = json.dumps(value)
        return httpx.Response(200, json=data)
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert Pipeline(store, provider).execute(run) == "PREPARED", store.one("SELECT error FROM pipeline_runs")
    assert len([x for stage,x,_ in api.calls if stage == "research"]) == 2
    assert store.one("SELECT revision FROM concepts")["revision"] == 2
    assert len(store.rows("SELECT * FROM research_jobs")) == 2
    assert len(store.rows("SELECT * FROM plan_approvals")) == 2
    assert store.one("SELECT status FROM research_jobs ORDER BY id LIMIT 1")["status"] == "insufficient_research"


def test_search_prose_is_converted_from_paid_response_without_another_search(native_run):
    store, api, provider = native_run
    price_models(store)
    store.set_setting("research_use_web_tool", True)
    run = start_run(store, calendar_for(1), "", 100, confirmed=True)
    original = api.handler
    research_calls = []
    def handler(request):
        response = original(request)
        if request.method == "GET":
            return response
        payload = json.loads(request.content)
        if payload["model"] != "research-test":
            return response
        context = json.loads(payload["messages"][1]["content"])
        research_calls.append(payload)
        data = response.json()
        if len(research_calls) == 1:
            assert payload["tools"]
            data["choices"][0]["message"]["content"] = "Paid web search narrative; https://example.com/r1"
        else:
            assert "tools" not in payload
            assert context["previous_response"].startswith("Paid web search narrative")
            assert "https://example.com/r1" in context["verified_search_urls_from_paid_responses"]
            data["citations"] = []
        return httpx.Response(200, json=data)
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert Pipeline(store, provider).execute(run) == "PREPARED", store.one("SELECT error FROM pipeline_runs")
    assert len(research_calls) == 2
    original_receipt = store.one("SELECT result_json FROM ai_calls WHERE call_key=?", (f"run:{run}:refresh:1:v0",))
    assert json.loads(original_receipt["result_json"])["choices"][0]["message"]["content"].startswith("Paid web search narrative")


def test_real_file_urls_are_extracted_but_require_visual_review(native_run):
    store, api, provider = native_run
    price_models(store)
    run = start_run(store, calendar_for(1), "", 100, confirmed=True)
    original = api.handler
    def handler(request):
        response = original(request)
        if request.method == "GET":
            return response
        payload = json.loads(request.content)
        user = payload["messages"][1]["content"]
        if payload["model"] == "visual-test" and isinstance(user, str):
            result = response.json()
            value = json.loads(result["choices"][0]["message"]["content"])
            value["assets"] = []
            value["cover"]["source_asset_id"] = None
            result["choices"][0]["message"]["content"] = json.dumps(value)
            return httpx.Response(200, json=result)
        return response
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert Pipeline(store, provider).execute(run) == "PREPARED", store.one("SELECT error FROM pipeline_runs")
    assert len([s for s,_,_ in api.calls if s == "image"]) == 2
    article = store.list_all()[0]
    assert store.active_image(article.id)["origin"] == "REAL_PHOTO"
    assert qa_article(store, article.id)["passed"]
    report = json.loads(store.one("SELECT payload_json FROM visual_results")["payload_json"])
    actual = next(a for a in report["assets"] if a["asset_id"].startswith("PAGE_"))
    assert actual["download_status"] == "downloaded" and actual["authenticity_status"] == "verified"


def test_four_workspaces_and_main_have_no_intermediate_recovery_buttons(tmp_path):
    config = replace(get_settings(), database_path=tmp_path / "new-ui.db", aitunnel_api_key=None)
    with patch("dzen_agent.config.get_settings", return_value=config):
        app = AppTest.from_file(str(PROJECT_ROOT / "dzen_agent/ui.py"), default_timeout=20).run()
        assert not app.exception
        assert app.sidebar.radio[0].options == ["Главная", "Материалы", "Настройки", "Журнал"]
        labels = [b.label for b in app.button]
        assert "Запустить агента" in labels
        assert not any("Продолжить" in label or "Восстанов" in label or "Проверить Research" in label for label in labels)
        for name in ("Материалы", "Настройки", "Журнал", "Главная"):
            app.sidebar.radio[0].set_value(name).run()
            assert not app.exception, name


def test_photo_candidates_skip_icons_and_diversify_observed_pages(monkeypatch):
    from dzen_agent.visuals import page_image_candidates
    pages = {
        "https://example.com/a": '<img src="/logo.jpg"><img width="30" src="/tiny.jpg">'
                                 '<img src="/a1.jpg"><img src="/a2.jpg"><img src="/a3.jpg">',
        "https://example.com/b": '<img src="/b1.jpg"><img src="/b2.jpg">',
    }
    monkeypatch.setattr("dzen_agent.visuals.fetch_public", lambda url, limit: pages[url].encode())
    assets = page_image_candidates(set(pages), limit=3, preferred_urls=set(pages))
    assert [str(a.source_image_url) for a in assets] == [
        "https://example.com/a1.jpg", "https://example.com/b1.jpg", "https://example.com/a2.jpg"]
    assert all(a.authenticity_status == "uncertain" and a.local_file_ref is None for a in assets)


def test_virtual_cover_is_owned_by_python_but_inline_references_stay_strict():
    from dzen_agent.execution import authoritative_fields
    from dzen_agent.editorial_contracts import ImageV1
    value = {"cover": {"source_asset_id": "REAL"}, "placements": [
        {"role":"cover", "asset_id":"VIRTUAL_COVER"}, {"role":"inline", "asset_id":"UNKNOWN"}]}
    normalized, repairs = authoritative_fields(value, ImageV1, {})
    assert normalized["placements"] == [{"role":"inline", "asset_id":"UNKNOWN"}]
    assert normalized["cover"]["source_asset_id"] == "REAL" and repairs
    assert len(value["placements"]) == 2
    from test_editorial_contracts_v1 import example
    from pydantic import ValidationError
    full = example("image")
    full["placements"] = [{"placement_id":"X", "asset_id":"UNKNOWN", "role":"inline",
        "target_section_heading":"Title", "anchor_text":"Text", "occurrence_index":0,
        "placement_rule":"after_paragraph", "visible_caption":None, "alt_text":"Photo", "status":"planned"}]
    full, _ = authoritative_fields(full, ImageV1, {})
    with pytest.raises(ValidationError, match="placements.asset_id"):
        ImageV1.model_validate(full)


def test_invalid_calendar_is_rejected_before_run_or_payment(native_run):
    store, api, _ = native_run
    for calendar in ({"2000-01-01":1}, {"2030-01-08":21,"2030-01-09":1}):
        with pytest.raises(ValueError):
            start_run(store, calendar, "", 100, confirmed=True)
    assert not store.rows("SELECT * FROM pipeline_runs") and not api.calls


def test_inline_gap_is_visible_and_document_change_invalidates_draft(native_run):
    from dzen_agent.services import article_fingerprint
    from pathlib import Path
    store, _, provider = native_run
    price_models(store)
    run = start_run(store, calendar_for(1), "", 100, confirmed=True)
    assert Pipeline(store, provider).execute(run) == "PREPARED"
    article = store.list_all()[0]
    visual = store.one("SELECT * FROM visual_results")
    payload = json.loads(visual["payload_json"])
    before = article_fingerprint(store, article.id)
    document = Path(payload["assembly"]["assembled_document_ref"])
    document.write_text(document.read_text(encoding="utf-8") + "\n",encoding="utf-8")
    assert article_fingerprint(store, article.id) != before
    payload["visual_plan"]["missing_sections"] = ["Missing section"]
    store.execute("UPDATE visual_results SET payload_json=? WHERE id=?", (json.dumps(payload),visual["id"]))
    result = qa_article(store, article.id)
    assert result["passed"] and not result["transfer_ready"] and result["warnings"]


def test_planner_scales_output_and_image_search_is_configured_separately(native_run):
    store, api, provider = native_run
    price_models(store)
    store.set_setting("max_output_tokens", 16000)
    store.set_setting("research_use_web_tool", False)
    store.set_setting("visual_use_web_tool", True)
    captured = []
    original = api.handler
    def handler(request):
        if request.method != "GET":
            payload = json.loads(request.content)
            captured.append(payload)
        return original(request)
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    run = start_run(store, calendar_for(21), "", 100, confirmed=True)
    assert Pipeline(store, provider).execute(run) == "PREPARED"
    planner = next(p for p in captured if p["model"] == "planner-test")
    assert planner["max_tokens"] == 42000
    research = next(p for p in captured if p["model"] == "research-test")
    visual = next(p for p in captured if p["model"] == "visual-test")
    assert "tools" not in research and visual["tools"]
