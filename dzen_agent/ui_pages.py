from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

import streamlit as st

from dzen_agent import scheduler
from dzen_agent.browser.scheduling import DzenScheduling, UNVERIFIED
from dzen_agent.browser.session import launch_session
from dzen_agent.browser.statistics_download import STATUS as CSV_DOWNLOAD_STATUS
from dzen_agent.errors import UserActionRequired, log_exception, redact
from dzen_agent.pipeline.orchestrator import STAGES, approve_week, create_run, estimate_operations
from dzen_agent.plan_review import approve_plan, editing_reason, is_approved
from dzen_agent.prompt_service import PROMPT_TYPES, VARIABLES, Prompts
from dzen_agent.provider import AITunnel, choose_defaults, fetch_catalog
from dzen_agent.recovery import authorize_retry, refresh_pending_config
from dzen_agent.services import ContentService, qa_article
from dzen_agent.statistics import ALIASES, import_csv, preview_csv
from dzen_agent.store import MSK, dumps, now_iso

DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
PROMPT_NAMES = {"ANALYTICS": "Analytics", "RESEARCH": "Research", "PLANNER": "Content Planner",
                "WRITER": "Writer", "IMAGE": "Image", "EDITOR": "Editorial Chat / Груминг"}


def rub(value):
    return "Нет данных" if value is None else f"{value:,.2f} ₽".replace(",", " ").replace(".", ",")


def handle_error(error):
    path = log_exception(error, "UI")
    if isinstance(error, (ValueError, UserActionRequired)):
        st.error(redact(str(error)))
    else:
        st.error("Операция не завершена. Проверьте подключение и настройки. "
                 f"Подробности: logs/{path.name}")


def action(label, function):
    try:
        with st.spinner(label):
            result = function()
        st.success("Готово.")
        return result
    except Exception as error:
        handle_error(error)
        return None


def provider(store, config):
    return AITunnel(store, config.aitunnel_api_key)


def service(store, config):
    return ContentService(store, provider(store, config))


def show_account(store, config):
    st.subheader("AITUNNEL")
    if not config.aitunnel_api_key:
        st.info("Добавьте AITUNNEL_API_KEY в .env в папке проекта и нажмите «Обновить баланс».")
        st.button("Обновить баланс", disabled=True)
        return
    if st.button("Обновить баланс") or "account_attempted" not in st.session_state:
        st.session_state.account_attempted = True
        result = action("Проверяю AITUNNEL…", lambda: provider(store, config).account())
        if result:
            st.session_state.account = result
        else:
            st.session_state.pop("account", None)
    account = st.session_state.get("account")
    if account:
        st.success("AITUNNEL отвечает")
        columns = st.columns(3)
        columns[0].metric("Баланс аккаунта", rub(account["balance"]))
        columns[1].metric("Расход сегодня", rub(account["today_spend"]))
        columns[2].metric("Расход за 30 дней", rub(account["month_spend"]))
        if account["budget_remaining"] is not None:
            st.metric("Остаток бюджета ключа", rub(account["budget_remaining"]))
        st.write(f"Запросов сегодня: **{account['today_requests']}** · "
                 f"за 30 дней: **{account['month_requests']}**")
        st.write("Самая затратная модель:", account["top_model_by_spend"] or "Пока нет")
        st.caption(f"Получено: {account['fetched_at']}. Расходы относятся к текущему ключу; "
                   "AITUNNEL может кэшировать сводку около часа.")


def show_run(store, run):
    st.write(f"**Запуск №{run['id']} · неделя {run['week_start']} · {run['status']}**")
    if run.get("error"):
        st.warning(run["error"])
    if run["status"] == "AWAITING_PLAN_REVIEW":
        st.info("Контент-план готов. Откройте «Контент-план», проверьте идеи и утвердите их перед созданием статей.")
    stages = store.rows("SELECT stage,status,started_at,ended_at,error,attempts "
                       "FROM pipeline_stages WHERE run_id=?", (run["id"],))
    st.dataframe(sorted(stages, key=lambda row: STAGES.index(row["stage"]) if row["stage"] in STAGES else 99),
                 hide_index=True, use_container_width=True)
    st.write("Стоимость запуска:", rub(store.costs(run_id=run["id"])["total"]))


