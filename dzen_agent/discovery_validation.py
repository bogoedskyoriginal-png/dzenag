"""Strict Discovery 1.0 validation with per-candidate quarantine, never invented repairs."""
from __future__ import annotations

import json
import copy
from collections import Counter

from pydantic import ValidationError

from dzen_agent.editorial_contracts import DiscoveryV1, Idea, RejectedIdea


def repair_candidate(candidate, index):
    """Only representation changes: no missing sources/facts, fuzzy matching or ID guessing."""
    value, changes = copy.deepcopy(candidate), []
    if not isinstance(value, dict):
        return value, changes
    def change(container, key, new, path):
        old = container[key]
        if old != new:
            container[key] = new
            changes.append({"type": "technical_repair", "loc": ["candidates", index, *path],
                            "message": "Однозначная техническая нормализация", "before": old, "after": new})
    for field in ("sources", "anchor_facts"):
        if isinstance(value.get(field), dict):
            change(value, field, [value[field]], [field])
    sources = value.get("sources", [])
    if not isinstance(sources, list):
        return value, changes
    ids = [s.get("source_id") for s in sources if isinstance(s, dict) and isinstance(s.get("source_id"), str)]
    trimmed = [s.strip() for s in ids]
    if all(trimmed) and len(set(trimmed)) == len(trimmed):
        for n, source in enumerate(sources):
            if isinstance(source, dict) and isinstance(source.get("source_id"), str):
                change(source, "source_id", source["source_id"].strip(), ["sources", n, "source_id"])
        ids = trimmed
    lookup = {}
    for source_id in ids:
        lookup.setdefault(source_id.strip().casefold(), []).append(source_id)
    entries = [("anchor_facts", value.get("anchor_facts", []))]
    selection = value.get("selection_feasibility")
    if isinstance(selection, dict):
        entries.append(("selection_feasibility.verified_subjects", selection.get("verified_subjects", [])))
    for field, items in entries:
        if not isinstance(items, list):
            continue
        for n, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            path = [*field.split("."), n, "source_ids"]
            if isinstance(item.get("source_ids"), str):
                change(item, "source_ids", [item["source_ids"]], path)
            references = item.get("source_ids")
            if not isinstance(references, list):
                continue
            for position, reference in enumerate(references):
                matches = lookup.get(reference.strip().casefold(), []) if isinstance(reference, str) else []
                if reference not in ids and len(matches) == 1:
                    change(references, position, matches[0], [*path, position])
    scores = value.get("scores")
    if isinstance(scores, dict):
        for field, score in list(scores.items()):
            if isinstance(score, str) and score.strip() in {str(n) for n in range(6)}:
                change(scores, field, int(score), ["scores", field])
    return value, changes


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
        candidate, changes = repair_candidate(candidate, index)
        issues.extend(changes)
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
