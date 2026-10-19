"""Conservative local repair of Research references; original receipts remain immutable."""
from __future__ import annotations

import copy
import json
import re
import unicodedata

from dzen_agent.editorial_contracts import ResearchV1

IDENTITIES = {"sources": "source_id", "facts": "fact_id", "quotes": "quote_id", "works": "work_id",
              "people": "person_id", "excluded_claims": "claim_id", "article_structure": "section_id",
              "contradictions": "contradiction_id"}
TARGETS = {"source_ids": ("sources",), "fact_ids": ("facts",), "quote_ids": ("quotes",),
           "work_ids": ("works",), "related_work_ids": ("works",), "person_ids": ("people",),
           "supports": ("facts", "quotes"), "approved_fact_ids": ("facts",),
           "approved_quote_ids": ("quotes",), "forbidden_claim_ids": ("excluded_claims",)}


def canonical(value):
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFKC", value).strip().casefold()
    match = re.fullmatch(r"([a-z]+)[-_ ]?(\d+)", value)
    return (match[1], int(match[2])) if match else value


def reference_entries(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in TARGETS:
                yield value, key, item, (*path, key)
            else:
                yield from reference_entries(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from reference_entries(item, (*path, index))


def identifiers(data):
    return {group: [i[field] for i in data.get(group, []) if isinstance(i, dict) and isinstance(i.get(field), str)]
            for group, field in IDENTITIES.items()}


def inspect_references(data):
    ids = identifiers(data)
    issues = []
    for group, values in ids.items():
        for index, value in enumerate(values):
            if values.count(value) > 1:
                issues.append({"path": [group, index, IDENTITIES[group]], "actual": value,
                               "expected": "unique identifier", "type": "duplicate_identity"})
    for _, key, values, path in reference_entries(data):
        expected = sorted({v for group in TARGETS[key] for v in ids[group]})
        if key == "approved_fact_ids":
            expected = sorted(i["fact_id"] for i in data["facts"] if isinstance(i, dict) and i.get("usage") in ("direct", "attributed") and i.get("source_ids"))
        if key == "approved_quote_ids":
            expected = sorted(i["quote_id"] for i in data["quotes"] if isinstance(i, dict) and i.get("usage") in ("direct", "attributed") and i.get("source_ids") and i.get("verification_status") == "verified")
        if not isinstance(values, list):
            issues.append({"path": list(path), "actual": values, "expected": expected, "type": "reference_list_type"})
            continue
        for index, value in enumerate(values):
            if value not in expected:
                issues.append({"path": [*path, index], "actual": value, "expected": expected, "type": "unknown_reference"})
    return issues


class ResearchReferenceError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("; ".join(f"{'.'.join(map(str, i['path']))}: {i['actual']!r}; допустимые ID: {i['expected']}" for i in issues))


def parse_research(content):
    raw = json.loads(content)
    if not isinstance(raw, dict) or any(not isinstance(raw.get(g), list) for g in IDENTITIES):
        return ResearchV1.model_validate(raw), {"issues": [], "repairs": [], "quarantined": []}
    data = copy.deepcopy(raw)
    report = {"issues": inspect_references(raw), "repairs": [], "quarantined": []}
    # Ambiguous declarations cannot be repaired by guessing which URL an ID meant.
    if any(i["type"] == "duplicate_identity" for i in report["issues"]):
        raise ResearchReferenceError(report["issues"])
    # Normalize declarations only if identity remains unique. Collisions stay strict errors.
    for group, field in IDENTITIES.items():
        items = data[group]
        ids = [i.get(field) for i in items if isinstance(i, dict)]
        if all(isinstance(v, str) and v.strip() for v in ids) and len({v.strip() for v in ids}) == len(ids):
            for index, item in enumerate(items):
                if item[field] != item[field].strip():
                    report["repairs"].append({"path": [group, index, field], "before": item[field], "after": item[field].strip()})
                    item[field] = item[field].strip()
    ids = identifiers(data)
    for parent, key, values, path in reference_entries(data):
        if isinstance(values, str):
            parent[key] = values = [values]
            report["repairs"].append({"path": list(path), "before": values[0], "after": values[:]})
        if not isinstance(values, list):
            continue
        allowed = {v for group in TARGETS[key] for v in ids[group]}
        for index, value in enumerate(values):
            if not isinstance(value, str):
                continue
            matches = [v for v in allowed if canonical(v) == canonical(value)]
            if value not in allowed and len(matches) == 1:
                report["repairs"].append({"path": [*path, index], "before": value, "after": matches[0]})
                values[index] = matches[0]
    removed = set()
    evidence_changed = False
    # Remove complete unsupported assertions, then cascade through dependent assertions.
    while True:
        issues = inspect_references(data)
        owners = {}
        for issue in issues:
            path = issue["path"]
            if len(path) >= 3 and path[0] in ("facts", "quotes", "timeline", "works", "people", "contradictions", "article_structure"):
                owners.setdefault((path[0], path[1]), []).append(issue)
        if not owners:
            break
        for group, index in sorted(owners, key=lambda p: (p[0], -p[1])):
            item = data[group].pop(index)
            report["quarantined"].append({"path": [group, index], "item": item, "issues": owners[group, index]})
            field = IDENTITIES.get(group)
            if field and isinstance(item, dict) and isinstance(item.get(field), str):
                removed.add(item[field])
            evidence_changed |= group != "timeline" or bool(item.get("fact_ids"))
        # Bibliographic edges and handoff lists cannot continue referencing quarantined assertions.
        for parent, key, values, path in reference_entries(data):
            if key in ("supports", "approved_fact_ids", "approved_quote_ids") and isinstance(values, list):
                kept = [v for v in values if v not in removed]
                if kept != values:
                    report["repairs"].append({"path": list(path), "before": values[:], "after": kept})
                    parent[key] = kept
    remaining = inspect_references(data)
    if remaining:
        raise ResearchReferenceError(remaining)
    if report["quarantined"]:
        data["research_limitations"].append("Локально изолированы элементы с неподтверждёнными ссылками; не использовать их утверждения: "
            + "; ".join(dumps_item(q["item"]) for q in report["quarantined"]))
        if evidence_changed or not data["writer_handoff"]["approved_fact_ids"]:
            data["status"] = "partial"
            data["research_status"] = "editorial_review_required"
            data["writer_handoff"]["sufficiency"]["coverage"] = "limited"
        elif data["status"] == "ok":
            data["status"] = "partial"
    return ResearchV1.model_validate(data), report


def dumps_item(item):
    return json.dumps(item, ensure_ascii=False)
