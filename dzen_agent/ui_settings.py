"""Grouped settings and an intentionally secondary technical workspace."""
from __future__ import annotations

from datetime import time

import streamlit as st

from dzen_agent import scheduler
from dzen_agent.browser.session import launch_session
from dzen_agent.provider import fetch_catalog
from dzen_agent.recovery import refresh_pending_config, restart_analytics
from dzen_agent.services import week_slots
from dzen_agent.store import now_iso
from dzen_agent.ui_workflow import MODES, STAGES, human_status, recovery_panel, run_stages, schedule_fields, week_label

ROLES = {"analytics": ("Аналитик", "Выводы из статистики канала"), "discovery": ("Поиск тем", "Новые идеи и первые источники"),
    "planner": ("Планировщик", "Редакционные концепты и груминг"), "research": ("Исследователь", "Подробная проверка фактов после утверждения тем"),
    "writer": ("Автор", "Текст по утверждённой теме и исследованию"), "visual": ("Визуальный редактор", "Поиск и проверка подлинных фотографий")}


def model_settings(store, config):
    from dzen_agent.ui_pages import action
    settings = store.settings()
    catalog = settings.get("public_catalog", {})
    st.caption("Ключ AITUNNEL настроен в .env" if config.aitunnel_api_key else "Добавьте ключ AITUNNEL в локальный .env. Ключ не хранится в настройках приложения.")
    if st.button("Обновить каталог моделей"):
        result = action("Получаю публичный каталог…", fetch_catalog)
        if result:
            store.set_setting("public_catalog", result)
            store.set_setting("catalog_updated_at", now_iso())
            st.rerun()
    with st.form("model_settings"):
        values = {}
        for role, (label, description) in ROLES.items():
            current = settings.get(role + "_model") or ""
            options = list(dict.fromkeys(["", *([current] if current else []), *catalog.get("chat", {})]))
            st.markdown("**" + label + "**")
            st.caption(description)
            values[role + "_model"] = st.selectbox(label + " · модель", options, index=options.index(current),
                format_func=lambda value, role=role: value or ("Как у исследователя" if role in ("visual", "discovery") else "Не выбрана"))
            actual = values[role + "_model"] or (settings["research_model"] if role in ("visual", "discovery") else "")
            info = catalog.get("chat", {}).get(actual, {})
            if info:
                st.caption(f"Каталог: ввод {info.get('prompt_cost', '—')} ₽ / 1 млн токенов · выход {info.get('completion_cost', '—')} ₽ / 1 млн. Поиск может оплачиваться отдельно.")
        values["image_model"] = st.text_input("Модель генерации обложек для старого формата Image", settings["image_model"])
        values["research_use_web_tool"] = st.checkbox("Добавлять инструмент веб-поиска AITUNNEL", settings["research_use_web_tool"], help="Sonar использует встроенный поиск. Дополнительный поиск оплачивается отдельно.")
        qualities = ["low", "medium", "high", "auto"]
        values["image_quality"] = st.selectbox("Качество генерации для старого Image", qualities, index=qualities.index(settings["image_quality"]))
        if st.form_submit_button("Сохранить модели"):
            for key, value in values.items():
                store.set_setting(key, value)
            st.success("Модели сохранены для новых недель. Текущий запуск использует свой сохранённый набор.")


def settings_page(store, config):
    from dzen_agent.ui_pages import action, prompts_page
    st.title("Настройки")
    st.caption("Изменения не перезаписывают настройки уже созданной недели")
    edition, models, prompts, budget, mode, dzen = st.tabs(["Редакция", "AI-модели", "Промпты", "Расходы и лимиты", "Режим работы", "Дзен"])
    settings = store.settings()
    with edition:
        topic = st.text_area("Тематика и аудитория канала", value=settings["channel_topic"], height=110)
        st.caption("Расписание публикаций по умолчанию. В мастере новой недели его можно изменить отдельно.")
        values = schedule_fields(settings, "defaults-")
        if st.button("Сохранить редакционные настройки"):
            def save():
                week_slots("2099-01-05", {**settings, **values})
                for key, value in {**values, "channel_topic": topic}.items():
                    store.set_setting(key, value)
                return True
            action("Сохраняю расписание…", save)
    with models:
        model_settings(store, config)
    with prompts:
        prompts_page(store, config)
    with budget, st.form("budget_settings"):
        values = {}
        for key, label in (("monthly_soft_budget_rub", "Бюджет за 30 дней, ₽"), ("stop_when_balance_below_rub", "Остановка при балансе ниже, ₽")):
            values[key] = st.number_input(label, min_value=0., value=float(settings[key]), step=10.)
        for key, label, lo, hi in (("max_output_tokens", "Выходных токенов на этап", 1000, 128000),
                ("analytics_max_output_tokens", "Выходных токенов на анализ статистики", 1000, 128000),
                ("max_research_calls_per_article", "Исследований на статью, максимум", 1, 3),
                ("statistics_max_age_days", "Актуальность статистики, дней", 1, 90), ("max_retries", "Повторов при ограничении частоты сервиса", 0, 3)):
            values[key] = st.number_input(label, lo, hi, int(settings[key]))
        efforts = ["low", "medium", "high"]
        values["analytics_reasoning_effort"] = st.selectbox("Глубина рассуждений аналитика DeepSeek", efforts, index=efforts.index(settings["analytics_reasoning_effort"]))
        values["ai_qa_enabled"] = st.checkbox("Дополнительная платная AI-проверка", settings["ai_qa_enabled"])
        st.caption("Автоматические повторы оплаченных и неопределённых ответов отключены. Изменение лимита не разрешает новую оплату.")
        if st.form_submit_button("Сохранить лимиты"):
            for key, value in values.items():
                store.set_setting(key, value)
            st.success("Лимиты сохранены. Текущий запуск не изменён.")
    with mode:
        selected = st.radio("Режим подготовки", list(MODES), index=list(MODES).index(settings["mode"]), format_func=lambda value: MODES[value])
        st.caption("С проверкой редактора — утверждение тем и готовых материалов. Ручной — передача в Дзен отдельным действием. "
                   "Автономный режим допускает фоновую работу по расписанию; интеграция отправки пока не подтверждена.")
        confirmed = st.checkbox("Разрешаю автономные AI-расходы и действия после проверки интеграции")
        if st.button("Применить режим"):
            if selected == "AUTOPILOT" and not confirmed:
                st.error("Подтвердите разрешение автономной работы.")
            else:
                store.set_setting("mode", selected)
                st.success("Режим сохранён.")
    with dzen:
        st.info("Заполнение редактора доступно. Полная загрузка оформления и автоматическое планирование ещё не проверены на канале.")
        editor_url = st.text_input("Адрес редактора вашего канала", settings["dzen_editor_url"])
        if st.button("Сохранить адрес редактора"):
            store.set_setting("dzen_editor_url", editor_url)
            st.success("Адрес сохранён.")
        if st.button("Открыть Дзен для ручного входа"):
            action("Открываю браузер…", launch_session, success="Браузер открыт. Выполните вход вручную.")


