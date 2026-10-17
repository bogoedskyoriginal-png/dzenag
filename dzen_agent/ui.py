from __future__ import annotations

import streamlit as st

from dzen_agent.config import get_settings
from dzen_agent.prompt_service import Prompts
from dzen_agent.store import Store
from dzen_agent.ui_pages import PAGES, handle_error

st.set_page_config(page_title="Dzen Agent", page_icon="✍️", layout="wide")
st.title("Dzen Agent")
st.caption("Контент, исследования и подготовка публикаций · время по Москве")

try:
    config = get_settings()
    store = Store(config.database_path)
    store.initialize()
    Prompts(store).seed()
    settings = store.settings()
    with st.sidebar:
        section = st.radio("Раздел", list(PAGES))
        st.caption(f"Режим: {settings['mode']}")
        if st.button("🛑 Остановить автопилот", type="primary", use_container_width=True):
            store.set_setting("stop_requested", True)
            store.set_setting("scheduler_enabled", False)
            store.set_setting("mode", "REVIEW")
            st.rerun()
        if settings["stop_requested"]:
            st.warning("Агент остановлен. Готовые результаты сохранены.")
        if not config.aitunnel_api_key:
            st.info("Ключ AITUNNEL не добавлен в .env. AI-операции пока недоступны.")
    PAGES[section](store, config)
except Exception as error:
    handle_error(error)
