from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import streamlit as st

from dzen_agent.browser.session import launch_session
from dzen_agent.browser.statistics_download import STATUS as CSV_DOWNLOAD_STATUS
from dzen_agent.errors import UserActionRequired, log_exception, redact
from dzen_agent.pipeline.orchestrator import stages_for
from dzen_agent.analytics_contract import analytics_summary, briefs, parse_analytics
from dzen_agent.analytics_input import prepare_analytics_input, validate_metadata
from dzen_agent.prompt_service import PROMPT_TYPES, VARIABLES, Prompts
from dzen_agent.provider import AITunnel
from dzen_agent.services import ContentService
from dzen_agent.statistics import ALIASES, import_statistics, preview_statistics
from dzen_agent.statistics_excel import excel_sheets
from dzen_agent.store import dumps

DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
PROMPT_NAMES = {"ANALYTICS": "Analytics", "RESEARCH": "Research", "PLANNER": "Content Planner",
                "WRITER": "Writer", "IMAGE": "Image", "EDITOR": "Editorial Chat / Груминг", "DISCOVERY": "Discovery"}


def rub(value):
    return "Нет данных" if value is None else f"{value:,.2f} ₽".replace(",", " ").replace(".", ",")


def handle_error(error):
    path = log_exception(error, "UI")
    if isinstance(error, (ValueError, UserActionRequired)):
        st.error(redact(str(error)))
    else:
        st.error("Операция не завершена. Проверьте подключение и настройки. "
                 f"Подробности: logs/{path.name}")


def action(label, function, *, success="Сохранено."):
    try:
        with st.spinner(label):
            result = function()
        st.success(success)
        return result
    except Exception as error:
        handle_error(error)
        return None


def provider(store, config):
    return AITunnel(store, config.aitunnel_api_key)


def service(store, config):
    return ContentService(store, provider(store, config))


def show_analytics(payload):
    result = parse_analytics(payload)
    discovery, planner = briefs(payload)
    st.write(analytics_summary(payload))
    if payload.get("contract_version") == "1.0":
        st.caption(f"Анализ статистики · {'Готов' if result.status == 'ok' else 'Есть ограничения данных'} · публикаций: {result.data_quality.articles_total} "
                   f"· пригодных: {result.data_quality.articles_usable}")
        for issue in result.data_quality.issues + result.data_quality.comparison_limitations:
            st.warning(issue)
        for observation in result.observations:
            st.write(f"**{observation.id}.** {observation.finding}")
        for request in result.missing_data_requests:
            st.info(request)
    else:
        st.caption("Архивный формат Analytics; исходный результат сохранён.")
    names = {"promising_directions": "Перспективные направления", "underexplored_directions": "Что ещё стоит исследовать",
        "avoid_repetition": "Чего не повторять", "research_questions": "Вопросы для исследования",
        "content_mix_guidance": "Сочетание материалов", "priority_clusters": "Приоритетные темы", "caution_clusters": "Темы с оговорками",
        "headline_insights": "Заголовки", "format_insights": "Форматы", "priority_experiment_ids": "Приоритетные эксперименты"}
    for heading, brief in (("Направления поиска тем", discovery), ("Рекомендации для контент-плана", planner)):
        st.markdown("**" + heading + "**")
        for key, value in brief.items():
            if value:
                st.caption(names.get(key, key.replace("_", " ")))
                if isinstance(value, list):
                    for item in value:
                        st.write("• " + item)
                else:
                    st.write(value)
    with st.expander("Технические детали анализа"):
        st.json(payload, expanded=False)


def show_account(store, config):
    st.subheader("AITUNNEL")
    if not config.aitunnel_api_key:
        st.info("Добавьте AITUNNEL_API_KEY в .env в папке проекта и нажмите «Обновить баланс».")
        st.button("Обновить баланс", disabled=True)
        return
    if st.button("Обновить баланс"):
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
    order = stages_for(json.loads(run["settings_json"]))
    st.dataframe(sorted(stages, key=lambda row: order.index(row["stage"]) if row["stage"] in order else 99),
                 hide_index=True, use_container_width=True)
    st.write("Стоимость запуска:", rub(store.costs(run_id=run["id"])["total"]))


def dashboard(store, config):
    from dzen_agent.ui_workflow import dashboard as render_page
    render_page(store, config)