def dashboard(store, config):
    show_account(store, config)
    settings = store.settings()
    st.subheader("Состояние агента")
    columns = st.columns(3)
    columns[0].metric("Режим", settings["mode"])
    columns[1].metric("Расписание", "Включено" if settings["scheduler_enabled"] else "Выключено")
    columns[2].metric("Статей готово", len(store.rows("SELECT id FROM articles WHERE status IN "
                                                     "('READY_FOR_REVIEW','APPROVED')")))
    st.write("Следующий ожидаемый запуск (Москва):", scheduler.next_run(settings) or "Не назначен")
    st.write("Подтверждено в отложенных:", len(store.rows(
        "SELECT id FROM publications WHERE status='SCHEDULED_VERIFIED'")))
    latest = store.one("SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 1")
    if latest:
        show_run(store, latest)
    st.info("Автоматическая постановка в отложенные пока не проверена на вашем канале. "
            "Подготовка материалов и V0-заполнение редактора доступны.")
    st.caption("План операций для новой недели:")
    st.json(estimate_operations(settings))
    if st.button("Подготовить неделю", type="primary", disabled=settings["stop_requested"]):
        def launch():
            run_id = create_run(store)
            scheduler.launch_pipeline(run_id)
            return run_id
        run_id = action("Запускаю подготовку…", launch)
        if run_id:
            st.info(f"Запуск №{run_id} работает в фоне. Смотрите раздел «Автопилот».")
    errors = store.rows("SELECT timestamp,stage,message FROM event_log ORDER BY id DESC LIMIT 5")
    if errors:
        with st.expander("Последние события"):
            st.dataframe(errors, hide_index=True, use_container_width=True)


def statistics_page(store, config):
    st.subheader("Статистика канала")
    upload = st.file_uploader("Выгрузка статистики Дзена", type=["csv", "tsv", "txt"])
    if upload:
        try:
            preview = preview_csv(upload.getvalue())
            st.caption(f"Строк: {len(preview.rows)} · кодировка: {preview.encoding} · "
                       f"разделитель: {repr(preview.delimiter)}")
            st.dataframe(preview.rows[:30], hide_index=True, use_container_width=True)
            mapping = {}
            with st.expander("Сопоставить колонки", expanded=not preview.mapping):
                options = ["Сохранить как есть"] + list(ALIASES)
                for column in preview.columns:
                    default = preview.mapping.get(column, "Сохранить как есть")
                    target = st.selectbox(column, options, index=options.index(default),
                                          key=f"map-{preview.sha256}-{column}")
                    if target != "Сохранить как есть":
                        mapping[column] = target
            if st.button("Сохранить импорт", type="primary"):
                result = action("Сохраняю статистику…", lambda: import_csv(store, upload.name, preview, mapping))
                if result:
                    st.info(f"Импорт №{result} сохранён.")
        except ValueError as error:
            st.error(str(error))
    imports = store.rows("SELECT id,filename,created_at,encoding FROM statistics_imports ORDER BY id DESC")
    if imports:
        st.dataframe(imports, hide_index=True, use_container_width=True)
        selected = st.selectbox("Импорт для анализа", [r["id"] for r in imports])
        if st.button("Проанализировать статистику", disabled=not config.aitunnel_api_key):
            action("Анализирую…", lambda: service(store, config).analyze(selected, "analytics:" + uuid4().hex))
    for row in store.rows("SELECT * FROM analytics_results ORDER BY id DESC LIMIT 10"):
        with st.expander(f"Анализ №{row['id']} · {row['created_at']}"):
            st.json(json.loads(row["payload_json"]))
    st.caption("Автоматическая загрузка: " + CSV_DOWNLOAD_STATUS)
    if st.button("Открыть Дзен для входа / диагностики", key="stats-login"):
        action("Открываю браузер…", launch_session)


