from __future__ import annotations

import json
from datetime import datetime

import streamlit as st

from dzen_agent.editorial import EditorialChat
from dzen_agent.plan_review import apply_proposals, editing_reason, proposal_preview, reject_proposal
from dzen_agent.provider import AITunnel
from dzen_agent.store import MSK

FIELD_NAMES = {"topic": "Тема", "working_title": "Рабочий заголовок", "angle": "Ракурс",
               "why_now": "Почему сейчас", "audience_value": "Польза читателю", "thesis": "Тезис",
               "key_points": "Ключевые пункты", "scheduled_at": "Дата и время",
               "suggested_publication_date": "Предложенная дата", "suggested_publication_time": "Предложенное время"}


def _format(value, field):
    if field == "scheduled_at":
        return datetime.fromisoformat(value).astimezone(MSK).strftime("%d.%m.%Y %H:%M (Москва)")
    return "\n".join(value) if isinstance(value, list) else str(value or "—")


def grooming_panel(store, config, plan_id):
    from dzen_agent.ui_pages import action

    st.subheader("Редакторский чат")
    st.caption("Обсуждение с AI оплачивается. Чат ничего не меняет без кнопки «Применить».")
    chat = EditorialChat(store, AITunnel(store, config.aitunnel_api_key))
    blocked = editing_reason(store, plan_id)
    messages = chat.messages(plan_id)
    for message in messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message["error"]:
                st.warning(message["error"])
            if message["role"] == "user" and message["status"] in ("ERROR", "PENDING"):
                st.caption("Запрос сохранён. Восстановление использует прежний ответ, если он уже оплачен.")
                if st.button("Восстановить ответ", key=f"editor-retry-{message['id']}",
                             disabled=not config.aitunnel_api_key or store.settings()["stop_requested"]):
                    if action("Восстанавливаю ответ редактора…", lambda: chat.retry(message["id"])):
                        st.rerun()
            suggestions = json.loads(message["suggestions_json"])
            pending = [i for i, item in enumerate(suggestions) if item["status"] == "PENDING"]
            for index, item in enumerate(suggestions):
                proposal = item["proposal"]
                with st.container(border=True):
                    st.markdown(f"**Предложение для концепта №{proposal['concept_id']}**")
                    st.write("Причина:", proposal["reason"])
                    if item["status"] != "PENDING":
                        st.caption("Изменение применено." if item["status"] == "APPLIED" else "Предложение отклонено.")
                        continue
                    st.caption("Изменение предложено — данные ещё не изменены.")
                    stale = False
                    try:
                        preview = proposal_preview(store, plan_id, item)
                        for change in preview["diff"]:
                            label = FIELD_NAMES.get(change["field"], change["field"])
                            st.markdown(f"**{label}**")
                            st.write("Было:", _format(change["before"], change["field"]))
                            st.write("Стало:", _format(change["after"], change["field"]))
                        if preview["needs_research"]:
                            st.info("Для изменённой темы потребуется новое исследование.")
                    except ValueError as error:
                        stale = True
                        st.warning(str(error))
                    apply_column, reject_column = st.columns(2)
                    if apply_column.button("Применить", key=f"editor-apply-{message['id']}-{index}",
                                           disabled=bool(blocked) or stale):
                        def apply_one(message_id=message["id"], proposal_index=index):
                            apply_proposals(store, message_id, [proposal_index])
                            return True
                        if action("Применяю изменение…", apply_one):
                            st.rerun()
                    if reject_column.button("Отклонить", key=f"editor-reject-{message['id']}-{index}"):
                        reject_proposal(store, message["id"], index)
                        st.rerun()
            if len(pending) > 1 and len({suggestions[i]["proposal"]["concept_id"] for i in pending}) == len(pending):
                st.caption("Связанные изменения, например перестановку дат, можно применить вместе.")
                if st.button("Применить все предложения вместе", key=f"editor-apply-all-{message['id']}",
                             disabled=bool(blocked)):
                    def apply_all(message_id=message["id"], indices=pending):
                        apply_proposals(store, message_id, indices)
                        return True
                    if action("Применяю изменения вместе…", apply_all):
                        st.rerun()
    if not messages:
        st.info("Обсудите идеи: например, «Какие темы повторяют прошлые публикации?»")
    text = st.chat_input("Обсудить идеи или предложить изменения…", key=f"editor-chat-{plan_id}",
                         max_chars=4000, disabled=not config.aitunnel_api_key or store.settings()["stop_requested"])
    if text and action("Редактор обдумывает план…", lambda: chat.send(plan_id, text)):
        st.rerun()
