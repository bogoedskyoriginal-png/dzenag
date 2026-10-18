"""Identity of an article stage's inputs, shared by execution and local QA."""
import hashlib

from dzen_agent.store import dumps


def item_fingerprint(store, concept, stage, article_id=None):
    content = store.get(article_id) if article_id else None
    identity = [concept["revision"], concept["research_version"], concept["topic"],
                concept["research_id"] if stage != "RESEARCH" else None,
                [content.title, content.body] if content else None]
    return hashlib.sha256(dumps(identity).encode()).hexdigest()
