"""Diagnose incomplete paid responses before attempting schema validation."""
from dzen_agent.errors import UserActionRequired


def require_complete_response(response: dict) -> None:
    choices = response.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return  # The caller reports malformed envelopes using its normal schema diagnostic.
    choice = choices[0]
    if "length" not in (choice.get("finish_reason"), choice.get("native_finish_reason")):
        return
    usage = response.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    raise UserActionRequired(
        "Ответ обрезан по лимиту токенов (finish_reason=length). "
        f"Выходных токенов: {usage.get('completion_tokens', 'неизвестно')}; "
        f"из них reasoning: {details.get('reasoning_tokens', 'неизвестно')}. "
        "JSON нельзя считать полным, даже если его начало корректно. "
        "Оплаченный ответ и расходы сохранены. Автоматический платный повтор отключён; "
        "новый запрос требует вашего явного разрешения.")