def statistics_page(store, config):
    st.title("Аналитика")
    st.caption("Импортируйте оригинальную выгрузку Дзена. Анализ для текущей недели сохраняется и не повторяется при продолжении.")
    upload = st.file_uploader("Выгрузка статистики Дзена", type=["xlsx", "csv", "tsv", "txt"])
    if upload:
        try:
            raw = upload.getvalue()
            sheet_name, header_row = None, None
            if Path(upload.name).suffix.lower() == ".xlsx":
                file_key = hashlib.sha256(raw).hexdigest()
                sheets = excel_sheets(raw)
                sheet_name = st.selectbox("Лист Excel", sheets, key=f"sheet-{file_key}") if len(sheets) > 1 else sheets[0]
                if st.checkbox("Выбрать строку заголовков вручную", key=f"header-manual-{file_key}-{sheet_name}"):
                    header_row = st.number_input("Номер строки заголовков", 1, 50, 1,
                        key=f"header-row-{file_key}-{sheet_name}")
            preview = preview_statistics(raw, upload.name, sheet_name=sheet_name, header_row=header_row)
            if preview.source_format == "xlsx":
                st.caption(f"Лист: {preview.sheet_name} · строка заголовков: {preview.header_row} · строк данных: {len(preview.rows)}")
                st.caption("Процентные ячейки приводятся к единицам CSV: 3,5% → 3,5. "
                           "Все колонки сохраняются, включая не сопоставленные с показателями.")
            else:
                st.caption(f"Строк: {len(preview.rows)} · кодировка: {preview.encoding} · "
                           f"разделитель: {repr(preview.delimiter)}")
            st.dataframe(preview.rows[:30], hide_index=True, use_container_width=True)
            mapping = {}
            with st.expander("Сопоставить колонки", expanded=not preview.mapping):
                options = ["Сохранить как есть"] + list(ALIASES)
                for column in preview.columns:
                    default = preview.mapping.get(column, "Сохранить как есть")
                    target = st.selectbox(column, options, index=options.index(default),
                                          format_func=lambda v: ALIASES[v][0].capitalize() if v in ALIASES else v,
                                          key=f"map-{preview.sha256}-{column}")
                    if target != "Сохранить как есть":
                        mapping[column] = target
            if st.button("Сохранить импорт", type="primary"):
                result = action("Сохраняю статистику…", lambda: import_statistics(store, upload.name, preview, mapping))
                if result:
                    st.info(f"Импорт №{result} сохранён.")
        except ValueError as error:
            st.error(str(error))
    imports = store.rows("SELECT id,filename,created_at,encoding,metadata_json FROM statistics_imports ORDER BY id DESC")
    if imports:
        st.dataframe([{"Импорт №": r["id"], "Файл": r["filename"], "Добавлен": r["created_at"][:16].replace("T", " · "),
                       "Лист": json.loads(r["metadata_json"]).get("source", {}).get("sheet_name", "")}
                      for r in imports], hide_index=True, use_container_width=True)
        selected = st.selectbox("Импорт для анализа", [r["id"] for r in imports])
        with st.expander("Проверка данных для Analytics — без AI"):
            context, audit = prepare_analytics_input(store, selected)
            quality = audit["data_quality"]
            st.write(f"Строк в импорте: {quality['source_rows']}; сводных строк исключено: {quality['aggregate_rows']}; "
                     f"публикаций обработано: {quality['unique_articles']}; примеров для AI: {len(context['articles'])}.")
            st.caption(f"Контекст: {len(dumps(context)):,} символов. Это не подсчёт токенов модели. "
                       "Сводка рассчитана по всем публикациям. Исходный импорт сохранён без изменений.")
            st.json(context["metric_definitions"])
            st.json(quality)
            st.dataframe([{"Показатель": k, **v} for k, v in context["channel_metrics"]["metric_statistics"].items()],
                         hide_index=True, use_container_width=True)
            if audit["rankings"]:
                metric = st.selectbox("Полный рейтинг публикаций по показателю", list(audit["rankings"]))
                st.dataframe(audit["rankings"][metric], hide_index=True, use_container_width=True)
            st.caption("Превышение открытий над показами требует уточнения определений и источников трафика; "
                       "это не автоматическая ошибка. Дата анализа не заменяет неизвестный период или as_of.")
        with st.expander("Отдельный анализ вне подготовки недели"):
            st.caption("Создаёт новый оплачиваемый анализ. Для продолжения текущей недели используйте рабочий стол.")
            analysis_consent = st.checkbox("Разрешаю оплату отдельного анализа")
            if st.button("Проанализировать статистику", disabled=not config.aitunnel_api_key or not analysis_consent):
                action("Анализирую…", lambda: service(store, config).analyze(selected, "analytics:" + uuid4().hex))
    for row in store.rows("SELECT * FROM analytics_results ORDER BY id DESC LIMIT 10"):
        with st.expander(f"Анализ №{row['id']} · {row['created_at']}"):
            show_analytics(json.loads(row["payload_json"]))
    if imports:
        with st.expander("Период измерения выбранного импорта"):
            st.caption("Укажите только известные значения. Дата загрузки файла не считается датой формирования выгрузки.")
            current = store.one("SELECT metadata_json FROM statistics_imports WHERE id=?", (selected,))
            metadata = json.loads(current["metadata_json"])
            windows = ["unknown", "lifetime", "calendar_period", "first_24h", "first_72h", "first_7d"]
            names = ["Неизвестен", "Накопительные показатели", "Календарный период", "Первые 24 часа", "Первые 72 часа", "Первые 7 дней"]
            window = st.selectbox("Окно измерения", windows, index=windows.index(metadata.get("measurement_window", "unknown")),
                                 format_func=lambda value: names[windows.index(value)])
            period = metadata.get("period", {})
            start = st.date_input("Начало периода, если известно", value=datetime.fromisoformat(period["start"]).date() if period.get("start") else None)
            end = st.date_input("Конец периода, если известно", value=datetime.fromisoformat(period["end"]).date() if period.get("end") else None)
            as_of = st.date_input("Дата формирования выгрузки, если известна", value=datetime.fromisoformat(period["as_of"]).date() if period.get("as_of") else None)
            attribution = st.checkbox("Подписки в выгрузке достоверно относятся к отдельным статьям", value=metadata.get("subscription_attribution_confirmed", False))
            ctr_confirmed = st.checkbox("Подтверждаю: открытия и показы имеют общий охват и позволяют считать CTR",
                value=metadata.get("metric_definitions", {}).get("ctr_compatible", False),
                help="Не отмечайте только на основании названий колонок. Сначала проверьте определения счётчиков Дзена.")
            if st.button("Сохранить сведения о периоде"):
                def save_metadata():
                    value = validate_metadata({**metadata, "measurement_window": window,
                        "subscription_attribution_confirmed": attribution,
                        "metric_definitions": {**metadata.get("metric_definitions", {}), "ctr_compatible": ctr_confirmed},
                        "period": {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None,
                                   "as_of": as_of.isoformat() if as_of else None, "timezone": "Europe/Moscow"}})
                    store.execute("UPDATE statistics_imports SET metadata_json=? WHERE id=?", (dumps(value), selected))
                    return True
                action("Сохраняю сведения…", save_metadata)
    st.caption("Автоматическая загрузка: " + CSV_DOWNLOAD_STATUS)
    if st.button("Открыть Дзен для входа / диагностики", key="stats-login"):
        action("Открываю браузер…", launch_session)


