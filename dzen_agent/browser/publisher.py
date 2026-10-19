"""Observed Studio workflow with durable checkpoints and no AI dependencies."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.parse import parse_qs
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright

from dzen_agent.browser.diagnostics import capture, profile_lock_path, validate_dzen_url
from dzen_agent.browser.editor import DzenEditor, PUBLICATION_MODAL, publication_time
from dzen_agent.browser.rich_document import ImageBlock, load_document
from dzen_agent.browser.scheduling import checkpoint
from dzen_agent.config import get_settings
from dzen_agent.errors import UserActionRequired
from dzen_agent.locking import exclusive_lock
from dzen_agent.services import article_fingerprint

ADAPTER_VERSION = "studio-2026-10-18-v1"


def profile_identity(config):
    return hashlib.sha256(str(config.browser_profile_dir.resolve()).lower().encode()).hexdigest()


class DzenPublisher:
    def __init__(self, store, config=None):
        self.store, self.config = store, config or get_settings()

    @property
    def verified(self):
        proof = self.store.settings().get("dzen_adapter_verification", {})
        return bool(proof.get("adapter_version") == ADAPTER_VERSION
                    and proof.get("profile_identity") == profile_identity(self.config)
                    and proof.get("scheduled_verified") and proof.get("edit_verified")
                    and proof.get("studio_url") and proof.get("test_editor_url"))

    def check_stop(self):
        if self.store.settings()["stop_requested"]:
            raise UserActionRequired("Отправка остановлена. Прогресс сохранён.")

    @contextmanager
    def browser(self):
        self.check_stop()
        with exclusive_lock(profile_lock_path(self.config.browser_profile_dir)), sync_playwright() as p:
            try:
                context = p.chromium.launch_persistent_context(str(self.config.browser_profile_dir),
                    headless=False, executable_path=self.config.dzen_browser_executable,
                    viewport={"width": 1440, "height": 1000})
            except Exception as error:
                raise UserActionRequired("Не удалось открыть профиль Chromium. Закройте другое окно Дзена с этим профилем.") from error
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            try:
                yield page
            except Exception as error:
                capture(page, self.store, "DZEN_SCHEDULE", error)
                raise
            finally:
                context.close()

    def studio(self, page):
        proof = self.store.settings().get("dzen_adapter_verification", {})
        url = validate_dzen_url(proof["studio_url"])
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            expect(page.get_by_role("link", name="Публикации", exact=True)).to_be_visible()
        except AssertionError as error:
            raise UserActionRequired("Студия недоступна. Войдите в Дзен вручную в сохранённом профиле.") from error
        if urlsplit(page.url).path.rstrip("/") != urlsplit(url).path.rstrip("/"):
            raise UserActionRequired("Открыт другой канал или страница входа. Нужна ручная проверка профиля.")

    def find(self, page, title, state):
        self.studio(page)
        tab = "Ожидают публикации" if state == "pending" else "Черновики"
        page.get_by_text(re.compile("^" + tab + r"\d*$")).click()
        row = page.get_by_role("row", name="Публикация", exact=True).filter(
            has=page.get_by_role("heading", name=title, exact=True))
        try:
            expect(row).to_have_count(1, timeout=5000)
            return row
        except AssertionError:
            if row.count() > 1:
                raise UserActionRequired("В Студии несколько материалов с этим заголовком. Выберите нужный вручную.") from None
        page.get_by_placeholder("Поиск публикации").fill(title)
        with page.expect_response(lambda response: urlsplit(response.url).path == "/editor-api/v3/publications"
                                  and response.request.method == "GET", timeout=30000) as completed:
            page.get_by_role("button", name="Найти", exact=True).click()
        if not completed.value.ok:
            raise UserActionRequired("Поиск в Студии завершился ошибкой. Создание материала заблокировано.")
        completed.value.finished()
        row = page.get_by_role("row", name="Публикация", exact=True).filter(
            has=page.get_by_role("heading", name=title, exact=True))
        try:
            expect(row).to_have_count(1, timeout=10000)
        except AssertionError:
            if row.count() > 1:
                raise UserActionRequired("В Студии несколько материалов с этим заголовком. Выберите нужный вручную.") from None
            return None
        return row

    def row_editor(self, page, row):
        # Current Studio draft rows expose the editor URL directly; no search/menu mutation is needed.
        direct = row.locator('a[href*="/profile/editor/id/"][href$="/edit"]')
        if direct.count() == 1:
            return validate_dzen_url(urljoin(page.url, direct.get_attribute("href")))
        row.get_by_role("button", name="Меню публикации", exact=True).click(timeout=60000)
        link = page.locator('a.Menu-ItemInteractive').filter(has_text=re.compile("^Отредактировать$"))
        return validate_dzen_url(urljoin(page.url, link.get_attribute("href")))

    def document(self, article, *, require_cover=True):
        row = self.store.one("SELECT payload_json FROM visual_results WHERE article_id=? ORDER BY id DESC LIMIT 1", (article.id,))
        if not row:
            raise UserActionRequired("Нет готовой визуальной сборки Image. Отправка остановлена.")
        visual = json.loads(row["payload_json"])
        reference = visual["assembly"]["assembled_document_ref"]
        blocks = load_document(reference, article.title, article.body)
        document = json.loads(Path(reference).read_text(encoding="utf-8"))
        cover = document.get("cover_file_ref")
        images = [b for b in blocks if isinstance(b, ImageBlock)]
        indexes = [i for i, b in enumerate(images) if cover and b.path.resolve() == Path(cover).resolve()]
        if len(indexes) != 1 and (require_cover or indexes):
            raise UserActionRequired("Обложка должна присутствовать в визуальной сборке ровно один раз.")
        return blocks, indexes[0] if indexes else None

    def collect_statistics(self):
        """Use only the export control actually observed in this saved channel/profile."""
        from dzen_agent.browser.statistics_download import download_statistics
        proof = self.store.settings().get("statistics_export_verification", {})
        if proof.get("profile_identity") != profile_identity(self.config) or proof.get("adapter_version") != ADAPTER_VERSION:
            raise UserActionRequired("Экспорт статистики не проверен для сохранённого профиля. Используется локальный импорт.")
        folder = self.store.database_path.parent / "statistics-exports"
        folder.mkdir(exist_ok=True)
        with self.browser() as page:
            destination = folder / (uuid4().hex + ".xlsx")
            download_statistics(page, destination, statistics_url=proof["statistics_url"], verified_selector=proof["selector"])
            parameters = parse_qs(urlsplit(page.locator(proof["selector"]).get_attribute("href")).query)
            return {"path": str(destination), "from": parameters.get("from", [None])[0],
                    "to": parameters.get("to", [None])[0], "filter_type": parameters.get("filterType", [None])[0]}

    def create_draft(self, article, detail, image, publication):
        """Save and reopen an editor draft. Never enter publication settings."""
        blocks, cover_index = self.document(article, require_cover=False)
        with self.browser() as page:
            if publication.get("dzen_url"):
                remote_title = json.loads(publication.get("evidence_json") or "{}").get("title", article.title)
                row = self.find(page, remote_title, "draft")
                if not row or self.row_editor(page, row) != publication["dzen_url"]:
                    raise UserActionRequired("Сохранённый черновик не найден однозначно. Дубликат не создавался.")
                page.goto(validate_dzen_url(publication["dzen_url"]), wait_until="domcontentloaded")
            else:
                if publication["status"] == "CREATING_DRAFT":
                    raise UserActionRequired("Адрес созданного черновика неизвестен. Нужна сверка Студии; дубликат заблокирован.")
                for state in ("pending", "draft"):
                    if self.find(page, article.title, state):
                        raise UserActionRequired("В Студии уже есть материал с этим названием. Автоматическое присвоение чужого ID запрещено.")
                checkpoint(self.store, article.id, "CREATING_DRAFT")
                page.locator('button[class*="author-studio-header__addButton"]').click()
                page.get_by_text("Написать статью", exact=True).click()
                expect(page.locator('[contenteditable=true]')).to_have_count(2, timeout=60000)
                url = validate_dzen_url(page.url)
                checkpoint(self.store, article.id, "RENDERING", url=url, dzen_id=urlsplit(url).path.split("/")[-2])
            editor = DzenEditor(page, self.check_stop)
            if publication["status"] != "DRAFT_VERIFIED" or publication.get("content_hash") != article_fingerprint(self.store, article.id):
                editor.replace(article.title, blocks)
                editor.wait_saved()
            snapshot = editor.verify(article.title, blocks)
            url = validate_dzen_url(page.url)
            row = self.find(page, article.title, "draft")
            if not row or self.row_editor(page, row) != url:
                raise UserActionRequired("Черновик не подтверждён в списке Студии.")
            page.goto(url, wait_until="domcontentloaded")
            snapshot = DzenEditor(page, self.check_stop).verify(article.title, blocks)
            return {**snapshot, "dzen_url": url, "dzen_id": urlsplit(url).path.split("/")[-2],
                    "title": article.title,
                    "cover_in_document": cover_index is not None, "publicly_published": False,
                    "image_hashes": [b.sha256 for b in blocks if isinstance(b, ImageBlock)]}

    def create_scheduled(self, article, detail, image):
        blocks, cover_index = self.document(article)
        with self.browser() as page:
            # Reconcile scheduled and draft lists BEFORE the create action.
            for state in ("pending", "draft"):
                row = self.find(page, article.title, state)
                if row:
                    raise UserActionRequired("В Студии уже есть такой заголовок. Свяжите существующую статью перед отправкой.")
            self.check_stop()
            checkpoint(self.store, article.id, "CREATING_DRAFT")
            page.locator('button[class*="author-studio-header__addButton"]').click()
            page.get_by_text("Написать статью", exact=True).click()
            expect(page.locator('[contenteditable=true]')).to_have_count(2, timeout=60000)
            url = validate_dzen_url(page.url)
            checkpoint(self.store, article.id, "RENDERING", url=url, dzen_id=urlsplit(url).path.split("/")[-2])
            return self._render_and_save(page, article, detail, blocks, cover_index)

    def _render_and_save(self, page, article, detail, blocks, cover_index):
        editor = DzenEditor(page, self.check_stop)
        editor.replace(article.title, blocks)
        editor.wait_saved()
        snapshot = editor.verify(article.title, blocks)
        snapshot["image_hashes"] = [block.sha256 for block in blocks if isinstance(block, ImageBlock)]
        checkpoint(self.store, article.id, "DRAFT_READY", evidence=snapshot)
        editor.prepare_schedule(detail["scheduled_at"], cover_index)
        self.check_stop()
        checkpoint(self.store, article.id, "SAVING_SCHEDULE")
        # This is the only final remote mutation; prior state is already durable.
        page.locator(PUBLICATION_MODAL).get_by_role("button", name="Опубликовать позже", exact=True).click()
        expect(page.locator(PUBLICATION_MODAL)).not_to_be_visible(timeout=60000)
        checkpoint(self.store, article.id, "SCHEDULED_PENDING_VERIFY")
        publication = self.store.one("SELECT * FROM publications WHERE article_id=?", (article.id,))
        return self._verify(page, publication, article, detail, blocks)

    def resume_scheduled(self, publication, article, detail, image):
        if publication["status"] == "PREPARING_IN_DZEN" and not publication["dzen_url"]:
            # Browser/auth/search failed before the durable create checkpoint: no create was sent.
            return self.create_scheduled(article, detail, image)
        blocks, cover_index = self.document(article)
        with self.browser() as page:
            if not publication["dzen_url"]:
                for state in ("pending", "draft"):
                    row = self.find(page, article.title, state)
                    if row:
                        url = self.row_editor(page, row)
                        # Never automatically adopt an unknown draft after a lost create response.
                        raise UserActionRequired(f"Найден возможный материал: {url}. Нужна ручная привязка; новый не создан.")
                raise UserActionRequired("Адрес черновика не сохранён. Проверьте Студию вручную; повторное создание заблокировано.")
            row = self.find(page, article.title, "pending")
            if row:
                return self._verify(page, publication, article, detail, blocks)
            row = self.find(page, article.title, "draft")
            if not row or self.row_editor(page, row) != publication["dzen_url"]:
                raise UserActionRequired("Не найден однозначный черновик с сохранённым ID. Повторная отправка остановлена.")
            page.goto(validate_dzen_url(publication["dzen_url"]), wait_until="domcontentloaded")
            return self._render_and_save(page, article, detail, blocks, cover_index)

    def verify(self, publication):
        article = self.store.get(publication["article_id"])
        detail = self.store.detail(article.id)
        blocks, _ = self.document(article)
        with self.browser() as page:
            return self._verify(page, publication, article, detail, blocks)

    def _verify(self, page, publication, article, detail, blocks):
        self.check_stop()
        row = self.find(page, article.title, "pending")
        if not row:
            raise UserActionRequired("Материал не найден в отложенных. Автоматическое создание дубликата заблокировано.")
        expect(row.get_by_text("Ожидает публикации", exact=True)).to_be_visible()
        observed_editor = self.row_editor(page, row)
        if observed_editor != publication["dzen_url"]:
            raise UserActionRequired("ID найденной статьи не совпадает с сохранённым ID.")
        moment = publication_time(detail["scheduled_at"])
        cells = row.locator("td").all_inner_texts()
        short_months = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
        expected_date = f"{moment.day} {short_months[moment.month-1]}"
        if not any(expected_date in cell and moment.strftime("%H:%M") in cell for cell in cells):
            raise UserActionRequired("Дата или время в списке отложенных не совпадают с планом.")
        cover = row.get_by_test_id("publication-cover").locator("div").get_attribute("style")
        page.goto(observed_editor, wait_until="domcontentloaded")
        editor = DzenEditor(page, self.check_stop)
        snapshot = editor.verify(article.title, blocks)
        saved = json.loads(publication["evidence_json"] or "{}")
        expected_images = [block.sha256 for block in blocks if isinstance(block, ImageBlock)]
        if saved.get("image_hashes") != expected_images:
            raise UserActionRequired("Локальные иллюстрации изменились после переноса. Нужна повторная проверка.")
        def identities(urls):
            return [re.search(r"/pub_[^/]+/", url).group() if re.search(r"/pub_[^/]+/", url) else url for url in urls]
        if identities(saved.get("image_urls", [])) != identities(snapshot["image_urls"]):
            raise UserActionRequired("Изображения в редакторе изменились или переставлены.")
        snapshot["image_hashes"] = expected_images
        # Reopen publication settings to check the full date/year/timezone and selected cover.
        page.get_by_role("button", name="Опубликовать", exact=True).click()
        modal = page.locator(PUBLICATION_MODAL)
        expect(modal.locator('input[name="delayedTime"]')).to_have_value(moment.strftime("%H:%M"))
        from dzen_agent.browser.editor import MONTHS_DATE
        expect(modal.locator('input[type="text"][readonly]')).to_have_value(
            f"{moment.day} {MONTHS_DATE[moment.month-1]} {moment.year}")
        expect(modal.locator('input[class*="checkbox-v2__input"]')).to_be_checked()
        expect(modal.get_by_text("GMT+3", exact=True)).to_be_visible()
        selected = modal.locator('[class*="cover-picker__coverContainer"]').filter(
            has=page.locator('[class*="cover-picker__coverBorder"]'))
        selected_style = selected.locator('[class*="cover-picker__cover-"]').get_attribute("style")
        image_id = re.search(r"/pub_[^/]+/", selected_style or "")
        if not image_id or image_id.group() not in (cover or ""):
            raise UserActionRequired("Обложка в Студии не совпадает с выбранной обложкой.")
        _, cover_index = self.document(article)
        if image_id.group() not in snapshot["image_urls"][cover_index]:
            raise UserActionRequired("Выбранная обложка не соответствует сборке.")
        return {**snapshot, "found_in_scheduled": True, "title": article.title,
                "scheduled_at": detail["scheduled_at"], "url": observed_editor,
                "id": publication["dzen_id"], "content_hash": article_fingerprint(self.store, article.id),
                "cover_verified": True, "timezone": "GMT+3"}

    def update_scheduled(self, publication, article, image):
        blocks, cover_index = self.document(article)
        detail = self.store.detail(article.id)
        with self.browser() as page:
            remote_title = json.loads(publication["evidence_json"] or "{}").get("title", article.title)
            row = self.find(page, remote_title, "pending")
            if not row or self.row_editor(page, row) != publication["dzen_url"]:
                raise UserActionRequired("Исходный материал не найден по сохранённому ID. Новый не создаётся.")
            page.goto(validate_dzen_url(publication["dzen_url"]), wait_until="domcontentloaded")
            return self._render_and_save(page, article, detail, blocks, cover_index)