def content_plan(store, config):
    from dzen_agent.editorial_ui import grooming_panel

    st.subheader("Недельный контент-план")
    plans = store.rows("SELECT * FROM content_plans ORDER BY id DESC")
    if not plans:
        st.info("Нажмите «Подготовить неделю» на главной после загрузки статистики и выбора моделей.")
        return
    plan_id = st.selectbox("Неделя", [p["id"] for p in plans], format_func=lambda value:
                           next(p["week_start"] for p in plans if p["id"] == value))
    run_id = next(p["run_id"] for p in plans if p["id"] == plan_id)
    plan = next(p for p in plans if p["id"] == plan_id)
    left, right = st.columns([3, 2], gap="large")
    with left:
        reason = editing_reason(store, plan_id)
        if reason:
            st.info(reason)
        elif is_approved(plan):
            st.success("Контент-план утверждён. Можно продолжать подготовку статей.")
        else:
            st.info("Контент-план готов. Проверьте идеи перед созданием статей.")
        for number, row in enumerate(store.rows(
                "SELECT * FROM concepts WHERE plan_id=? ORDER BY scheduled_at,id", (plan_id,)), 1):
            concept = json.loads(row["payload_json"])
            when = datetime.fromisoformat(row["scheduled_at"]).astimezone(MSK).strftime("%d.%m · %H:%M")
            with st.container(border=True):
                st.markdown(f"**{number}. {concept['working_title']}**")
                st.caption(f"{when} (Москва) · концепт №{row['id']} · версия {row['revision']}")
                st.write("Тема:", row["topic"])
                st.write("Ракурс:", concept["angle"])
                if row["needs_research"]:
                    st.warning("Для изменённой темы потребуется новое исследование.")
                with st.expander("Подробнее об идее"):
                    for field, label in [("why_now", "Почему сейчас"), ("audience_value", "Польза читателю"),
                                         ("thesis", "Главный тезис")]:
                        st.write(label + ":", concept[field])
                    for point in concept["key_points"]:
                        st.write("• " + point)
                    st.caption(f"Оценка: {concept['score']}")
        if st.button("Утвердить контент-план и продолжить", type="primary",
                     disabled=store.settings()["stop_requested"] or (is_approved(plan) and bool(reason))):
            def approve_and_continue():
                approve_plan(store, plan_id)
                scheduler.launch_pipeline(run_id)
                return True
            if action("Утверждаю контент-план…", approve_and_continue):
                st.success("Контент-план утверждён. Продолжаю подготовку статей.")
        st.caption("После создания статей и QA потребуется отдельное утверждение готовой недели.")
        qa = store.one("SELECT status FROM pipeline_stages WHERE run_id=? AND stage='QA'", (run_id,))
        if qa and qa["status"] == "SUCCEEDED":
            if st.button("Утвердить неделю"):
                action("Проверяю готовые материалы…", lambda: approve_week(store, run_id))
            st.caption("Отправка утверждённой недели — в разделе «Автопилот».")
    with right:
        grooming_panel(store, config, plan_id)