def system_page(store, config):
    from dzen_agent.ui_pages import action, show_account
    st.title("Система")
    st.caption("История, обслуживание и диагностика. Ежедневная работа — на рабочем столе.")
    history, windows, diagnostics = st.tabs(["История запусков", "Автоматизация Windows", "Диагностика"])
    with history:
        runs = store.rows("SELECT * FROM pipeline_runs ORDER BY id DESC")
        if runs:
            run_id = st.selectbox("История недель", [r["id"] for r in runs], format_func=lambda i:
                next(f"№{i} · {week_label(r['week_start'])} · {human_status(r['status'])}" for r in runs if r["id"] == i))
            run = next(r for r in runs if r["id"] == run_id)
            rows = run_stages(store, run)
            st.dataframe([{"Этап": STAGES.get(r["stage"], r["stage"]), "Состояние": human_status(r["status"]),
                "Начало": r["started_at"], "Завершение": r["ended_at"]} for r in rows], hide_index=True)
            failed = next((r for r in rows if r["status"] == "ERROR"), None)
            if failed:
                with st.expander("Восстановление текущего этапа"):
                    recovery_panel(store, config, run, failed)
            with st.expander("Технические детали и расширенное восстановление"):
                st.json({"run_id": run_id, "status": run["status"], "error": run["error"]}, expanded=False)
                calls = store.rows("SELECT call_key,status,error,result_json FROM ai_calls WHERE call_key LIKE ? ORDER BY created_at DESC", (f"run:{run_id}:%",))
                for call in calls:
                    with st.expander(call["call_key"]):
                        st.json(call, expanded=False)
                update_ok = st.checkbox("Применить сохранённые сейчас модели и промпты к оставшимся этапам")
                if st.button("Обновить настройки незавершённых этапов", disabled=not update_ok):
                    action("Обновляю снимок настроек…", lambda: refresh_pending_config(store, run_id))
                if not store.one("SELECT id FROM content_plans WHERE run_id=?", (run_id,)):
                    restart_ok = st.checkbox("Разрешаю новый платный анализ последнего импорта вместо текущего основания")
                    if st.button("Сменить импорт и начать новый анализ", disabled=not restart_ok):
                        action("Обновляю основание запуска…", lambda: restart_analytics(store, run_id, confirmed=restart_ok))
        else:
            st.info("История появится после создания первой недели.")
    with windows:
        settings = store.settings()
        st.subheader("Когда начинать подготовку недели")
        st.caption("Это расписание запуска программы, а не время публикации статей. Время статей задаётся в редакционных настройках и контент-плане.")
        st.write("Автоматический запуск:", "включён" if settings["scheduler_enabled"] else "выключен")
        with st.form("scheduler"):
            day = st.selectbox("День запуска подготовки", list(range(7)), index=settings["scheduler_day"],
                format_func=lambda i: ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"][i])
            at = st.time_input("Время запуска подготовки · Москва", time.fromisoformat(settings["scheduler_time"]))
            confirmed = st.checkbox("Разрешаю фоновую подготовку и AI-расходы по этому расписанию")
            if st.form_submit_button("Включить расписание Windows"):
                if not confirmed:
                    st.warning("Подтвердите разрешение фоновой подготовки.")
                else:
                    store.set_setting("scheduler_day", day)
                    store.set_setting("scheduler_time", at.strftime("%H:%M"))
                    action("Устанавливаю задание…", lambda: scheduler.install(store), success="Расписание установлено. Подготовка сейчас не запускалась.")
        if st.button("Выключить расписание"):
            action("Выключаю расписание…", lambda: scheduler.remove(store))
        st.caption("Компьютер должен быть включён, пользователь должен войти в Windows.")
        if st.button("Проверить задание Windows"):
            result = action("Проверяю задание…", scheduler.windows_task_status)
            if result:
                st.write(result.get("state", "Состояние получено"))
    with diagnostics:
        show_account(store, config)
        if st.button("Проверить диагностику Дзена"):
            action("Открываю браузер диагностики…", launch_session, success="Браузер открыт. Вход и проверки выполняются вручную.")
        with st.expander("Технический журнал"):
            st.dataframe(store.rows("SELECT * FROM event_log ORDER BY id DESC LIMIT 100"), hide_index=True)
