import json

import httpx
import pytest

from dzen_agent.provider import AITunnel, choose_defaults, parse_account, parse_usage
from dzen_agent.store import Store


def test_native_account_and_optional_cost():
    result = parse_account({"balance": "500", "budget": 12},
                           {"budget": {"remaining": 13}},
                           {"today_spend": 1, "today_requests": 2,
                            "month_spend": 3, "month_requests": 4})
    assert result["balance"] == 500
    assert result["budget_remaining"] == 12
    assert parse_usage({})["cost_rub"] is None
    assert parse_usage({"usage": {"cost_rub": 0, "balance": 0}})["cost_rub"] == 0


def test_paid_call_replay_and_account_routes(tmp_path):
    store = Store(tmp_path / "test.db")
    store.initialize()
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer test-key"
        if request.url.path.endswith("balance"):
            return httpx.Response(200, json={"balance": 300})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{}'}}],
                                        "usage": {"cost_rub": 2, "balance": 298}})

    provider = AITunnel(store, "test-key", httpx.Client(transport=httpx.MockTransport(handler)))
    args = dict(call_key="one", operation="WRITE", model="test", payload={"messages": []})
    assert provider.call(**args) == provider.call(**args)
    assert calls.count("/v1/chat/completions") == 1
    assert store.costs()["total"] == 2
    assert "test-key" not in json.dumps(store.rows("SELECT * FROM ai_calls"))


def test_timeout_never_retries_paid_call(tmp_path):
    store = Store(tmp_path / "test.db")
    store.initialize()
    calls = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"balance": 300})
        calls.append(1)
        raise httpx.ReadTimeout("unknown", request=request)

    p = AITunnel(store, "test-key", httpx.Client(transport=httpx.MockTransport(handler)))
    for _ in range(2):
        with pytest.raises(RuntimeError):
            p.call(call_key="same", operation="WRITE", model="test", payload={})
    assert len(calls) == 1


def test_defaults_come_from_catalog():
    catalog = {"chat": {"new-cheap": {"provider": "deepseek", "prompt_cost": 1,
                                     "completion_cost": 1}}, "images": {}}
    assert choose_defaults(catalog)["writer_model"] == "new-cheap"