def content_plan(store, config):
    from dzen_agent.ui_content import content_plan as render_page
    render_page(store, config)


def articles_page(store, config):
    from dzen_agent.ui_content import articles_page as render_page
    render_page(store, config)


def invalidate_approval(store, article_id):
    from dzen_agent.database import _invalidate_run
    with store._connect() as conn:
        conn.execute("UPDATE articles SET status='WRITTEN' WHERE id=? AND status IN "
                     "('APPROVED','READY_FOR_REVIEW')", (article_id,))
        _invalidate_run(conn, article_id)


def prompts_page(store, config):
    st.subheader("Промпты и история версий")
    st.caption("Пользовательские версии сохраняются дословно. Промпт новой роли добавляется "
               "только после вашего сохранения; существующие версии остаются в истории.")
    menu, editor = st.columns([1, 3], gap="large")
    with menu:
        kind = st.radio("Назначение", PROMPT_TYPES, format_func=lambda x: PROMPT_NAMES[x])
    prompts = Prompts(store)
    active = prompts.active(kind)
    if not active:
        st.warning("Промпт ещё не задан. Вставьте согласованный текст и сохраните первую активную версию. "
                   "Приложение не заменяет его промптом другой роли.")
        active = {"id": 0, "title": PROMPT_NAMES[kind], "content": "", "version": 0, "created_at": ""}
    else:
        st.write(f"Активная версия: **{active['version']}** · {active['created_at']}")
    with editor, st.form(f"prompt-{kind}-{active['id']}"):
        title = st.text_input("Название", value=active["title"])
        content = st.text_area("Текст промпта", value=active["content"], height=400)
        if st.form_submit_button("Сохранить новую версию", type="primary"):
            if action("Сохраняю версию…", lambda: prompts.save(kind, title, content)):
                st.rerun()
    with st.expander("Переменные шаблона"):
        st.caption(", ".join("{{" + v + "}}" for v in sorted(VARIABLES)))
    history = store.rows("SELECT * FROM prompt_versions WHERE prompt_type=? ORDER BY version DESC", (kind,))
    if not history:
        return
    with st.expander("История версий промпта"):
        version_id = st.selectbox("Открыть версию из истории", [p["id"] for p in history],
                              format_func=lambda value: next(f"v{p['version']} · {p['created_at']}"
                                                              for p in history if p["id"] == value))
        old = next(p for p in history if p["id"] == version_id)
        st.text(old["content"])
        confirmed = st.checkbox("Сделать эту редакцию новой активной версией")
        if st.button("Восстановить как новую активную версию", disabled=not confirmed):
            if action("Восстанавливаю…", lambda: prompts.restore(version_id)):
                st.rerun()


