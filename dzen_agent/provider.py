"""AITUNNEL HTTP boundary, durable paid-call receipts and native account endpoints."""
from __future__ import annotations

import base64
import io
import json
import math
import time
from pathlib import Path

import httpx
from PIL import Image

from dzen_agent.errors import UserActionRequired, log_exception, redact
from dzen_agent.locking import exclusive_lock
from dzen_agent.store import Store, dumps, now_iso

BASE_URL = "https://api.aitunnel.ru/v1"
CATALOG_URL = "https://api.aitunnel.ru/public/aitunnel/models"


def number(value):
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("API вернул некорректное число.")
    return result


def parse_usage(payload: dict) -> dict:
    usage = payload.get("usage") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
        "completion_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
        "total_tokens": usage.get("total_tokens"),
        "cost_rub": number(usage.get("cost_rub", usage.get("cost"))),
        "balance_after": number(usage.get("balance")),
    }


def parse_account(balance: dict, key: dict, stats: dict) -> dict:
    budget = key.get("budget") or {}
    return {
        "balance": number(balance["balance"]),
        "budget_remaining": number(balance.get("budget", budget.get("remaining"))),
        "today_spend": number(stats["today_spend"]),
        "today_requests": int(stats["today_requests"]),
        "month_spend": number(stats["month_spend"]),
        "month_requests": int(stats["month_requests"]),
        "top_model_by_spend": stats.get("top_model_by_spend"),
        "fetched_at": now_iso(),
    }


def fetch_catalog(client: httpx.Client | None = None) -> dict:
    owned = client is None
    client = client or httpx.Client(timeout=30)
    try:
        result = {}
        for group in ("chat", "images"):
            response = client.get(f"{CATALOG_URL}/{group}")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Каталог AITUNNEL имеет неожиданный формат.")
            result[group] = {k: v for k, v in data.items()
                             if isinstance(v, dict) and not v.get("hidden")}
        return result
    finally:
        if owned:
            client.close()


def choose_defaults(catalog: dict) -> dict:
    chat = catalog.get("chat", {})
    images = catalog.get("images", {})
    def cheap(key):
        return float(chat[key].get("prompt_cost", 1e10)) + float(
            chat[key].get("completion_cost", 1e10))
    deepseek = [k for k, v in chat.items() if v.get("provider") == "deepseek"]
    text = min(deepseek or list(chat), key=cheap) if chat else ""
    research = [k for k, v in chat.items() if v.get("provider") == "perplexity"]
    research_model = min(research, key=cheap) if research else ""
    generation = [k for k, v in images.items() if v.get("supports_generation")]
    image = min(generation, key=lambda k: float(images[k].get("max_price_per_image", 1e10))) \
        if generation else ""
    return {"analytics_model": text, "planner_model": text, "writer_model": text,
            "research_model": research_model, "discovery_model": research_model, "image_model": image}


def save_image_bytes(raw: bytes, target: Path) -> str:
    if len(raw) > 30 * 1024 * 1024:
        raise ValueError("Изображение больше 30 МБ.")
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError("Слишком большое разрешение изображения.")
        image.load()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        image.convert("RGB").save(temporary, format="PNG")
        temporary.replace(target)
    return str(target.resolve())


