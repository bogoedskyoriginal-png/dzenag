"""Strict Discovery 1.0 validation with per-candidate quarantine, never invented repairs."""
from __future__ import annotations

import json
from collections import Counter

from pydantic import ValidationError

from dzen_agent.editorial_contracts import DiscoveryV1, Idea, RejectedIdea


def candidate_errors(candidate, error, index):
    issues = [{"loc": ["candidates", index, *e["loc"]], "type": e["type"], "message": e["msg"]}
              for e in error.errors(include_input=False, include_context=False, include_url=False)]
    if not isinstance(candidate, dict):
        return issues
    sources = candidate.get("sources")
    if not isinstance(sources, list):
        return issues
    ids = {s["source_id"] for s in sources if isinstance(s, dict) and isinstance(s.get("source_id"), str)}
    for field, entries in (("anchor_facts", candidate.get("anchor_facts")),
                           ("selection_feasibility.verified_subjects", (candidate.get("selection_feasibility") or {}).get("verified_subjects")
                            if isinstance(candidate.get("selection_feasibility"), dict) else None)):
        if not isinstance(entries, list):
            continue
        for number, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("source_ids"), list):
                continue
            for position, source_id in enumerate(entry["source_ids"]):
                if isinstance(source_id, str) and source_id not in ids:
                    issues.append({"loc": ["candidates", index, *field.split("."), number, "source_ids", position],
                        "type": "unknown_source_id", "message": f"Источник {source_id} отсутствует внутри кандидата.",
                        "source_id": source_id, "available_source_ids": sorted(ids)})
    return issues


def reject(result, candidate, reason, *, reason_code="other"):
    topic = candidate.get("topic") if isinstance(candidate, dict) else None
    idea_id = candidate.get("idea_id") if isinstance(candidate, dict) else None
    result.rejected_ideas.append(RejectedIdea(topic=topic if isinstance(topic, str) else "Кандидат без корректной темы",
        reason_code=reason_code, reason=f"Локальная проверка {idea_id or ''}: {reason}", related_article_ids=[]))


def parse_discovery(content):
    """Validate the entire envelope first; only independent candidate errors are isolatable."""
    raw = json.loads(content)
    if not isinstance(raw, dict) or not isinstance(raw.get("candidates"), list):
        return DiscoveryV1.model_validate(raw), []  # Fail with the original strict schema.
    result = DiscoveryV1.model_validate({**raw, "candidates": []})
    counts = Counter(c.get("idea_id") for c in raw["candidates"]
                     if isinstance(c, dict) and isinstance(c.get("idea_id"), str))
    issues = []
    for index, candidate in enumerate(raw["candidates"]):
        try:
            idea = Idea.model_validate(candidate)
        except ValidationError as error:
            errors = candidate_errors(candidate, error, index)
            issues.extend(errors)
            reject(result, candidate, "; ".join(e["message"] for e in errors), reason_code="unreliable_claim")
            continue
        if counts[idea.idea_id] != 1:
            issues.append({"loc": ["candidates", index, "idea_id"], "type": "duplicate_idea_id",
                           "message": f"Неоднозначный idea_id: {idea.idea_id}."})
            reject(result, candidate, "Повтор idea_id; ни один из неоднозначных кандидатов не принят.")
            continue
        result.candidates.append(idea)
    if issues and result.status == "ok":
        result.status = "partial"
    # Keep the public contract fully valid, including unique IDs and all original requirements.
    return DiscoveryV1.model_validate(result.model_dump(mode="json")), issues
