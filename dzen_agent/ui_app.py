"""Four product workspaces; execution decisions stay in the worker."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import streamlit as st

from dzen_agent.autonomy import start_run
from dzen_agent.execution import authorize_run
from dzen_agent.store import MSK

PHASES = (("Аналитика", ("COLLECT_STATS", "ANALYZE"), "Анализируем статистику"),
          ("Поиск тем", ("DISCOVERY",), "Ищем новые темы"),
          ("Планирование", ("PLAN", "PLAN_REVIEW"), "Составляем редакционный план"),
          ("Исследование", ("RESEARCH", "REFRESH_RESEARCH"), "Проверяем факты"),
          ("Написание", ("WRITE",), "Пишем статьи"),
          ("Оформление и проверка", ("IMAGE", "QA"), "Подбираем фотографии и проверяем материалы"))


def generation_settings(store):
    tomorrow = (datetime.now(MSK).date() + timedelta(days=1)).isoformat()
    calendar = dict(store.settings().get("generation_calendar") or {tomorrow: 1})
    st.write("Выберите даты в календаре и количество статей. Время редакция назначит автоматически.")
    date = st.date_input("Дата генерации", min_value=datetime.now(MSK).date(), value=datetime.now(MSK).date() + timedelta(days=1))
    if st.button("Добавить дату"):
        calendar.setdefault(date.isoformat(), 1)
        store.set_setting("generation_calendar", calendar)
        st.session_state.requested_preparation = None
        st.rerun()
    with st.form("generation-calendar"):
        values = {}
        for day, count in sorted(calendar.items()):
            values[day] = int(st.number_input(datetime.fromisoformat(day).strftime("%d.%m.%Y"), min_value=0, max_value=21, value=count, key="day-" + day))
        if st.form_submit_button("Сохранить календарь"):
            if not 1 <= sum(values.values()) <= 21:
                st.error("Всего должно быть от 1 до 21 статьи.")
            else:
                store.set_setting("generation_calendar", {d:n for d,n in values.items() if n})
                st.session_state.requested_preparation = None
                st.success("Календарь сохранён.")
                st.rerun()
    return calendar


def ready_articles(store, run_id):
    rows = store.rows("SELECT a.*,d.concept_id,d.qa_json,d.short_summary FROM articles a JOIN article_details d ON d.article_id=a.id WHERE d.run_id=? ORDER BY a.id", (run_id,))
    ready = []
    for row in rows:
        item = store.one("SELECT status FROM pipeline_items WHERE run_id=? AND concept_id=? AND stage='QA' ORDER BY updated_at DESC,id DESC LIMIT 1", (run_id, row["concept_id"]))
        if (item and item["status"] == "SUCCEEDED") or (not item and json.loads(row["qa_json"] or "{}").get("passed")):
            ready.append(row)
    return ready


def progress(store, run):
    stages = {r["stage"]: r["status"] for r in store.rows("SELECT stage,status FROM pipeline_stages WHERE run_id=?", (run["id"],))}
    items = store.rows("SELECT * FROM pipeline_items WHERE run_id=? ORDER BY updated_at,id", (run["id"],))
    latest = {(r["concept_id"], r["stage"]): r for r in items}
    target = json.loads(run["settings_json"])["max_articles_per_week"]
    for number, (title, codes, description) in enumerate(PHASES, 1):
        item_codes = {"RESEARCH" if c == "REFRESH_RESEARCH" else c for c in codes}
        relevant = [r for r in latest.values() if r["stage"] in item_codes]
        active = any(r["status"] == "RUNNING" for r in relevant)
        done = all(stages.get(c) == "SUCCEEDED" for c in codes)
        if number <= 3:
            active = any(stages.get(c) == "RUNNING" for c in codes)
        completed = len({r["concept_id"] for r in relevant if r["status"] == "SUCCEEDED" and r["stage"] == ("QA" if number == 6 else codes[0])})
        state = "Готово" if done else (description if active and run["status"] == "RUNNING" else "Ожидает")
        symbol = "✓" if done else ("●" if active and run["status"] == "RUNNING" else str(number))
        css = "pulse" if active and run["status"] == "RUNNING" else ""
        st.markdown(f'<div class="phase"><span class="phase-icon {css}">{symbol}</span><div><strong>{title}</strong><small>{state}</small></div></div>', unsafe_allow_html=True)
        if number >= 4 and target:
            st.progress(min(1.0, completed / target), text=f"{completed} из {target}")


@st.fragment(run_every=3)
def overview(store, config):
    runs = store.rows("SELECT * FROM pipeline_runs ORDER BY id DESC")
    run = None
    if runs:
        selected = st.session_state.get("active_preparation", runs[0]["id"])
        run = next((r for r in runs if r["id"] == selected), None)
    if run:
        ready = ready_articles(store, run["id"])
        target = json.loads(run["settings_json"]).get("requested_count", json.loads(run["settings_json"])["max_articles_per_week"])
        status = {"RUNNING": "Агент работает", "PREPARED": "Материалы готовы", "PARTIAL_READY": "Готова часть материалов",
                  "STOPPED": "Работа приостановлена", "SUCCEEDED": "Готово"}.get(run["status"], "Готовые результаты сохранены")
        st.subheader(status)
        a, b = st.columns(2)
        a.metric("Готово статей", f"{len(ready)} / {target}")
        cost = store.costs(run_id=run["id"])
        b.metric("Расходы", f"{cost['total']:.2f} ₽")
        if cost["unknown"]:
            st.warning("Есть расход без подтверждённой стоимости. Новые платные запросы будут заблокированы.")
        if run["status"] == "RUNNING":
            if st.button("Остановить агента"):
                store.set_setting("stop_requested", True)
                st.rerun()
        progress(store, run)
        if ready:
            st.subheader("Готовые материалы")
            for article in ready:
                with st.container(border=True):
                    st.markdown("**" + article["title"] + "**")
                    st.write(article["short_summary"] or article["body"][:250])
                    image = store.active_image(article["id"])
                    if image:
                        from pathlib import Path
                        if Path(image["path"]).is_file():
                            st.image(image["path"], width=380)
                    publication = store.one("SELECT * FROM publications WHERE article_id=?", (article["id"],))
                    if publication and publication["status"] == "DRAFT_VERIFIED":
                        st.link_button("Открыть черновик в Дзене", publication["dzen_url"])
                    elif publication and publication.get("error"):
                        st.caption("Передача черновика остановлена. Готовый материал сохранён; подробности — в журнале.")
                    else:
                        from dzen_agent.execution import authorization
                        grant = authorization(store, run["id"])
                        if grant and json.loads(grant["policy_json"])["drafts"]:
                            st.caption("Черновик ещё не подтверждён. Проверьте вход в Дзен в настройках и журнал передачи.")
                    warnings = json.loads(article["qa_json"] or "{}").get("warnings", [])
                    for warning in warnings:
                        st.caption(warning)
        if run["status"] not in ("RUNNING", "PREPARED", "SUCCEEDED"):
            if run.get("error"):
                from dzen_agent.execution import category
                reason = {"BUDGET_BLOCKER":"Достигнут предел бюджета подготовки.",
                          "PAYMENT_UNCERTAIN":"Нужна сверка последнего списания перед новыми расходами.",
                          "AUTH_REQUIRED":"Нужен ручной вход в подключённый сервис.",
                          "EDITORIAL_BLOCKER":"Часть материала не удалось подтвердить.",
                          "LOCAL_RECOVERABLE":"Не все данные удалось обработать автоматически.",
                          "FATAL":"Выполнение остановлено; готовые результаты сохранены."}[category(run["error"])]
                st.write(reason)
            st.caption("Подробности недостающих материалов и возможных ограничений доступны в журнале.")
    else:
        st.subheader("Ваша редакция готова к работе")
        st.write("Выберите даты, задайте бюджет и запустите подготовку. От поиска тем до готовых статей агент работает самостоятельно.")
        for _, (title, _, _) in enumerate(PHASES):
            st.caption(title)


def home(store, config):
    from dzen_agent.execution import authorization
    st.title("Dzen Agent")
    st.caption("Редакция «Перемотки»")
    if "requested_preparation" in st.session_state:
        st.session_state.active_preparation = st.session_state.pop("requested_preparation")
    runs = store.rows("SELECT * FROM pipeline_runs ORDER BY id DESC")
    selected = None
    if runs:
        selected = st.selectbox("Подготовка", [r["id"] for r in runs] + [None], key="active_preparation",
            format_func=lambda i: "Новая подготовка" if i is None else next(datetime.fromisoformat(r["week_start"]).strftime("%d.%m.%Y") + f" · №{i}" for r in runs if r["id"] == i))
    existing = next((r for r in runs if r["id"] == selected and r["status"] != "SUCCEEDED"), None)
    grant = authorization(store, selected) if existing else None
    with st.expander("Настроить генерацию"):
        calendar = generation_settings(store)
    wishes = st.text_area("Пожелания редактора", placeholder="Необязательно: темы, ракурсы и пожелания к подборке", height=90)
    budget = st.number_input("Бюджет подготовки, ₽", min_value=1.0, max_value=10000.0, value=float(store.settings().get("run_budget_rub", 100)), step=10.0,
                             help="Для продолжения существующей подготовки это разрешение на дополнительные расходы. Уже потраченные деньги сохраняются отдельно.")
    consent = st.checkbox("Разрешаю AI-расходы в пределах этого бюджета")
    drafts = st.checkbox("Разрешаю создавать черновики в моём Дзене", value=False,
                         help="Отдельное разрешение. Окончательная публикация остаётся за вами.")
    if grant:
        st.caption(f"Сохранённое разрешение: осталось до {max(0, grant['budget_rub'] - store.costs(run_id=selected)['total']):.2f} ₽. Без нового согласия используется только этот остаток.")
    running = store.one("SELECT id FROM pipeline_runs WHERE status='RUNNING'")
    if st.button("Запустить агента", type="primary", disabled=not (consent or grant) or not config.aitunnel_api_key or bool(running)):
        try:
            from dzen_agent import scheduler
            selected = st.session_state.get("active_preparation")
            existing = store.one("SELECT * FROM pipeline_runs WHERE id=? AND status<>'SUCCEEDED'", (selected,))
            grant = authorization(store, selected) if existing else None
            if existing and existing["status"] == "PREPARED" and not (drafts or (grant and json.loads(grant["policy_json"])["drafts"])):
                raise ValueError("Материалы этой подготовки готовы. Выберите «Новая подготовка» для другого календаря.")
            store.set_setting("stop_requested", False)
            if existing:
                run_id = existing["id"]
                if consent or not grant:
                    authorize_run(store, run_id, budget_rub=budget, confirmed=consent, drafts=drafts)
                elif drafts and not json.loads(grant["policy_json"])["drafts"]:
                    from dzen_agent.store import dumps
                    policy = json.loads(grant["policy_json"])
                    policy["drafts"] = True
                    store.execute("UPDATE run_authorizations SET policy_json=? WHERE run_id=?", (dumps(policy), run_id))
                    store.log("AUTHORIZATION", "Пользователь отдельно разрешил черновики; AI-бюджет не увеличен", {"run_id":run_id})
            else:
                run_id = start_run(store, calendar, wishes, budget, confirmed=consent, drafts=drafts)
            store.set_setting("run_budget_rub", budget)
            st.session_state.requested_preparation = run_id
            scheduler.launch_pipeline(run_id)
            st.success("Подготовка запущена в фоне. Вкладку можно закрыть.")
            st.rerun()
        except (ValueError, RuntimeError) as error:
            st.error(str(error))
    if not config.aitunnel_api_key:
        st.caption("Для запуска добавьте ключ AITUNNEL в .env.")
    overview(store, config)


def journal(store, config):
    from dzen_agent.ui_pages import costs_page, show_account
    st.title("Журнал")
    runs, operations, costs, drafts, plans, technical = st.tabs(["Запуски", "Действия", "Расходы", "Дзен", "Планы", "Диагностика"])
    with runs:
        st.dataframe(store.rows("SELECT id,week_start,status,started_at,ended_at,error FROM pipeline_runs ORDER BY id DESC"), hide_index=True)
    with operations:
        st.dataframe(store.rows("SELECT timestamp,stage,message FROM event_log ORDER BY id DESC LIMIT 300"), hide_index=True)
    with costs:
        show_account(store, config)
        costs_page(store, config)
    with drafts:
        st.dataframe(store.rows("SELECT article_id,status,dzen_url,verified_at,error FROM publications ORDER BY id DESC"), hide_index=True)
    with plans:
        st.dataframe(store.rows("SELECT p.run_id,c.topic,c.scheduled_at,c.status FROM concepts c JOIN content_plans p ON p.id=c.plan_id ORDER BY p.run_id DESC,c.scheduled_at"), hide_index=True)
    with technical:
        entries = store.rows("SELECT * FROM event_log ORDER BY id DESC LIMIT 100")
        if entries:
            chosen = st.selectbox("Событие", range(len(entries)), format_func=lambda i: entries[i]["timestamp"] + " · " + entries[i]["stage"])
            st.json(json.loads(entries[chosen]["details_json"]), expanded=False)
        st.dataframe(store.rows("SELECT call_key,status,error,updated_at FROM ai_calls ORDER BY updated_at DESC LIMIT 100"), hide_index=True)
        st.link_button("Прежний интерфейс и восстановление старых запусков", "/?view=legacy")


def settings(store, config):
    from dzen_agent.ui_pages import prompts_page, statistics_page
    from dzen_agent.ui_settings import model_settings
    from dzen_agent.browser.session import launch_session
    st.title("Настройки")
    calendar, models, prompts, statistics, automation = st.tabs(["Генерация", "Модели", "Промпты", "Статистика", "Подключения"])
    with calendar:
        generation_settings(store)
    with models:
        model_settings(store, config)
    with prompts:
        prompts_page(store, config)
    with statistics:
        statistics_page(store, config)
    with automation:
        st.write("Первый вход в Дзен, CAPTCHA и подтверждения выполняются владельцем вручную.")
        export_verified = bool(store.settings().get("statistics_export_verification"))
        automatic_stats = st.checkbox("Обновлять статистику из Студии перед подготовкой", disabled=not export_verified,
            value=bool(store.settings().get("statistics_auto_export", False)),
            help="Используется проверенная кнопка экспорта. При недоступности — последний пригодный локальный импорт.")
        if automatic_stats != bool(store.settings().get("statistics_auto_export", False)):
            store.set_setting("statistics_auto_export", automatic_stats)
        if not export_verified:
            st.caption("Автоматическая выгрузка ещё не проверена для этого профиля. Загрузите XLSX в разделе «Статистика».")
        if st.button("Открыть Дзен для входа"):
            launch_session()
        from dzen_agent import scheduler
        st.subheader("Фоновый запуск Windows")
        with st.form("autonomous-windows"):
            day = st.selectbox("День запуска", range(7), format_func=lambda i: ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"][i])
            at = st.time_input("Время запуска")
            amount = st.number_input("Бюджет одного фонового запуска, ₽", min_value=1.0, value=100.0)
            confirmed = st.checkbox("Разрешаю фоновую подготовку в пределах указанного бюджета")
            if st.form_submit_button("Включить фоновое расписание"):
                if not confirmed:
                    st.warning("Нужно разрешение на фоновые AI-расходы.")
                else:
                    store.set_setting("scheduler_day", day)
                    store.set_setting("scheduler_time", at.strftime("%H:%M"))
                    store.set_setting("scheduled_autonomous_policy", {"budget_rub": amount, "drafts": False})
                    scheduler.install(store)
                    st.success("Расписание установлено. Будут использованы будущие даты сохранённого календаря.")
        if st.button("Отключить фоновый запуск"):
            scheduler.remove(store)


def main():
    from dzen_agent.config import get_settings
    from dzen_agent.store import Store
    from dzen_agent.prompt_service import Prompts
    from dzen_agent.ui_content import articles_page
    st.set_page_config(page_title="Dzen Agent", page_icon="🗞️", layout="wide")
    st.markdown("""<style>.block-container{max-width:1120px;padding-top:2.5rem}.phase{display:flex;gap:16px;padding:14px 0;border-bottom:1px solid #ffffff18}.phase small{display:block;opacity:.62;margin-top:4px}.phase-icon{border-radius:50%;background:#447cff24;color:#729aff;width:32px;height:32px;display:grid;place-items:center}.pulse{animation:pulse 1s ease-in-out infinite}@keyframes pulse{50%{opacity:.3}}h1{letter-spacing:-.04em}div[data-testid=stMetricValue]{font-weight:650}</style>""", unsafe_allow_html=True)
    config = get_settings()
    store = Store(config.database_path)
    store.initialize()
    Prompts(store).seed()
    from dzen_agent.autonomy import resume_interrupted
    resume_interrupted(store)
    page = st.sidebar.radio("Раздел", ["Главная", "Материалы", "Настройки", "Журнал"])
    {"Главная": home, "Материалы": articles_page, "Настройки": settings, "Журнал": journal}[page](store, config)