def articles_page(store, config):
    st.subheader("Статьи")
    with st.expander("Новая статья", expanded=not store.list_all()):
        topic = st.text_input("Тема статьи")
        if st.button("Исследовать тему и написать статью", disabled=not config.aitunnel_api_key):
            def generate():
                if not topic.strip():
                    raise ValueError("Введите тему статьи.")
                content = service(store, config)
                key = "manual:" + uuid4().hex
                research_id = content.research(topic.strip(), {}, key + ":research")
                return content.write(topic.strip(), research_id, key + ":write")
            article_id = action("Исследую и пишу…", generate)
            if article_id:
                st.session_state.selected_article = article_id
        with st.form("manual_article"):
            manual_title = st.text_input("Заголовок своего текста")
            manual_body = st.text_area("Свой текст", height=160)
            if st.form_submit_button("Сохранить свой черновик"):
                def create_manual():
                    if not manual_title.strip() or not manual_body.strip():
                        raise ValueError("Введите заголовок и текст.")
                    article = store.create(topic or manual_title, manual_title, manual_body)
                    store.execute("INSERT INTO article_details(article_id) VALUES(?)", (article.id,))
                    return article.id
                article_id = action("Сохраняю…", create_manual)
                if article_id:
                    st.session_state.selected_article = article_id
    articles = store.list_all()
    if not articles:
        st.info("Здесь появятся сохранённые статьи.")
        return
    ids = [a.id for a in articles]
    if st.session_state.get("selected_article") not in ids:
        st.session_state.selected_article = ids[0]
    article_id = st.selectbox("Открыть статью", ids, key="selected_article", format_func=lambda value:
                              next(f"№{a.id} · {a.title} · {a.status}" for a in articles if a.id == value))
    article, detail = store.get(article_id), store.detail(article_id)
    if not detail:
        store.execute("INSERT OR IGNORE INTO article_details(article_id) VALUES(?)", (article_id,))
        detail = store.detail(article_id)
    st.caption(f"Расходы, привязанные к статье: {rub(store.costs(article_id=article_id)['total'])}")
    with st.form(f"edit-{article_id}-{article.updated_at}"):
        title = st.text_input("Заголовок", value=article.title)
        body = st.text_area("Текст", value=article.body, height=400)
        if st.form_submit_button("Сохранить изменения"):
            action("Сохраняю…", lambda: store.update_content(article_id, title, body))
            st.rerun()
    columns = st.columns(3)
    for column, label, part in zip(columns, ["Перегенерировать", "Только заголовок", "Только текст"],
                                    ["all", "title", "body"], strict=True):
        if column.button(label, disabled=not config.aitunnel_api_key):
            def regenerate(part=part):
                content = service(store, config)
                research_id = detail.get("research_id")
                if not research_id:
                    research_id = content.research(article.topic, {}, "manual-research:" + uuid4().hex)
                    store.execute("UPDATE article_details SET research_id=? WHERE article_id=?",
                                   (research_id, article_id))
                return content.write(article.topic, research_id, "regenerate:" + uuid4().hex,
                                      article_id=article_id, part=part)
            if action("Обновляю материал…", regenerate):
                st.rerun()
    with st.expander("История изменений"):
        for revision in store.rows("SELECT id,title,body,created_at FROM article_revisions "
                                    "WHERE article_id=? ORDER BY id DESC", (article_id,)):
            st.write(f"Версия {revision['id']} · {revision['created_at']} · {revision['title']}")
            st.text(revision["body"])
    st.subheader("Обложка")
    image = store.active_image(article_id)
    if image and Path(image["path"]).is_file():
        st.image(image["path"], width=550)
        st.caption(f"Источник: {image['origin']} · {image.get('model') or 'ваша картинка'}")
    if st.button("Сгенерировать заново" if image else "Сгенерировать обложку",
                 disabled=not config.aitunnel_api_key):
        if action("Генерирую обложку…", lambda: service(store, config).image(
                article_id, "image:" + uuid4().hex, force=True)):
            st.rerun()
    own_image = st.file_uploader("Загрузить свою картинку", type=["png", "jpg", "jpeg", "webp"],
                                 key=f"image-upload-{article_id}")
    if own_image and st.button("Использовать мою картинку"):
        if action("Сохраняю обложку…", lambda: service(store, config).upload_image(article_id, own_image.getvalue())):
            st.rerun()
    with st.form(f"schedule-{article_id}"):
        scheduled = datetime.fromisoformat(detail["scheduled_at"]) if detail.get("scheduled_at") else (
            datetime.now(MSK) + timedelta(days=7))
        date_value = st.date_input("Дата публикации", value=scheduled.date())
        time_value = st.time_input("Время публикации (Москва)", value=scheduled.time())
        if st.form_submit_button("Сохранить дату и время"):
            value = datetime.combine(date_value, time_value, tzinfo=MSK).isoformat()
            store.execute("UPDATE article_details SET scheduled_at=?,qa_json=NULL WHERE article_id=?",
                           (value, article_id))
            invalidate_approval(store, article_id)
            st.success("Дата сохранена; требуется новая проверка QA.")
    if st.button("Проверить статью (QA)"):
        result = action("Проверяю…", lambda: qa_article(store, article_id))
        if result:
            if result["passed"]:
                st.success("QA пройден.")
            else:
                for issue in result["issues"]:
                    st.warning(issue)
    st.subheader("Дзен")
    st.caption("Подготовка V0 заполняет заголовок и текст. Обложку и публикацию проверьте в браузере.")
    if st.button("Подготовить в Дзене"):
        action("Открываю редактор…", lambda: launch_session(article_id))
    if st.button("Обновить статью в Дзене"):
        action("Проверяю соответствие публикации…", lambda: DzenScheduling(store).update(article_id))
    publication = store.one("SELECT * FROM publications WHERE article_id=?", (article_id,))
    if publication:
        st.write(publication["status"], publication["scheduled_at"])
        if publication["dzen_url"]:
            st.link_button("Открыть публикацию", publication["dzen_url"])