def models_page(store, config):
    from dzen_agent.ui_settings import settings_page as render_page
    render_page(store, config)


def autopilot_page(store, config):
    from dzen_agent.ui_settings import system_page as render_page
    render_page(store, config)


def costs_page(store, config):
    st.subheader("Расходы в рублях")
    columns = st.columns(2)
    columns[0].metric("Локально за сегодня", rub(store.costs(days=1)["total"]))
    columns[1].metric("Локально за 30 дней", rub(store.costs(days=30)["total"]))
    if store.costs(days=30)["unknown"]:
        st.warning("Есть запросы без подтверждённой стоимости. Сверьте расходы с AITUNNEL; "
                   "неизвестная стоимость не считается нулевой.")
    st.caption("Общие анализ и планирование относятся ко всей неделе. Стоимость отдельных статей включает только связанные с ними операции.")
    rows = store.rows("SELECT timestamp,pipeline_run_id,article_id,operation,model,cost_rub,"
                            "balance_after,success,error,request_id FROM usage_events ORDER BY id DESC "
                            "LIMIT 1000")
    operations = {"ANALYTICS": "Анализ статистики", "DISCOVERY": "Поиск тем", "PLANNING": "Контент-план", "RESEARCH": "Исследование",
        "WRITE": "Написание", "IMAGE_SEARCH": "Поиск фотографий", "IMAGE_INSPECT": "Проверка фотографий", "EDITOR_CHAT": "Редакторский чат", "IMAGE": "Обложка"}
    st.dataframe([{"Дата": r["timestamp"], "Неделя №": r["pipeline_run_id"], "Материал №": r["article_id"],
        "Операция": operations.get(r["operation"], "Дополнительная операция"), "Модель": r["model"],
        "Стоимость, ₽": r["cost_rub"], "Результат": "Ответ получен" if r["success"] else "Есть ошибка"} for r in rows], hide_index=True)
    for column, label in [("pipeline_run_id", "запускам"), ("article_id", "статьям")]:
        st.subheader("Стоимость по " + label)
        totals = store.rows(f"SELECT {column},SUM(cost_rub) AS cost_rub,COUNT(*) AS requests "
                            f"FROM usage_events WHERE {column} IS NOT NULL GROUP BY {column}")
        st.dataframe([{"Неделя №" if column == "pipeline_run_id" else "Материал №": r[column],
            "Стоимость, ₽": r["cost_rub"], "Обращений": r["requests"]} for r in totals], hide_index=True)
    with st.expander("Техническая детализация расходов"):
        st.dataframe(rows, hide_index=True)


PAGES = {"Рабочий стол": dashboard, "Контент-план": content_plan, "Материалы": articles_page,
         "Аналитика": statistics_page, "Настройки": models_page, "Система": autopilot_page}
