from __future__ import annotations

import streamlit as st

from dzen_agent.config import get_settings
from dzen_agent.prompt_service import Prompts
from dzen_agent.store import Store
from dzen_agent.ui_pages import PAGES, handle_error

st.set_page_config(page_title="Dzen Agent · Перемотка", page_icon="🗞️", layout="wide")

try:
    config = get_settings()
    store = Store(config.database_path)
    store.initialize()
    Prompts(store).seed()
    settings = store.settings()
    if "nav_requested" in st.session_state:
        st.session_state.navigation = st.session_state.pop("nav_requested")
    with st.sidebar:
        st.title("Dzen Agent")
        st.caption("Редакция «Перемотки»")
        section = st.radio("Раздел", list(PAGES), key="navigation", label_visibility="collapsed")
        st.divider()
        st.caption("Локальная редакционная система\n\nВремя публикаций · Москва")
        running = store.one("SELECT id FROM pipeline_runs WHERE status='RUNNING'") or st.session_state.get("dispatch")
        def stop_agent():
            store.set_setting("stop_requested", True)
            store.set_setting("scheduler_enabled", False)
            store.set_setting("mode", "REVIEW")
            st.session_state.pop("dispatch", None)
        if running:
            st.button("Остановить агента", on_click=stop_agent, width="stretch")
            st.caption("Уже отправленный AI-запрос может завершиться и быть оплачен.")
        else:
            with st.expander("Управление агентом"):
                st.button("Остановить агента", on_click=stop_agent, width="stretch")
        if settings["stop_requested"]:
            st.warning("Остановка запрошена" if running else "Агент приостановлен")
            if st.button("Разрешить продолжение работы"):
                store.set_setting("stop_requested", False)
                st.rerun()
        if not config.aitunnel_api_key:
            st.caption("AI пока недоступен: добавьте ключ AITUNNEL в .env.")
    PAGES[section](store, config)
except Exception as error:
    handle_error(error)