def invalidate_approval(store, article_id):
    from dzen_agent.database import _invalidate_run
    with store._connect() as conn:
        conn.execute("UPDATE articles SET status='WRITTEN' WHERE id=? AND status IN "
                     "('APPROVED','READY_FOR_REVIEW')", (article_id,))
        _invalidate_run(conn, article_id)


def prompts_page(store, config):
    st.subheader("Промпты и история версий")
    st.caption("Четыре авторских промпта пока не переданы. Стартовые шаблоны помечены; "
               "Writer использует прежние инструкции V0. Ваш текст сохраняется дословно.")
    kind = st.radio("Назначение", PROMPT_TYPES, format_func=lambda x: PROMPT_NAMES[x], horizontal=True)
    prompts = Prompts(store)
    active = prompts.active(kind)
    st.write(f"Активная версия: **{active['version']}** · {active['created_at']}")
    with st.form(f"prompt-{kind}-{active['id']}"):
        title = st.text_input("Название", value=active["title"])
        content = st.text_area("Текст промпта", value=active["content"], height=400)
        if st.form_submit_button("Сохранить новую версию", type="primary"):
            if action("Сохраняю версию…", lambda: prompts.save(kind, title, content)):
                st.rerun()
    st.caption("Переменные: " + ", ".join("{{" + v + "}}" for v in sorted(VARIABLES)))
    history = store.rows("SELECT * FROM prompt_versions WHERE prompt_type=? ORDER BY version DESC", (kind,))
    version_id = st.selectbox("Открыть версию из истории", [p["id"] for p in history],
                              format_func=lambda value: next(f"v{p['version']} · {p['created_at']}"
                                                              for p in history if p["id"] == value))
    old = next(p for p in history if p["id"] == version_id)
    st.text(old["content"])
    if st.button("Восстановить как новую активную версию"):
        if action("Восстанавливаю…", lambda: prompts.restore(version_id)):
            st.rerun()


def models_page(store, config):
    st.subheader("Настройки / Модели")
    settings = store.settings()
    st.write("Ключ AITUNNEL:", "добавлен в .env" if config.aitunnel_api_key else "не добавлен")
    catalog = settings.get("public_catalog", {})
    if st.button("Обновить каталог моделей", type="primary"):
        result = action("Получаю публичный каталог AITUNNEL…", fetch_catalog)
        if result:
            store.set_setting("public_catalog", result)
            store.set_setting("catalog_updated_at", now_iso())
            for key, value in choose_defaults(result).items():
                if not settings[key]:
                    store.set_setting(key, value)
            st.rerun()
    if not catalog:
        st.info("Загрузите каталог: для этого API-ключ не требуется.")
    else:
        st.caption("Каталог обновлён: " + settings.get("catalog_updated_at", "неизвестно"))
    with st.form("model_settings"):
        selected = {}
        for role in ("analytics", "research", "planner", "writer", "image"):
            group = "images" if role == "image" else "chat"
            options = list(catalog.get(group, {}))
            current = settings[role + "_model"]
            if current and current not in options:
                options.insert(0, current)
            if options:
                selected[role + "_model"] = st.selectbox(role.title() + " model", options,
                    index=options.index(current) if current in options else 0)
            else:
                selected[role + "_model"] = st.text_input(role.title() + " model ID", current)
        selected["research_use_web_tool"] = st.checkbox("Добавлять инструмент web search AITUNNEL",
            value=settings["research_use_web_tool"], help="Sonar имеет собственный поиск. "
            "Для другой модели включите инструмент. Поиск оплачивается отдельно.")
        selected["image_quality"] = st.selectbox("Качество изображения", ["low", "medium", "high", "auto"],
            index=["low", "medium", "high", "auto"].index(settings["image_quality"]))
        if st.form_submit_button("Сохранить модели"):
            for key, value in selected.items():
                store.set_setting(key, value)
            st.success("Модели сохранены для новых запусков.")
    if catalog:
        with st.expander("Назначение, цены и возможности моделей"):
            rows = []
            for group, models in catalog.items():
                for model_id, info in models.items():
                    rows.append({"ID": model_id, "Группа": group, "Назначение": info.get("description", ""),
                        "Вход ₽/1M токенов": info.get("prompt_cost"),
                        "Выход ₽/1M токенов": info.get("completion_cost"),
                        "Картинка от ₽": info.get("min_price_per_image"),
                        "Картинка до ₽": info.get("max_price_per_image"),
                        "Возможности": dumps(info.get("modalities", {}))})
            st.dataframe(rows, hide_index=True, use_container_width=True)
            st.caption("У research-моделей может быть дополнительная плата за поиск. "
                       "Точная стоимость каждого вызова сохраняется из usage.cost_rub.")
    with st.form("agent_settings"):
        values = {"channel_topic": st.text_input("Тематика и аудитория канала", settings["channel_topic"])}
        values["dzen_editor_url"] = st.text_input("Адрес редактора статьи в вашем канале", settings["dzen_editor_url"])
        numeric = [("max_articles_per_week", "Статей в неделю", 1, 7),
                   ("max_research_calls_per_article", "Исследований на статью (максимум)", 1, 3),
                   ("max_retries", "Повторов после ограничения частоты API", 0, 3),
                   ("statistics_max_age_days", "Срок актуальности импорта, дней", 1, 90),
                   ("max_output_tokens", "Лимит выходных токенов", 1000, 32000)]
        for key, label, low, high in numeric:
            values[key] = st.number_input(label, low, high, int(settings[key]))
        for key, label in [("monthly_soft_budget_rub", "Локальный бюджет за 30 дней, ₽"),
                           ("stop_when_balance_below_rub", "Остановиться при балансе ниже, ₽")]:
            values[key] = st.number_input(label, min_value=0.0, value=float(settings[key]), step=10.0)
        values["publication_days"] = st.multiselect("Дни публикаций", list(range(7)),
            default=settings["publication_days"], format_func=lambda day: DAYS[day])
        values["publication_time"] = st.time_input("Время публикаций (Москва)",
            time.fromisoformat(settings["publication_time"])).strftime("%H:%M")
        values["ai_qa_enabled"] = st.checkbox("Дополнительный платный AI-QA", settings["ai_qa_enabled"])
        if st.form_submit_button("Сохранить настройки"):
            if len(values["publication_days"]) < values["max_articles_per_week"]:
                st.error("Выберите не меньше дней, чем статей в неделю.")
            else:
                for key, value in values.items():
                    store.set_setting(key, value)
                st.success("Настройки сохранены.")


