"""Local HTTP diagnostics without authentication headers or secret values."""
from __future__ import annotations

import json
import re

from dzen_agent.errors import redact

SENSITIVE = ("authorization", "apikey", "password", "passwd", "secret", "cookie")


def sanitize(value, *, secrets=()):
    def clean(item):
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                sensitive = (normalized == "headers" or normalized.endswith("token")
                             or any(word in normalized for word in SENSITIVE))
                result[key] = "[СКРЫТО]" if sensitive else clean(child)
            return result
        if isinstance(item, (list, tuple)):
            return [clean(child) for child in item]
        if isinstance(item, str):
            for secret in secrets:
                if secret:
                    item = item.replace(secret, "[СКРЫТО]")
            item = redact(item)
            item = re.sub(r"(?i)Basic\s+[^\s\"']+", "Basic [СКРЫТО]", item)
            # Providers sometimes embed echoed credentials inside a plain-text/raw error.
            return re.sub(r'''(?i)(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|client[_-]?secret|authorization|cookie|set-cookie)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
                          lambda m: m[1] + "[СКРЫТО]", item)
        return item
    return clean(value)


def http_diagnostic(error, *, model, payload, operation, call_key, api_key=None):
    response = error.response
    try:
        body = response.json()
    except ValueError:
        body = response.text
    diagnostic = sanitize({"http_status": response.status_code, "method": error.request.method,
        "endpoint": error.request.url.path,
        "model": model, "operation": operation, "call_key": call_key,
        "request_id": response.headers.get("x-request-id") or (body.get("id") if isinstance(body, dict) else None),
        "request": {**payload, "model": model}, "response_body": body}, secrets=(api_key,))
    # Sanitize before truncation, so a cut through a credential cannot leak a prefix.
    serialized = json.dumps(diagnostic["response_body"], ensure_ascii=False)
    if len(serialized) > 32768:
        diagnostic["response_body"] = serialized[:32768]
        diagnostic["response_body_truncated"] = True
    return diagnostic


def error_detail(diagnostic):
    body = diagnostic["response_body"]
    if isinstance(body, dict):
        error = body.get("error", body.get("detail", body))
        if isinstance(error, dict):
            error = error.get("message", error)
        detail = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
    else:
        detail = str(body)
    return detail[:1000]
