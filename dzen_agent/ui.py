from __future__ import annotations

import subprocess
import sys

import streamlit as st

from dzen_agent.ai import ArticleGenerator
from dzen_agent.config import PROJECT_ROOT, get_settings
from dzen_agent.database import ArticleRepository


settings = get_settings()
repository = ArticleRepository(settings.database_path)
repository.initialize()

st.set_page_config(page_title="Dzen Agent", page_icon="✍️", layout="wide")
st.title("Dzen Agent")
st.caption("Создание и подготовка статей. Финальная публикация всегда остаётся за вами.")

if "draft" not in st.session_state:
    st.session_state.draft = None

with st.container(border=True):
    st.subheader("Новая статья")
    topic = st.text_input("Тема статьи", placeholder="Например: как выбрать кофемолку для дома")
    if st.button("Создать статью", type="primary"):
        if not settings.openai_api_key:
            st.error("Не найден OPENAI_API_KEY. Скопируйте .env.example в .env и добавьте ключ.")
        elif not topic.strip():
            st.warning("Введите тему статьи.")
        else:
            try:
                with st.spinner("Готовлю черновик…"):
                    generated = ArticleGenerator(
                        settings.openai_api_key, settings.openai_model
                    ).generate(topic)
                st.session_state.draft = {
                    "topic": topic.strip(),
                    "title": generated.title,
                    "body": generated.body,
                }
            except Exception as error:
                st.error(f"Не удалось создать статью: {error}")

draft = st.session_state.draft
if draft:
    with st.form("new_article"):
        title = st.text_input("Заголовок", value=draft["title"])
        body = st.text_area("Текст статьи", value=draft["body"], height=420)
        if st.form_submit_button("Сохранить статью"):
            if title.strip() and body.strip():
                article = repository.create(draft["topic"], title, body)
                st.session_state.draft = None
                st.success(f"Статья №{article.id} сохранена.")
                st.rerun()
            else:
                st.warning("Заголовок и текст не должны быть пустыми.")

st.divider()
st.subheader("Сохранённые статьи")
articles = repository.list_all()
if not articles:
    st.info("Здесь появятся сохранённые статьи.")

for article in articles:
    with st.expander(f"#{article.id} · {article.title} · {article.status.value}"):
        with st.form(f"article-{article.id}"):
            edited_title = st.text_input("Заголовок", value=article.title, key=f"title-{article.id}")
            edited_body = st.text_area(
                "Текст", value=article.body, height=320, key=f"body-{article.id}"
            )
            save, prepare = st.columns(2)
            save_clicked = save.form_submit_button("Сохранить изменения")
            prepare_clicked = prepare.form_submit_button("Подготовить в Дзене")

        if save_clicked or prepare_clicked:
            repository.update_content(article.id, edited_title, edited_body)
        if save_clicked:
            st.success("Изменения сохранены.")
            st.rerun()
        if prepare_clicked:
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "dzen_agent.browser.worker", str(article.id)],
                    cwd=str(PROJECT_ROOT),
                )
                st.info(
                    "Открываю отдельное окно браузера. При первом запуске войдите в Дзен вручную. "
                    "После заполнения проверьте черновик — приложение ничего не публикует."
                )
            except OSError as error:
                st.error(f"Не удалось запустить браузер: {error}")