def autopilot_page(store, config):
    st.subheader("Автопилот и запуски")
    settings = store.settings()
    st.warning(UNVERIFIED)
    mode = st.radio("Режим работы", ["MANUAL", "REVIEW", "AUTOPILOT"],
                     index=["MANUAL", "REVIEW", "AUTOPILOT"].index(settings["mode"]), horizontal=True)
    confirmed = st.checkbox("Разрешаю автономные AI-запросы и отложенные публикации после проверки интеграции")
    if st.button("Применить режим"):
        if mode == "AUTOPILOT" and not confirmed:
            st.error("Для AUTOPILOT требуется явное подтверждение выше.")
        else:
            store.set_setting("mode", mode)
            st.success(f"Режим {mode} сохранён.")
    if settings["stop_requested"] and st.button("Возобновить работу агента"):
        store.set_setting("stop_requested", False)
        st.rerun()
    st.caption("Остановка действует перед следующим AI-вызовом или действием в Дзене. "
               "Уже отправленный запрос может завершиться и быть оплачен.")
    st.subheader("Расписание Windows")
    with st.form("scheduler"):
        day = st.selectbox("День запуска", list(range(7)), index=settings["scheduler_day"], format_func=lambda d: DAYS[d])
        at = st.time_input("Время запуска (Москва)", time.fromisoformat(settings["scheduler_time"]))
        if st.form_submit_button("Установить / обновить расписание"):
            store.set_setting("scheduler_day", day)
            store.set_setting("scheduler_time", at.strftime("%H:%M"))
            action("Устанавливаю расписание Windows…", lambda: scheduler.install(store))
    if st.button("Выключить расписание"):
        action("Выключаю расписание…", lambda: scheduler.remove(store))
    st.write("Следующий ожидаемый запуск:", scheduler.next_run(store.settings()) or "Не назначен")
    if st.button("Проверить задание Windows"):
        result = action("Проверяю…", scheduler.windows_task_status)
        if result:
            st.json(result)
    st.caption("Компьютер должен быть включён, пользователь — войти в Windows. "
               "Открытый Streamlit и вкладка браузера для запуска не нужны.")
    if st.button("Открыть браузер Дзена для входа / диагностики"):
        action("Открываю Chromium…", launch_session)
        st.info("Войдите вручную. После входа закройте окно Chromium перед следующей операцией.")
    if st.button("Обновить состояние запусков"):
        st.rerun()
    runs = store.rows("SELECT * FROM pipeline_runs ORDER BY id DESC")
    if runs:
        run_id = st.selectbox("Запуск", [r["id"] for r in runs])
        run = next(r for r in runs if r["id"] == run_id)
        show_run(store, run)
        if st.button("Продолжить подготовку", disabled=settings["stop_requested"]):
            action("Продолжаю с сохранённого этапа…", lambda: scheduler.launch_pipeline(run_id))
        if st.button("Утвердить неделю", key="autopilot-approve"):
            action("Проверяю неделю…", lambda: approve_week(store, run_id))
        if st.button("Отправить утверждённую неделю в Дзен", disabled=settings["stop_requested"]):
            action("Запускаю отправку…", lambda: scheduler.launch_pipeline(run_id, send=True))
        with st.expander("Восстановление после ошибки AI"):
            st.caption("Готовые результаты сохраняются. Здесь можно применить новые модели/промпты "
                       "к незавершённым этапам. Неопределённый запрос повторяется только после вашей проверки.")
            if st.button("Применить текущие модели и промпты к незавершённым этапам"):
                action("Обновляю настройки запуска…", lambda: refresh_pending_config(store, run_id))
            calls = store.rows("SELECT call_key,status,error,result_json FROM ai_calls WHERE call_key LIKE ? "
                                "AND call_key NOT LIKE '%:archived:%' ORDER BY created_at DESC", (f"run:{run_id}:%",))
            if calls:
                call_key = st.selectbox("Сохранённый запрос", [c["call_key"] for c in calls])
                receipt = next(c for c in calls if c["call_key"] == call_key)
                st.write("Статус:", receipt["status"])
                if receipt["error"]:
                    st.warning(receipt["error"])
                if receipt["result_json"]:
                    st.json(json.loads(receipt["result_json"]))
                retry_confirmed = st.checkbox("Я проверил историю AITUNNEL и разрешаю оплату нового запроса")
                if st.button("Разрешить повтор выбранного запроса"):
                    action("Сохраняю разрешение…", lambda: authorize_retry(store, call_key, confirmed=retry_confirmed))
                    st.info("Теперь нажмите «Продолжить подготовку». Уже готовые статьи не переписываются.")
    with st.expander("Журнал ошибок и диагностика"):
        st.dataframe(store.rows("SELECT * FROM event_log ORDER BY id DESC LIMIT 100"), hide_index=True,
                     use_container_width=True)


