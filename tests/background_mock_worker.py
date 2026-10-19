"""Test-only worker: same Pipeline, isolated DB, all model HTTP routed to MockTransport."""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from PIL import Image
from unittest.mock import patch
from test_editorial_contracts_v1 import NativeAPI
from dzen_agent.pipeline.orchestrator import Pipeline
from dzen_agent.provider import AITunnel
from dzen_agent.store import Store


if __name__ == "__main__":
    database = Path(sys.argv[1]).resolve()
    assert database.name.startswith("ux-test-")
    api = NativeAPI()
    picture = io.BytesIO()
    Image.new("RGB", (800,450), "#315d82").save(picture,"PNG")
    store = Store(database)
    def fetch(url, limit=None):
        return picture.getvalue() if url.endswith(".jpg") else b'<img src="https://example.com/still.jpg">'
    with patch("dzen_agent.visuals.fetch_public", fetch):
        provider = AITunnel(store, "mock-key", httpx.Client(transport=httpx.MockTransport(api.handler)))
        print(Pipeline(store, provider).execute(int(sys.argv[2])), flush=True)
