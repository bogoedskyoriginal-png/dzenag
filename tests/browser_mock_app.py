"""Browser-only fixture app. Refuses the real DB and blocks all outbound HTTP.

Run via Streamlit with DZEN_UI_TEST_DATABASE pointing to a ux-test-*.db fixture.
No production code imports this module.
"""
import importlib.util
import io
import json
import os
import runpy
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
from PIL import Image

import dzen_agent.config as config_module
import dzen_agent.scheduler as scheduler_module
import dzen_agent.visuals as visuals_module
from dzen_agent.config import PROJECT_ROOT, get_settings
from dzen_agent.pipeline.orchestrator import Pipeline
from dzen_agent.browser.scheduling import DzenScheduling
from dzen_agent.pipeline.run import PublishingOnlyProvider, assert_publish_ready
from dzen_agent.provider import AITunnel
from dzen_agent.store import Store, dumps

database = Path(os.environ["DZEN_UI_TEST_DATABASE"]).resolve()
original_config = get_settings()
real_database = getattr(config_module, "_ui_test_real_database", original_config.database_path.resolve())
config_module._ui_test_real_database = real_database
assert database.name.startswith("ux-test-") and database != real_database
config = replace(original_config, database_path=database, aitunnel_api_key="mock-key")
spec = importlib.util.spec_from_file_location("native_ui_fixture", PROJECT_ROOT / "tests/test_editorial_contracts_v1.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
studio_spec = importlib.util.spec_from_file_location("studio_ui_fixture", PROJECT_ROOT / "tests/test_publishing_pipeline.py")
studio_fixture = importlib.util.module_from_spec(studio_spec)
studio_spec.loader.exec_module(studio_fixture)
api = fixture.NativeAPI()
base_topics = list(fixture.TOPICS)
flags = database.with_suffix(".flags.json")
if flags.exists():
    for key, value in json.loads(flags.read_text("utf-8")).items():
        setattr(api, key, value)
picture = io.BytesIO()
Image.new("RGB", (800, 450), "#3c6180").save(picture, "PNG")
original_call = getattr(AITunnel, "_ui_test_base_call", AITunnel.call)
AITunnel._ui_test_base_call = original_call


def mock_call(self, **kwargs):
    role = {"ANALYTICS": "analytics", "DISCOVERY": "discovery", "PLANNING": "planner", "RESEARCH": "research",
            "WRITE": "writer", "EDITOR_CHAT": "editor", "IMAGE_SEARCH": "visual", "IMAGE_INSPECT": "visual"}[kwargs["operation"]]
    return original_call(self, **{**kwargs, "model": role + "-test"})


def request(self, method, path, **kwargs):
    current_flags = json.loads(flags.read_text("utf-8")) if flags.exists() else {}
    api.fail_json = current_flags.get("fail_json")
    api.fail_research = current_flags.get("fail_research")
    api.no_images = current_flags.get("no_images", False)
    payload = kwargs.get("json", {})
    if payload.get("messages"):
        content = payload["messages"][1]["content"]
        content = content[0]["text"] if isinstance(content, list) else content
        try:
            context = json.loads(content)
            run_id = context.get("run_context", {}).get("run_id", 1)
            prefix = {2: "Архивы киномузея: ", 3: "Практика монтажной мастерской: "}.get(run_id, "")
            fixture.TOPICS = [prefix + topic for topic in base_topics]
        except ValueError:
            pass
    req = httpx.Request(method, "https://mock.invalid" + path, json=payload)
    if path.endswith("/key"):
        response = httpx.Response(200, json={"budget": {"remaining": 500}})
    elif path.endswith("/stats/summary"):
        response = httpx.Response(200, json={"today_spend": 0, "today_requests": 0, "month_spend": 0, "month_requests": 0})
    elif payload.get("model") == "editor-test":
        context = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])
        reply = {"message": "Предлагаю уточнить ракурс первой темы", "proposals": [{"type": "update_concept",
            "concept_id": context["plan"][0]["concept_id"], "angle": "Как реставраторы сохраняют исходный цвет плёнки",
            "reason": "Конкретная профессиональная история"}]}
        response = httpx.Response(200, json={"choices": [{"message": {"content": dumps(reply)}}], "usage": {"cost_rub": .05}})
    else:
        response = api.handler(req)
    response.request = req
    response.raise_for_status()
    return response


def launch(run_id, **kwargs):
    store = Store(database)
    if flags.exists() and json.loads(flags.read_text("utf-8")).get("simulate_running"):
        store.execute("UPDATE pipeline_runs SET status='RUNNING' WHERE id=?", (run_id,))
        return 123
    if kwargs.get("send"):
        assert_publish_ready(store, run_id)
        Pipeline(store, PublishingOnlyProvider(), dzen=DzenScheduling(store, studio_fixture.MockStudio(store))).execute(run_id, allow_dzen=True)
    else:
        Pipeline(store, AITunnel(store, "mock-key")).execute(run_id, analytics_only=kwargs.get("analytics_only", False))
    return 123


def fetch(url, limit=None):
    return picture.getvalue() if url.endswith(".jpg") else b'<img src="https://example.com/still.jpg">'


# Patches MUST survive the initial script: Streamlit fragments run outside runpy later.
# Two independent hard barriers prevent any real network request or background child.
def forbidden(*args, **kwargs):
    raise AssertionError("Outbound HTTP and child processes are forbidden in browser tests")


config_module.get_settings = lambda: config
AITunnel._request = request
AITunnel.call = mock_call
scheduler_module.launch_pipeline = launch
visuals_module.fetch_public = fetch
httpx.Client.request = forbidden
subprocess.Popen = forbidden
runpy.run_path(str(PROJECT_ROOT / "dzen_agent/ui.py"), run_name="__main__")
