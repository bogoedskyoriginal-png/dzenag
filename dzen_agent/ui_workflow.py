"""Editor-facing run status and a single place to start, resume and recover work."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import streamlit as st

from dzen_agent import scheduler
from dzen_agent.editorial_pipeline import preflight
from dzen_agent.locking import exclusive_lock
from dzen_agent.pipeline.orchestrator import create_run, estimate_operations, next_week, stages_for
from dzen_agent.prompt_service import Prompts
from dzen_agent.recovery import authorize_retry
from dzen_agent.services import week_slots
from dzen_agent.store import MSK

STAGES = {"COLLECT_STATS": "Подготовка статистики", "ANALYZE": "Анализ статистики", "DISCOVERY": "Поиск тем",
    "PLAN": "Составление плана", "PLAN_REVIEW": "Утверждение тем", "RESEARCH": "Исследование",
    "REFRESH_RESEARCH": "Проверка исследований", "WRITE": "Написание", "IMAGE": "Оформление",
    "QA": "Проверка качества", "REVIEW": "Просмотр материалов", "DZEN_SCHEDULE": "Передача в Дзен", "VERIFY": "Проверка в Дзене"}
MODES = {"REVIEW": "С проверкой редактора", "MANUAL": "Ручное управление", "AUTOPILOT": "Автономный режим"}
STATUS = {"PENDING": "Ожидает", "RUNNING": "В работе", "ERROR": "Нужна помощь", "SUCCEEDED": "Готово",
    "WAITING": "Ожидает редактора", "NEEDS_ACTION": "Нужна помощь", "STOPPED": "Приостановлено",
    "ANALYTICS_READY": "Аналитика готова", "PLAN_APPROVED": "Темы утверждены", "APPROVED": "Материалы утверждены",
    "AWAITING_PLAN_REVIEW": "Проверьте темы", "AWAITING_REVIEW": "Материалы на проверке",
    "AWAITING_SEND": "Ожидает передачи в Дзен", "PLANNED": "В плане", "PUBLISHED": "Опубликовано",
    "WRITTEN": "Текст готов", "IMAGE_READY": "Оформлено", "READY_FOR_REVIEW": "Готово к проверке",
    "DRAFT": "Черновик", "RESEARCHED": "Исследовано", "SCHEDULED": "Передано в расписание",
    "SCHEDULED_VERIFIED": "В расписании Дзена"}
STATUS.update({"PREPARING_IN_DZEN": "Начинается перенос", "CREATING_DRAFT": "Создаётся черновик",
    "RENDERING": "Идёт вёрстка", "DRAFT_READY": "Черновик свёрстан", "SAVING_SCHEDULE": "Сохраняется отложенная статья",
    "SCHEDULED_PENDING_VERIFY": "Проверяется расписание"})
DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def navigate(page, **selection):
    st.session_state.update(selection)
    st.session_state.nav_requested = page
    st.rerun()


def human_status(value):
    return STATUS.get(str(value), "Требует проверки")


def week_label(value):
    start = datetime.fromisoformat(value)
    return f"{start:%d.%m} — {start + timedelta(days=6):%d.%m.%Y}"


def run_stages(store, run):
    rows = store.rows("SELECT * FROM pipeline_stages WHERE run_id=?", (run["id"],))
    order = stages_for(json.loads(run["settings_json"]))
    return sorted(rows, key=lambda r: order.index(r["stage"]) if r["stage"] in order else 99)


def current_stage(store, run):
    rows = run_stages(store, run)
    return next((r for r in rows if r["status"] != "SUCCEEDED"), rows[-1] if rows else None)


def failed_receipt(store, run, stage):
    operations = {"ANALYZE": ["ANALYTICS"], "DISCOVERY": ["DISCOVERY"], "PLAN": ["PLANNING"],
        "RESEARCH": ["RESEARCH"], "REFRESH_RESEARCH": ["RESEARCH"], "WRITE": ["WRITE"],
        "IMAGE": ["IMAGE", "IMAGE_SEARCH", "IMAGE_INSPECT"], "QA": ["QA"]}.get(stage, [])
    revision = json.loads(run["settings_json"]).get("analysis_revision", 0)
    prefix = f"run:{run['id']}" + (f":analysis-v{revision}" if revision else "")
    suffix = {"ANALYZE": ":analytics", "DISCOVERY": ":discovery", "PLAN": ":plan"}.get(stage)
    if suffix:
        return store.one("SELECT * FROM ai_calls WHERE call_key=?", (prefix + suffix,))
    rows = store.rows("SELECT a.*,u.operation FROM ai_calls a LEFT JOIN usage_events u ON a.call_key=u.call_key "
        "WHERE a.call_key LIKE ? AND a.call_key NOT LIKE '%:archived:%' ORDER BY a.updated_at DESC", (prefix + ":%",))
    matching = [r for r in rows if r.get("operation") in operations]
    return next((r for r in matching if r["status"] != "SUCCEEDED"), matching[0] if matching else None)


def error_explanation(run, stage, receipt=None):
    text = run.get("error") or stage.get("error") or ""
    if receipt and receipt.get("result_json"):
        saved = json.loads(receipt["result_json"])
        for choice in saved.get("choices", []):
            if "length" in (choice.get("finish_reason"), choice.get("native_finish_reason")):
                text += " length"
    if "length" in text or "обрезан" in text:
        return "Модель достигла лимита ответа. Результат получен не полностью; он сохранён вместе с расходами."
    if "400" in text:
        return "AI-сервис отклонил параметры запроса. Сохранённые результаты предыдущих этапов доступны."
    if any(word in text.lower() for word in ("структуре", "json", "контракт")):
        return "Ответ модели не прошёл проверку данных. Он сохранён; новый платный ответ не запрашивается автоматически."
    if "неизвестен" in text or "однозначно" in text or "5xx" in text or "HTTP 50" in text:
        return "Ответ AI-сервиса не подтверждён. Перед повтором проверьте историю расходов, чтобы избежать двойной оплаты."
    if "баланс" in text.lower() or "бюджет" in text.lower():
        return "Для продолжения проверьте баланс и ограничения расходов в настройках."
    if stage["stage"] == "COLLECT_STATS":
        return "Нужна свежая статистика канала. Загрузите выгрузку в разделе «Аналитика»."
    if stage["stage"] in ("DZEN_SCHEDULE", "VERIFY"):
        return "Передача в Дзен требует проверки интеграции и явного действия редактора. Готовые материалы сохранены."
    return "Этап не завершён. Готовые результаты сохранены. Посмотрите подробности перед продолжением."


def queue_run(store, run_id, *, analytics_only=False, send=False):
    """Record dispatch in the browser session; never report background work as finished."""
    from dzen_agent.ui_pages import handle_error
    if store.settings()["stop_requested"]:
        st.warning("Сначала возобновите работу агента.")
        return False
    run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    if run["status"] == "RUNNING" or st.session_state.get("dispatch", {}).get("run_id") == run_id:
        st.info("Задача уже передана в работу.")
        return False
    try:
        scheduler.launch_pipeline(run_id, **({"analytics_only": True} if analytics_only else {"send": True} if send else {}))
        st.session_state.dispatch = {"run_id": run_id, "signature": (run["status"], run["error"], run["ended_at"])}
        return True
    except Exception as error:
        handle_error(error)
        return False


def readiness(store, config, settings, week):
    """Read-only local checks: no balance/network calls and no configuration writes."""
    problems = []
    imported = store.one("SELECT * FROM statistics_imports ORDER BY id DESC LIMIT 1")
    if not imported:
        problems.append("Загрузите статистику канала в разделе «Аналитика».")
    elif datetime.now(MSK) - datetime.fromisoformat(imported["created_at"]) > timedelta(days=settings["statistics_max_age_days"]):
        problems.append("Импорт статистики устарел. Загрузите свежую выгрузку.")
    if not config.aitunnel_api_key:
        problems.append("Добавьте ключ AITUNNEL в локальный .env. Подключение проверяется отдельно.")
    prompts = Prompts(store).snapshot()
    for role in ("ANALYTICS", "DISCOVERY", "PLANNER", "RESEARCH", "WRITER", "IMAGE", "EDITOR"):
        if not prompts.get(role):
            problems.append(f"Сохраните редакционный промпт {role.title()}.")
    for role in ("analytics", "discovery", "planner", "research", "writer", "visual"):
        fallback = settings.get("research_model") if role in ("discovery", "visual") else None
        if not (settings.get(role + "_model") or fallback or (role == "visual" and settings.get("image_model"))):
            problems.append(f"Выберите модель для {role.title()}.")
    try:
        preflight(settings, prompts)
        slots = week_slots(week, settings)
        if datetime.fromisoformat(week).weekday() != 0:
            raise ValueError("Выберите понедельник начала недели.")
        if any(datetime.fromisoformat(s) <= datetime.now(MSK) for s in slots):
            raise ValueError("Выберите будущую неделю и время публикаций.")
        occupied = store.rows("SELECT scheduled_at FROM concepts UNION SELECT scheduled_at FROM article_details WHERE scheduled_at IS NOT NULL")
        if {datetime.fromisoformat(s) for s in slots} & {datetime.fromisoformat(r["scheduled_at"]) for r in occupied}:
            raise ValueError("Некоторые даты и время уже заняты. Измените расписание.")
    except (ValueError, RuntimeError) as error:
        problems.append(str(error))
        slots = []
    if store.costs(days=30)["total"] >= settings["monthly_soft_budget_rub"]:
        problems.append("Достигнут локальный бюджет за 30 дней.")
    return problems, slots


def schedule_fields(settings, prefix):
    count = st.number_input("Публикаций за неделю", 1, 21, int(settings["max_articles_per_week"]), key=prefix + "count")
    template = st.selectbox("Расписание публикаций", ["Текущее расписание", "Перемотка · 05:00, 06:00, 16:00", "Своё расписание"],
        index=1 if prefix == "wizard-" and count == 21 and settings["max_articles_per_week"] != 21 else 0,
        key=prefix + f"template-{count}")
    preset = template.startswith("Перемотка")
    days = st.multiselect("Дни публикаций", list(range(7)), default=list(range(7)) if preset else settings["publication_days"],
        format_func=lambda d: DAYS[d], key=prefix + f"days-{template}")
    times = st.text_input("Время публикаций по Москве", value="05:00, 06:00, 16:00" if preset else ", ".join(
        settings.get("publication_times") or [settings["publication_time"]]), key=prefix + f"times-{template}",
        help="Несколько слотов через запятую. Число публикаций определяется отдельно.")
    parsed = [v.strip() for v in times.split(",") if v.strip()]
    individual = st.checkbox("Отдельное время для каждого дня", value=bool(settings.get("publication_slots")) and not preset,
        key=prefix + f"individual-{template}")
    overrides = {}
    if individual:
        for day in days:
            raw = st.text_input(DAYS[day] + " · время", value=", ".join((settings.get("publication_slots") or {}).get(str(day), parsed)),
                key=f"{prefix}slots-{template}-{day}")
            overrides[str(day)] = [v.strip() for v in raw.split(",") if v.strip()]
    max_daily = st.number_input("Не более публикаций в день", 1, 21,
        min(21, max(3 if preset else int(settings.get("max_articles_per_day", 1)), len(parsed))), key=prefix + f"daily-{template}")
    return {"max_articles_per_week": count, "publication_days": days, "publication_times": parsed,
            "publication_time": parsed[0] if parsed else "12:00", "publication_slots": overrides, "max_articles_per_day": max_daily}


def wizard(store, config):
    from dzen_agent.ui_pages import handle_error
    st.subheader("Новая неделя")
    st.caption("Период → число публикаций → расписание → проверка готовности")
    date = st.date_input("Неделя начинается", value=datetime.fromisoformat(next_week()).date(), key="wizard-week")
    week = date.isoformat()
    override = schedule_fields(store.settings(), "wizard-")
    existing = store.one("SELECT id FROM pipeline_runs WHERE week_start=?", (week,))
    if existing:
        st.info("Эта неделя уже существует. Её результаты и расписание сохранены.")
        if st.button("Открыть существующую неделю", type="primary"):
            st.session_state.show_wizard = False
            navigate("Рабочий стол", active_run=existing["id"])
        if st.button("Отменить создание недели"):
            st.session_state.show_wizard = False
            st.rerun()
        return
    problems, slots = readiness(store, config, {**store.settings(), **override}, week)
    st.markdown("**Проверка перед запуском**")
    for problem in problems:
        st.warning(problem)
    if not problems:
        st.success("Локальные проверки пройдены. Баланс и доступ к модели проверятся перед первым AI-запросом.")
    if slots:
        st.dataframe([{"Дата": datetime.fromisoformat(s).strftime("%d.%m.%Y"), "Время · Москва": s[11:16]} for s in slots], hide_index=True)
    models = store.settings()
    st.caption("Модели: " + " · ".join(f"{r.title()}: {models.get(r + '_model') or models.get('research_model') or 'не выбрана'}"
        for r in ("analytics", "discovery", "planner", "research", "writer", "visual")))
    operations = estimate_operations({**models, **override})
    st.caption("Ожидаются платные анализ, поиск тем, планирование, исследования, тексты и оформление. "
               f"Статей: {override['max_articles_per_week']}. Исследований не более {operations['Исследования (максимум)']}. "
               "Точная стоимость появится после ответа сервиса. Темы и готовые материалы проходят отдельное утверждение.")
    consent = st.checkbox("Разрешаю AI-расходы на подготовку этой недели", key="wizard-consent")
    if st.button("Начать подготовку", type="primary", disabled=bool(problems) or not consent or store.settings()["stop_requested"]):
        try:
            with exclusive_lock(store.database_path.parent / "pipeline.lock"):
                if store.one("SELECT id FROM pipeline_runs WHERE week_start=?", (week,)):
                    raise ValueError("Неделя уже создана. Откройте её вместо повторного запуска.")
                run_id = create_run(store, week, settings_override=override)
            st.session_state.show_wizard = False
            st.session_state.active_run = run_id
            queue_run(store, run_id)
            st.rerun()
        except Exception as error:
            handle_error(error)
    if st.button("Отменить создание недели"):
        st.session_state.show_wizard = False
        st.rerun()


def recovery_panel(store, config, run, stage):
    from dzen_agent.ui_pages import action, rub
    receipt = failed_receipt(store, run, stage["stage"])
    st.write(error_explanation(run, stage, receipt))
    st.caption(f"Расходы запуска: {rub(store.costs(run_id=run['id'])['total'])}. Сохранённые ответы остаются в истории.")
    if not receipt:
        st.info("Активной заблокированной квитанции нет. Можно продолжить с сохранённых результатов.")
        return
    if receipt["status"] == "SUCCEEDED":
        st.caption("Ответ уже оплачен. После исправления кода его можно проверить без повторной оплаты этого запроса.")
        st.caption("Если ответ пройдёт проверку, подготовка продолжится на следующих платных этапах.")
        if st.button("Продолжить с сохранённым ответом", disabled=not config.aitunnel_api_key, key=f"replay-{run['id']}"):
            if queue_run(store, run["id"]):
                st.rerun()
    st.caption("Новый ответ модели потребует отдельной оплаты. При неизвестном результате сначала сверьте историю AITUNNEL.")
    approved = st.checkbox("Я проверил расходы и разрешаю новый платный запрос", key=f"consent-{run['id']}")
    analytics = stage["stage"] == "ANALYZE"
    label = "Повторить только анализ (платно)" if analytics else "Повторить этап и продолжить (платно)"
    if st.button(label, disabled=not approved or not config.aitunnel_api_key or store.settings()["stop_requested"], key=f"retry-{run['id']}"):
        def retry():
            authorize_retry(store, receipt["call_key"], confirmed=approved,
                            **({"analytics_run_id": run["id"]} if analytics else {}))
            return queue_run(store, run["id"], analytics_only=analytics)
        if action("Сохраняю разрешение и передаю задачу…", retry, success="Задача передана в фон. Ожидаем выполнение."):
            st.rerun()
    with st.expander("Технические детали"):
        st.code(run.get("error") or stage.get("error") or "Нет сообщения")
        st.caption(receipt["call_key"])
        if receipt.get("result_json"):
            st.json(json.loads(receipt["result_json"]), expanded=False)


def stage_timeline(store, run):
    groups = [("Аналитика", ["COLLECT_STATS", "ANALYZE"]), ("Поиск тем", ["DISCOVERY"]), ("План", ["PLAN"]),
        ("Утверждение тем", ["PLAN_REVIEW"]), ("Исследование", ["RESEARCH", "REFRESH_RESEARCH"]),
        ("Написание", ["WRITE"]), ("Оформление", ["IMAGE"]), ("Проверка", ["QA", "REVIEW"])]
    rows = run_stages(store, run)
    by_name = {r["stage"]: r for r in rows}
    for offset in (0, 4):
        for column, (name, names) in zip(st.columns(4), groups[offset:offset + 4], strict=True):
            states = [by_name[n]["status"] for n in names if n in by_name]
            status = "SUCCEEDED" if states and all(v == "SUCCEEDED" for v in states) else next(
                (v for v in ("ERROR", "RUNNING", "WAITING") if v in states), "PENDING")
            color = {"ERROR": "red", "RUNNING": "orange", "SUCCEEDED": "green"}.get(status, "gray")
            column.markdown(f"**{name}**")
            column.caption(f":{color}[{human_status(status)}]")
    last = max((r.get("ended_at") or r.get("started_at") or "" for r in rows), default="")
    if last:
        st.caption("Последнее действие: " + datetime.fromisoformat(last).astimezone(MSK).strftime("%d.%m · %H:%M:%S") + " · Москва")


@st.fragment(run_every=5)
def run_card(store, config, run_id):
    from dzen_agent.ui_pages import rub
    run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    if not run:
        return
    stage = current_stage(store, run)
    settings = store.settings()
    dispatch = st.session_state.get("dispatch", {})
    if dispatch.get("run_id") == run_id and (run["status"], run["error"], run["ended_at"]) != tuple(dispatch["signature"]):
        st.session_state.pop("dispatch", None)
        dispatch = {}
    queued = dispatch.get("run_id") == run_id
    with st.container(border=True):
        st.caption(f"ТЕКУЩАЯ НЕДЕЛЯ · ЗАПУСК №{run_id}")
        st.subheader("Подготовка материалов · " + week_label(run["week_start"]))
        st.write("Передано в фон. Ожидаем выполнение." if queued else "**" + human_status(run["status"]) + "**")
        if run["status"] == "RUNNING" and settings["stop_requested"]:
            st.warning("Остановка запрошена. Уже отправленный запрос может завершиться; новые этапы не начнутся.")
        elif queued or run["status"] == "RUNNING":
            st.info("Агент работает. Состояние обновляется автоматически; время завершения зависит от AI-сервиса.")
        elif settings["stop_requested"] or run["status"] == "STOPPED":
            st.info("Работа приостановлена. Результаты сохранены.")
            if st.button("Возобновить работу агента", type="primary", key=f"unstop-{run_id}"):
                store.set_setting("stop_requested", False)
                if run["status"] == "STOPPED":
                    queue_run(store, run_id)
                st.rerun()
        elif run["status"] == "AWAITING_PLAN_REVIEW":
            st.info("Проверьте темы и ракурсы перед глубоким исследованием и написанием.")
            if st.button("Проверить контент-план", type="primary", key=f"plan-{run_id}"):
                navigate("Контент-план", selected_plan_run=run_id)
        elif run["status"] in ("AWAITING_REVIEW", "APPROVED", "AWAITING_SEND", "SUCCEEDED") or (stage and stage["stage"] in ("DZEN_SCHEDULE", "VERIFY")):
            from dzen_agent.browser.publisher import DzenPublisher
            ready = DzenPublisher(store, config).verified
            st.info("Проверьте материалы и расписание. Утверждённые статьи можно передать в отложенные." if ready else
                    "Откройте материалы для чтения и итогового утверждения. Сначала нужна проверка отправки на вашем канале.")
            if st.button("Проверить материалы", type="primary", key=f"materials-{run_id}"):
                navigate("Материалы", material_run=run_id)
            if run["approved_at"] and run["status"] != "SUCCEEDED":
                if st.button("Продолжить отправку в Дзен", disabled=not ready, key=f"send-{run_id}"):
                    queue_run(store, run_id, send=True)
                    st.rerun()
                st.caption("AI-запросы не выполняются. Готовые отложенные статьи повторно не создаются.")
            for item in store.rows("SELECT a.title,p.* FROM publications p JOIN articles a ON a.id=p.article_id "
                                   "JOIN article_details d ON d.article_id=a.id WHERE d.run_id=? ORDER BY p.scheduled_at", (run_id,)):
                st.caption(item["title"] + " · " + human_status(item["status"]))
                if item["error"]:
                    st.warning(item["error"])
        elif stage:
            receipt = failed_receipt(store, run, stage["stage"]) if stage["status"] == "ERROR" else None
            if stage["status"] == "ERROR" and receipt:
                st.warning("Не удалось завершить этап «" + STAGES[stage["stage"]] + "».")
                st.caption(error_explanation(run, stage, receipt))
                if st.button("Разобраться с ошибкой", type="primary", key=f"error-{run_id}"):
                    st.session_state[f"recover-{run_id}"] = True
                if st.session_state.get(f"recover-{run_id}"):
                    recovery_panel(store, config, run, stage)
            elif stage["stage"] == "COLLECT_STATS" and not store.one("SELECT id FROM statistics_imports"):
                st.info("Сначала загрузите статистику вашего канала.")
                if st.button("Загрузить статистику", type="primary", key=f"stats-{run_id}"):
                    navigate("Аналитика")
            else:
                st.caption("Продолжение использует готовые результаты. Последующие AI-этапы оплачиваются до проверки плана редактором.")
                label = "Продолжить поиск тем" if stage["stage"] == "DISCOVERY" else "Продолжить подготовку"
                if st.button(label, type="primary", disabled=not config.aitunnel_api_key, key=f"resume-{run_id}"):
                    if queue_run(store, run_id):
                        st.rerun()
                if stage["stage"] == "DISCOVERY":
                    st.caption("Аналитика сохранена и повторяться не будет. Поиск тем продолжится с текущего места.")
                    if store.one("SELECT call_key FROM ai_calls WHERE call_key LIKE ?", (f"run:{run_id}:discovery:archived:%",)):
                        st.caption("Разрешение повтора поиска тем уже сохранено.")
        if not config.aitunnel_api_key:
            st.caption("AI-действия доступны после добавления ключа AITUNNEL в .env.")
        cols = st.columns(3)
        target = json.loads(run["settings_json"])["max_articles_per_week"]
        written = len(store.rows("SELECT article_id FROM article_details WHERE run_id=?", (run_id,)))
        cols[0].metric("Текстов подготовлено", f"{written} из {target}")
        costs = store.costs(run_id=run_id)
        cols[1].metric("Подтверждённые расходы", rub(costs["total"]))
        if costs["unknown"]:
            cols[1].caption("Есть неподтверждённая стоимость")
        cols[2].caption("Текущий этап")
        cols[2].write("**" + (STAGES.get(stage["stage"], "Завершено") if stage else "Нет этапов") + "**")
        with st.expander("Ход подготовки", expanded=True):
            stage_timeline(store, run)


def dashboard(store, config):
    from dzen_agent.ui_pages import costs_page
    st.title("Рабочий стол")
    st.caption("Перемотка · от идеи до готового материала")
    settings = store.settings()
    last_balance = store.one("SELECT balance_after,timestamp FROM usage_events WHERE balance_after IS NOT NULL ORDER BY id DESC LIMIT 1")
    from dzen_agent.browser.publisher import DzenPublisher
    dzen_ready = DzenPublisher(store, config).verified
    st.caption(MODES.get(settings["mode"], settings["mode"]) + " · " + ("Ключ AI настроен" if config.aitunnel_api_key else "Нужен ключ AI")
        + (f" · Баланс по последнему ответу: {last_balance['balance_after']:.2f} ₽" if last_balance else " · Баланс ещё не проверен")
        + (" · Дзен: отложенная публикация проверена" if dzen_ready else " · Дзен: требуется ручная проверка"))
    overview, expenses = st.tabs(["Производство", "Расходы"])
    with overview:
        if st.session_state.get("show_wizard"):
            wizard(store, config)
        else:
            runs = store.rows("SELECT * FROM pipeline_runs ORDER BY id DESC")
            if runs:
                ids = [r["id"] for r in runs]
                if st.session_state.get("active_run") not in ids:
                    st.session_state.active_run = next((r["id"] for r in runs if r["status"] != "SUCCEEDED"), ids[0])
                run_id = st.selectbox("Рабочая неделя", ids, key="active_run", format_func=lambda i:
                    next(f"{week_label(r['week_start'])} · {human_status(r['status'])} · №{i}" for r in runs if r["id"] == i))
                run_card(store, config, run_id)
                if st.button("Подготовить другую неделю", type="tertiary"):
                    st.session_state.show_wizard = True
                    st.rerun()
            else:
                with st.container(border=True):
                    st.subheader("Ваша редакционная неделя начинается здесь")
                    st.write("Загрузите статистику, выберите период и число публикаций. Перед созданием текстов вы утвердите темы.")
                    if st.button("Подготовить материалы", type="primary"):
                        st.session_state.show_wizard = True
                        st.rerun()
                if not store.one("SELECT id FROM statistics_imports") and st.button("Сначала загрузить статистику"):
                    navigate("Аналитика")
    with expenses:
        costs_page(store, config)
