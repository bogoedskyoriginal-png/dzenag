"""Calendar, grooming and a reading-first material workspace."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import streamlit as st

from dzen_agent.browser.session import launch_session
from dzen_agent.browser.scheduling import DzenScheduling
from dzen_agent.editorial_pipeline import native
from dzen_agent.editorial_ui import grooming_panel
from dzen_agent.pipeline.orchestrator import approve_week
from dzen_agent.plan_review import approve_plan, editing_reason, is_approved
from dzen_agent.prompt_service import Prompts
from dzen_agent.services import qa_article
from dzen_agent.store import MSK
from dzen_agent.ui_workflow import DAYS, human_status, navigate, queue_run, week_label


def content_plan(store, config):
    from dzen_agent.ui_pages import action
    st.title("Контент-план")
    st.caption("Расписание, темы и редакционное обсуждение · время по Москве")
    plans = store.rows("SELECT * FROM content_plans ORDER BY id DESC")
    if not plans:
        st.info("План появится после анализа статистики и поиска тем. Подготовка управляется на рабочем столе.")
        if st.button("Открыть рабочий стол", type="primary"):
            navigate("Рабочий стол")
        return
    selected_run = st.session_state.pop("selected_plan_run", None)
    ids = [p["id"] for p in plans]
    chosen = next((p["id"] for p in plans if p["run_id"] == selected_run), ids[0])
    plan_id = st.selectbox("Неделя плана", ids, index=ids.index(chosen), format_func=lambda i:
        next(week_label(p["week_start"]) for p in plans if p["id"] == i))
    plan = next(p for p in plans if p["id"] == plan_id)
    rows = store.rows("SELECT c.*,r.status AS research_status,d.article_id FROM concepts c "
        "LEFT JOIN research_jobs r ON c.research_id=r.id LEFT JOIN article_details d ON d.concept_id=c.id "
        "WHERE c.plan_id=? ORDER BY c.scheduled_at,c.id", (plan_id,))
    saved = json.loads(plan.get("payload_json") or "{}")
    for warning in saved.get("editorial_warnings", []):
        st.warning(warning)
    for missing in saved.get("unfilled_slots", []):
        st.warning(f"Осталось заполнить {missing['count']} мест. {missing['details']}")
    left, right = st.columns([3, 2], gap="large")
    with left:
        reason = editing_reason(store, plan_id)
        if is_approved(plan):
            st.success("Темы утверждены. Исследования выполняются после продолжения подготовки.")
        else:
            st.info("Проверьте темы и ракурсы. AI-редактор предлагает изменения; вы решаете, какие применить.")
        if reason:
            st.caption(reason)
        view = st.radio("Представление плана", ["По дням", "Все концепты"], horizontal=True)
        table = []
        for r in rows:
            payload = json.loads(r["payload_json"])
            when = datetime.fromisoformat(r["scheduled_at"]).astimezone(MSK)
            table.append({"День": when.strftime("%d.%m") + " · " + DAYS[when.weekday()], "Время": when.strftime("%H:%M"),
                "Заголовок": payload["working_title"], "Ракурс": payload["angle"],
                "Источник": "Discovery + аналитика" if payload.get("editorial_contract") else "Аналитика",
                "Исследование": "Готово" if r["research_status"] == "sufficient" and not r["needs_research"] else "После утверждения" if r["research_status"] == "pending" else "Требуется",
                "Материал": "Готов" if r["article_id"] else "Впереди"})
        if view == "По дням":
            for day in dict.fromkeys(t["День"] for t in table):
                with st.expander(day + " · " + str(sum(t["День"] == day for t in table)) + " публикации", expanded=len(table) <= 5):
                    st.dataframe([{k: v for k, v in t.items() if k != "День"} for t in table if t["День"] == day], hide_index=True)
        else:
            st.dataframe(table, hide_index=True, height=min(820, 40 + len(table) * 35))
        if rows:
            concept_id = st.selectbox("Подробнее о концепте", [r["id"] for r in rows], format_func=lambda i:
                next(f"{r['scheduled_at'][5:16].replace('T', ' · ')} · {json.loads(r['payload_json'])['working_title']}" for r in rows if r["id"] == i))
            row = next(r for r in rows if r["id"] == concept_id)
            payload = json.loads(row["payload_json"])
            with st.container(border=True):
                st.markdown("**" + payload["working_title"] + "**")
                st.write("Тема:", row["topic"])
                st.write("Ракурс:", payload["angle"])
                for field, label in (("why_now", "Почему сейчас"), ("audience_value", "Польза читателю"), ("thesis", "Главный тезис")):
                    st.write(label + ":", payload.get(field, "—"))
                for point in payload.get("key_points", []):
                    st.write("• " + point)
                if row["article_id"] and st.button("Открыть готовый материал"):
                    navigate("Материалы", material_run=plan["run_id"], selected_article=row["article_id"])
        if not is_approved(plan):
            st.caption("Утверждение запустит платные исследования, тексты и оформление. Следующая проверка — перед передачей готовых материалов.")
            if st.button("Утвердить контент-план и продолжить", type="primary", disabled=bool(reason) or store.settings()["stop_requested"] or not config.aitunnel_api_key):
                def approve():
                    approve_plan(store, plan_id)
                    return queue_run(store, plan["run_id"])
                if action("Утверждаю темы…", approve, success="Темы утверждены. Подготовка передана в фон."):
                    st.session_state.active_run = plan["run_id"]
                    st.rerun()
        elif st.button("Посмотреть подготовку материалов"):
            navigate("Рабочий стол", active_run=plan["run_id"])
    with right:
        grooming_panel(store, config, plan_id)


def visual_report(store, article_id):
    row = store.one("SELECT payload_json FROM visual_results WHERE article_id=? ORDER BY id DESC LIMIT 1", (article_id,))
    return json.loads(row["payload_json"]) if row else None


def preview_article(article, image, visual):
    if article.body.lstrip().partition("\n")[0] != "# " + article.title:
        st.subheader(article.title)
    document = None
    if visual:
        ref = visual.get("assembly", {}).get("assembled_document_ref")
        if ref and Path(ref).is_file():
            document = json.loads(Path(ref).read_text("utf-8"))
            if document.get("original_markdown") != article.body:
                st.info("Текст изменён после оформления. Показана актуальная редакция; расположение иллюстраций нужно перепроверить.")
                document = None
    if image and Path(image["path"]).is_file() and (not document or not any(b.get("role") == "cover" for b in document["blocks"])):
        st.image(image["path"], width="stretch")
    if document:
        for block in document["blocks"]:
            if block["type"] == "text":
                st.markdown(block["markdown"])
            elif Path(block["local_file_ref"]).is_file():
                path = image["path"] if block.get("role") == "cover" and image and Path(image["path"]).is_file() else block["local_file_ref"]
                st.image(path, width="stretch")
    else:
        st.markdown(article.body)


def new_article(store, config):
    from dzen_agent.ui_pages import action, service
    with st.expander("Добавить материал", expanded=not store.list_all()):
        manual, ai = st.tabs(["Свой текст", "Написать с AI"])
        with manual, st.form("manual_article"):
            title = st.text_input("Заголовок своего текста")
            body = st.text_area("Свой текст", height=180)
            if st.form_submit_button("Сохранить свой черновик"):
                def save():
                    if not title.strip() or not body.strip():
                        raise ValueError("Введите заголовок и текст.")
                    article = store.create(title, title, body)
                    store.execute("INSERT INTO article_details(article_id) VALUES(?)", (article.id,))
                    return article.id
                article_id = action("Сохраняю черновик…", save)
                if article_id:
                    st.session_state.selected_article = article_id
                    st.rerun()
        with ai:
            topic = st.text_input("Тема статьи")
            consent = st.checkbox("Разрешаю оплату исследования и написания", key="manual-ai-consent")
            needs_plan = native(Prompts(store).active("RESEARCH"), "research")
            if needs_plan:
                st.caption("В этой редакции AI пишет после утверждения темы. Подготовьте неделю на рабочем столе; свой текст можно добавить вручную.")
            if st.button("Исследовать тему и написать статью", disabled=needs_plan or not consent or not config.aitunnel_api_key or store.settings()["stop_requested"]):
                def generate():
                    if not topic.strip():
                        raise ValueError("Введите тему статьи.")
                    content = service(store, config)
                    key = "manual:" + uuid4().hex
                    research_id = content.research(topic, {}, key + ":research")
                    return content.write(topic, research_id, key + ":write")
                article_id = action("Исследую и пишу…", generate)
                if article_id:
                    st.session_state.selected_article = article_id
                    st.rerun()


def image_panel(store, config, article, image, visual):
    from dzen_agent.ui_pages import action, service
    if image and Path(image["path"]).is_file():
        st.image(image["path"], width="stretch")
    else:
        st.info("Обложка ещё не подготовлена.")
    if visual:
        for issue in visual.get("issues", []) + visual.get("assembly", {}).get("validation_issues", []):
            st.warning(issue)
        for asset in visual.get("assets", []):
            with st.expander(asset.get("source_name") or "Источник фотографии"):
                st.link_button("Открыть источник", asset["source_page_url"])
                st.caption("Правообладатель: " + (asset.get("rights_holder") or "не указан"))
                if asset.get("local_file_ref") and Path(asset["local_file_ref"]).is_file():
                    st.image(asset["local_file_ref"], width="stretch")
                for issue in asset.get("issues", []):
                    st.caption(issue)
    own = st.file_uploader("Заменить обложку своей фотографией", type=["png", "jpg", "jpeg", "webp"], key=f"upload-{article.id}")
    if own and st.button("Использовать мою картинку"):
        if action("Сохраняю обложку…", lambda: service(store, config).upload_image(article.id, own.getvalue())):
            st.rerun()
    with st.expander("Подбор фотографий с AI"):
        st.caption("Поиск и проверка фотографий оплачиваются. Существующее оформление остаётся в истории.")
        consent = st.checkbox("Разрешаю оплату нового подбора изображений", key=f"image-consent-{article.id}")
        label = "Подобрать фото и собрать оформление" if native(Prompts(store).active("IMAGE"), "image") else "Подготовить обложку"
        if st.button(label, disabled=not consent or not config.aitunnel_api_key or store.settings()["stop_requested"]):
            if action("Подбираю оформление…", lambda: service(store, config).image(article.id, "image:" + uuid4().hex, force=True)):
                st.rerun()


def articles_page(store, config):
    from dzen_agent.ui_pages import action, rub, service
    st.title("Материалы")
    st.caption("Читайте, редактируйте и проверяйте статьи перед передачей в Дзен")
    new_article(store, config)
    all_articles = store.list_all()
    if not all_articles:
        st.info("Готовые тексты появятся здесь после утверждения тем и исследования.")
        return
    runs = store.rows("SELECT id,week_start FROM pipeline_runs ORDER BY id DESC")
    options = [0] + [r["id"] for r in runs]
    selected_run = st.session_state.get("material_run", 0)
    selected_run = st.selectbox("Материалы недели", options, index=options.index(selected_run) if selected_run in options else 0,
        format_func=lambda i: "Все материалы" if i == 0 else next(week_label(r["week_start"]) for r in runs if r["id"] == i))
    ids_for_run = {r["article_id"] for r in store.rows("SELECT article_id FROM article_details WHERE run_id=?", (selected_run,))}
    articles = [a for a in all_articles if not selected_run or a.id in ids_for_run]
    if not articles:
        st.info("Для этой недели тексты ещё готовятся.")
        return
    left, center = st.columns([1, 3], gap="large")
    with left:
        ids = [a.id for a in articles]
        if st.session_state.get("selected_article") not in ids:
            st.session_state.selected_article = ids[0]
        article_id = st.radio("Статьи недели", ids, key="selected_article", format_func=lambda i:
            next(a.title for a in articles if a.id == i))
        st.caption(f"Всего материалов: {len(articles)}")
    article, detail = store.get(article_id), store.detail(article_id)
    image, visual = store.active_image(article_id), visual_report(store, article_id)
    with center:
        st.caption(human_status(article.status) + " · " + (datetime.fromisoformat(detail["scheduled_at"]).astimezone(MSK).strftime("%d.%m · %H:%M · Москва")
            if detail.get("scheduled_at") else "Дата публикации не назначена") + " · Расходы: " + rub(store.costs(article_id=article_id)["total"]))
        run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (detail.get("run_id"),))
        if run and not run["approved_at"]:
            qa = store.one("SELECT status FROM pipeline_stages WHERE run_id=? AND stage='QA'", (run["id"],))
            if qa and qa["status"] == "SUCCEEDED":
                st.caption("Итоговое утверждение проверит все статьи этой недели. Публикация не запускается.")
                if st.button("Утвердить материалы недели", type="primary"):
                    if action("Проверяю неделю…", lambda: approve_week(store, run["id"]) or True):
                        st.rerun()
        preview, edit, images, qa_tab, history = st.tabs(["Статья", "Редактировать", "Оформление", "Проверка", "История"])
        with preview:
            preview_article(article, image, visual)
            st.download_button("Скачать текст Markdown", f"# {article.title}\n\n{article.body}", file_name=f"article-{article_id}.md")
        with edit:
            with st.form(f"edit-{article_id}-{article.updated_at}"):
                title = st.text_input("Заголовок", value=article.title)
                body = st.text_area("Текст", value=article.body, height=480)
                if st.form_submit_button("Сохранить изменения"):
                    action("Сохраняю…", lambda: store.update_content(article_id, title, body))
                    st.rerun()
            with st.form(f"schedule-{article_id}"):
                scheduled = datetime.fromisoformat(detail["scheduled_at"]) if detail.get("scheduled_at") else datetime.now(MSK) + timedelta(days=7)
                day = st.date_input("Дата публикации", value=scheduled.date())
                at = st.time_input("Время публикации (Москва)", value=scheduled.time())
                if st.form_submit_button("Сохранить дату и время"):
                    action("Сохраняю расписание…", lambda: service(store, config).set_schedule(article_id, datetime.combine(day, at, tzinfo=MSK).isoformat()))
            with st.expander("Дополнительные AI-действия"):
                part = st.selectbox("Что написать заново", ["all", "title", "body"], format_func=lambda p: {"all": "Всю статью", "title": "Только заголовок", "body": "Только текст"}[p])
                allowed = st.checkbox("Разрешаю оплату новой редакции", key=f"regenerate-consent-{article_id}")
                if st.button("Написать выбранную часть заново", disabled=not allowed or not config.aitunnel_api_key or store.settings()["stop_requested"]):
                    def regenerate():
                        content = service(store, config)
                        research_id = detail.get("research_id")
                        if not research_id:
                            research_id = content.research(article.topic, {}, "manual-research:" + uuid4().hex)
                            store.execute("UPDATE article_details SET research_id=? WHERE article_id=?", (research_id, article_id))
                        return content.write(article.topic, research_id, "regenerate:" + uuid4().hex, article_id=article_id, part=part)
                    if action("Готовлю новую редакцию…", regenerate):
                        st.rerun()
        with images:
            image_panel(store, config, article, image, visual)
        with qa_tab:
            qa = json.loads(detail.get("qa_json") or "null")
            if qa:
                (st.success if qa["passed"] else st.warning)("Проверка пройдена" if qa["passed"] else "Есть замечания")
                for issue in qa["issues"]:
                    st.write("• " + issue)
                st.caption("Последняя проверка: " + qa["checked_at"])
            else:
                st.info("Статья ещё не проверена.")
            if st.button("Проверить статью (QA)"):
                if action("Проверяю материал…", lambda: qa_article(store, article_id)):
                    st.rerun()
            with st.expander("Передача в Дзен"):
                st.info("Заполнение редактора доступно. Загрузка всех иллюстраций и автоматическое планирование пока не подтверждены.")
                st.caption("Откроется браузер с заголовком и текстом. Проверку изображений и публикацию вы выполняете вручную.")
                if st.button("Открыть статью в редакторе Дзена"):
                    action("Открываю редактор…", lambda: launch_session(article_id), success="Редактор открыт. Публикация не выполнялась.")
                if st.button("Обновить статью в Дзене", disabled=True,
                    help="Автоматическое обновление ещё не проверено. Используйте ручной редактор."):
                    action("Проверяю возможность обновления…", lambda: DzenScheduling(store).update(article_id))
                publication = store.one("SELECT * FROM publications WHERE article_id=?", (article_id,))
                if publication and publication["dzen_url"]:
                    st.link_button("Открыть сохранённую публикацию", publication["dzen_url"])
        with history:
            for revision in store.rows("SELECT * FROM article_revisions WHERE article_id=? ORDER BY id DESC", (article_id,)):
                with st.expander(revision["created_at"] + " · " + revision["title"]):
                    st.markdown(revision["body"])
            with st.expander("Технические детали материала"):
                st.json(detail, expanded=False)