def costs_page(store, config):
    st.subheader("Расходы в рублях")
    columns = st.columns(2)
    columns[0].metric("Локально за сегодня", rub(store.costs(days=1)["total"]))
    columns[1].metric("Локально за 30 дней", rub(store.costs(days=30)["total"]))
    if store.costs(days=30)["unknown"]:
        st.warning("Есть запросы без подтверждённой стоимости. Сверьте расходы с AITUNNEL; "
                   "неизвестная стоимость не считается нулевой.")
    st.caption("Общие Analytics и Planner относятся к запуску. Основной баланс — на главной.")
    st.dataframe(store.rows("SELECT timestamp,pipeline_run_id,article_id,operation,model,cost_rub,"
                            "balance_after,success,error,request_id FROM usage_events ORDER BY id DESC "
                            "LIMIT 1000"), hide_index=True, use_container_width=True)
    for column, label in [("pipeline_run_id", "запускам"), ("article_id", "статьям")]:
        st.subheader("Стоимость по " + label)
        st.dataframe(store.rows(f"SELECT {column},SUM(cost_rub) AS cost_rub,COUNT(*) AS requests "
                                f"FROM usage_events WHERE {column} IS NOT NULL GROUP BY {column}"), hide_index=True)


PAGES = {"Главная": dashboard, "Статистика": statistics_page, "Контент-план": content_plan,
         "Статьи": articles_page, "Промпты": prompts_page, "Настройки / Модели": models_page,
         "Автопилот": autopilot_page, "Расходы": costs_page}