class AITunnel:
    def __init__(self, store: Store, api_key: str | None, client: httpx.Client | None = None):
        self.store = store
        self.api_key = api_key
        self.client = client

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self.api_key:
            raise UserActionRequired("Добавьте AITUNNEL_API_KEY в локальный файл .env.")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.client:
            response = self.client.request(method, BASE_URL + path, headers=headers, **kwargs)
        else:
            with httpx.Client(timeout=httpx.Timeout(300, connect=20), follow_redirects=False) as c:
                response = c.request(method, BASE_URL + path, headers=headers, **kwargs)
        response.raise_for_status()
        return response

    def balance(self) -> dict:
        data = self._request("GET", "/aitunnel/balance").json()
        number(data["balance"])
        return data

    def account(self) -> dict:
        return parse_account(self.balance(), self._request("GET", "/aitunnel/key").json(),
                             self._request("GET", "/aitunnel/stats/summary").json())

    def guard(self) -> None:
        settings = self.store.settings()
        if settings["stop_requested"]:
            raise UserActionRequired("Агент остановлен. Возобновите работу в разделе «Автопилот».")
        costs = self.store.costs(days=30)
        if costs["total"] >= settings["monthly_soft_budget_rub"]:
            raise UserActionRequired("Достигнут локальный бюджет за 30 дней. Проверьте расходы.")
        balance = self.balance()
        if float(balance["balance"]) <= settings["stop_when_balance_below_rub"]:
            raise UserActionRequired("Баланс AITUNNEL ниже заданного порога. Генерация остановлена.")
        if balance.get("budget") is not None and float(balance["budget"]) <= 0:
            raise UserActionRequired("Бюджет текущего ключа AITUNNEL исчерпан.")

    def call(self, *, call_key: str, operation: str, model: str, payload: dict,
             run_id: int | None = None, article_id: int | None = None,
             image_path: Path | None = None, retry_limit: int | None = None) -> dict:
        with exclusive_lock(self.store.database_path.parent / "ai.lock"):
            previous = self.store.one("SELECT * FROM ai_calls WHERE call_key=?", (call_key,))
            if previous:
                if previous["status"] == "SUCCEEDED":
                    return json.loads(previous["result_json"])
                if previous["status"] in ("PENDING", "UNCERTAIN"):
                    raise UserActionRequired(
                        "Результат предыдущего AI-запроса неизвестен. Автоповтор отключён, "
                        "чтобы не списать деньги дважды. Проверьте историю AITUNNEL."
                    )
                if previous["status"] == "ERROR":
                    try:
                        previous_http = json.loads(previous["result_json"] or "{}").get("http_error", {}).get("http_status")
                    except (ValueError, AttributeError):
                        previous_http = None
                    # Preserve the existing explicit-resume policy for rate limiting only.
                    if previous_http != 429:
                        raise UserActionRequired(
                            "Предыдущий AI-запрос завершился ошибкой. Автоповтор отключён. "
                            "Проверьте диагностику и явно разрешите новый запрос в разделе восстановления. "
                            + (previous["error"] or ""))
            self.guard()
            retries = min(3, max(0, int(self.store.settings()["max_retries"])))
            if retry_limit is not None:
                retries = min(retries, max(0, retry_limit))
            for attempt in range(retries + 1):
                self.store.execute(
                    "INSERT INTO ai_calls(call_key,status,created_at,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(call_key) DO UPDATE SET status='PENDING',updated_at=excluded.updated_at",
                    (call_key, "PENDING", now_iso(), now_iso()),
                )
                response, data, usage, request_id = None, {}, {}, None
                try:
                    path = "/images/generations" if image_path else "/chat/completions"
                    response = self._request("POST", path, json={**payload, "model": model})
                    request_id = response.headers.get("x-request-id")
                    data = response.json()
                    request_id = request_id or data.get("id")
                    usage = parse_usage(data)
                    if image_path:
                        raw = base64.b64decode(data["data"][0]["b64_json"], validate=True)
                        result = {"path": save_image_bytes(raw, image_path), "usage": data.get("usage")}
                    else:
                        # Keep the paid response for replay/validation; no secrets or base64.
                        result = {"choices": data["choices"], "usage": data.get("usage"),
                                  "citations": data.get("citations", []),
                                  "search_results": data.get("search_results", []),
                                  "search_metadata": {"citations_field_present": "citations" in data,
                                                      "search_results_field_present": "search_results" in data},
                                  "model": data.get("model", model)}
                        if operation == "ANALYTICS":
                            result["analytics_request"] = {"model": model, **payload}
                    with self.store._connect() as conn:
                        self._usage(conn, call_key, operation, data.get("model", model), run_id,
                                    article_id, usage, True, None, request_id)
                        conn.execute(
                            "UPDATE ai_calls SET status='SUCCEEDED',result_json=?,error=NULL,"
                            "updated_at=? WHERE call_key=?", (dumps(result), now_iso(), call_key),
                        )
                    return result
                except Exception as error:
                    # A timeout/5xx or invalid paid response can have incurred a charge.
                    status_code = error.response.status_code if isinstance(
                        error, httpx.HTTPStatusError) else None
                    uncertain = status_code is None or status_code >= 500
                    if isinstance(error, UserActionRequired):
                        uncertain = False
                    message = self._human_error(error, status_code)
                    diagnostic = None
                    if isinstance(error, httpx.HTTPStatusError):
                        from dzen_agent.http_diagnostics import error_detail, http_diagnostic
                        diagnostic = http_diagnostic(error, model=model, payload=payload, operation=operation,
                                                     call_key=call_key, api_key=self.api_key)
                        request_id = diagnostic["request_id"]
                        if status_code == 400:
                            message = "AITUNNEL: ошибка HTTP 400. " + error_detail(diagnostic) + (
                                " Подробности сохранены в локальный журнал. Автоповтор отключён.")
                    # Do not discard billing information if an HTTP error happens to include it.
                    if isinstance(error, httpx.HTTPStatusError):
                        try:
                            error_data = error.response.json()
                            if isinstance(error_data, dict) and isinstance(error_data.get("usage", {}), dict):
                                usage = parse_usage(error_data)
                        except (ValueError, TypeError):
                            pass
                    log_exception(error, f"AI {operation}; model={model}; request_id={request_id}"
                                  + ("\nHTTP diagnostic: " + dumps(diagnostic) if diagnostic else ""))
                    with self.store._connect() as conn:
                        self._usage(conn, call_key, operation, model, run_id, article_id,
                                    usage, False, message, request_id)
                        conn.execute("UPDATE ai_calls SET status=?,error=?,updated_at=? WHERE call_key=?",
                                     ("UNCERTAIN" if uncertain else "ERROR", message,
                                      now_iso(), call_key))
                        if diagnostic:
                            conn.execute("UPDATE ai_calls SET result_json=? WHERE call_key=?",
                                         (dumps({"http_error": diagnostic}), call_key))
                    self.store.log(operation, message, {"model": model, "request_id": request_id,
                                                       "http_status": status_code})
                    if status_code == 429 and attempt < retries:
                        time.sleep(min(2 ** attempt, 4))
                        self.guard()
                        continue
                    raise UserActionRequired(message) from None
            raise RuntimeError("Не удалось завершить AI-запрос.")

    @staticmethod
    def _human_error(error: Exception, code: int | None) -> str:
        if code in (401, 403):
            return "AITUNNEL отклонил ключ или доступ к модели. Проверьте ключ и его разрешения."
        if code == 402:
            return "AITUNNEL сообщает о недостаточном балансе или бюджете ключа."
        if code == 429:
            return "AITUNNEL временно ограничил запросы. Повторите позже."
        if code:
            return f"AITUNNEL: ошибка HTTP {code}. Подробности сохранены в локальный журнал."
        if isinstance(error, UserActionRequired):
            return redact(str(error))
        return "AI-запрос не завершён однозначно. Проверьте журнал и историю расходов AITUNNEL."

    @staticmethod
    def _usage(conn, key, operation, model, run_id, article_id, usage, success, error, request_id):
        conn.execute(
            "INSERT INTO usage_events(timestamp,pipeline_run_id,article_id,operation,model,"
            "prompt_tokens,completion_tokens,total_tokens,cost_rub,balance_after,success,error,"
            "request_id,call_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), run_id, article_id, operation, model, usage.get("prompt_tokens"),
             usage.get("completion_tokens"), usage.get("total_tokens"), usage.get("cost_rub"),
             usage.get("balance_after"), int(success), error, request_id, key),
        )
