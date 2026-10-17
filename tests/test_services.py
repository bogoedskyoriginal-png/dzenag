import base64
import io
import json

import httpx
import pytest
from PIL import Image

from dzen_agent.prompt_service import Prompts
from dzen_agent.provider import AITunnel
from dzen_agent.schemas import Research
from dzen_agent.services import ContentService, ground_research, week_slots
from dzen_agent.store import Store


def test_grounding_rejects_invented_urls():
    research = Research.model_validate({"status": "sufficient", "summary": "summary",
        "key_facts": ["fact"], "claims": [{"text": "fact", "source_ids": ["a"]}],
        "sources": [{"id": "a", "url": "https://example.com/a", "title": "A"}],
        "contradictions": [], "uncertainty": [], "suggested_angles": []})
    assert ground_research(research, {}).status == "insufficient_research"
    assert ground_research(research, {}).sources == []


def test_schedule_respects_settings():
    settings = {"publication_days": [0, 2], "publication_time": "10:30", "max_articles_per_week": 2}
    assert week_slots("2026-10-19", settings) == ["2026-10-19T10:30:00+03:00",
                                                "2026-10-21T10:30:00+03:00"]
    settings["max_articles_per_week"] = 3
    with pytest.raises(ValueError):
        week_slots("2026-10-19", settings)


def test_image_saved_locally_and_user_upload_wins(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.initialize()
    Prompts(store).seed()
    store.set_setting("image_model", "test")
    article = store.create("topic", "title", "body")
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(buffer, "PNG")
    calls = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 300})
        calls.append(1)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(
            buffer.getvalue()).decode()}], "usage": {"cost_rub": 1}})

    provider = AITunnel(store, "key", httpx.Client(transport=httpx.MockTransport(handler)))
    service = ContentService(store, provider)
    service.image(article.id, "img1")
    assert store.costs(article_id=article.id)["total"] == 1
    assert 'b64_json' not in json.dumps(store.rows("SELECT * FROM ai_calls"))
    uploaded = service.upload_image(article.id, buffer.getvalue())
    assert service.image(article.id, "img2") == uploaded
    assert len(calls) == 1
