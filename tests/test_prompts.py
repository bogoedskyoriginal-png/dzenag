import pytest

from dzen_agent.prompt_service import Prompts, render
from dzen_agent.store import Store


def test_renderer_rejects_missing_and_never_evaluates_code():
    assert render("{{topic}}", {"topic": "{{statistics}}"}) == "{{statistics}}"
    with pytest.raises(ValueError, match="statistics"):
        render("{{statistics}}", {})
    with pytest.raises(ValueError):
        render("{{__import__('os')}}", {})


def test_versions_restore_without_rewriting_history(tmp_path):
    store = Store(tmp_path / "test.db")
    store.initialize()
    prompts = Prompts(store)
    prompts.seed()
    prompts.seed()
    first = prompts.active("RESEARCH")
    prompts.save("RESEARCH", "My exact prompt", "Text\n {{topic}} ")
    assert prompts.active("RESEARCH")["content"] == "Text\n {{topic}} "
    prompts.restore(first["id"])
    assert prompts.active("RESEARCH")["version"] == 3
    assert prompts.active("RESEARCH")["content"] == first["content"]
