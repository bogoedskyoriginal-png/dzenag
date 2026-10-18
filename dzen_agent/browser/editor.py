"""Draft.js controls observed in the authorized Dzen Studio on 2026-10-18.

This module prepares drafts. The final scheduling action is deliberately separate.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from playwright.sync_api import expect

from dzen_agent.browser.rich_document import ImageBlock, paste_html
from dzen_agent.errors import UserActionRequired

TITLE = '[contenteditable="true"]:has(h1[data-block="true"])'
BODY = '[contenteditable="true"]:has([data-editor="ZenDraftEditor"])'
INSERT_IMAGE = 'button[data-tip="Вставить изображение"]'
IMAGE_INPUT = 'input[type="file"][accept*="image/png"]'
PUBLICATION_MODAL = '[class*="publication-modal__publicationModal-"]'
MONTHS = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")
MONTHS_DATE = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
MSK = timezone(timedelta(hours=3))


def publication_time(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise UserActionRequired("Назначьте корректную дату и время публикации в плане.") from error
    if moment.tzinfo is None:
        raise UserActionRequired("Дата публикации должна содержать часовой пояс.")
    moment = moment.astimezone(MSK)
    if moment <= datetime.now(MSK) + timedelta(minutes=10):
        raise UserActionRequired("Для отложенной публикации нужно время как минимум через 10 минут.")
    return moment


def normalized(text):
    return re.sub(r"\s+", " ", text).strip()


class DzenEditor:
    def __init__(self, page, check_stop=lambda: None):
        self.page, self.check_stop = page, check_stop

    @property
    def body(self):
        return self.page.locator(BODY)

    def dismiss_help(self):
        help_popup = self.page.locator('[class*="help-popup__overlay"]')
        if help_popup.is_visible():
            help_popup.get_by_role("button", name="Закрыть", exact=True).click()

    def replace(self, title, blocks):
        self.check_stop()
        if not title.strip() or len(title) > 140:
            raise UserActionRequired("Дзен принимает заголовок длиной от 1 до 140 символов. Исправьте его перед отправкой.")
        self.dismiss_help()
        expect(self.page.locator(TITLE)).to_have_count(1)
        expect(self.body).to_have_count(1)
        title_field = self.page.locator(TITLE)
        title_field.click()
        title_field.press("Control+a")
        self.page.keyboard.insert_text(title)
        self.body.click()
        self.body.press("Control+a")
        self.body.press("Backspace")
        for block in blocks:
            self.check_stop()
            self.body.press("Control+End")
            if isinstance(block, ImageBlock):
                self.insert_image(block.path)
            else:
                paste_html(self.body, block)
        self.verify(title, blocks)

    def insert_image(self, path):
        count = self.body.locator("img").count()
        if not self.body.inner_text().strip() and not count:
            self.page.keyboard.insert_text(" ")
        self.body.press("Enter")
        self.page.locator(INSERT_IMAGE).click()
        self.page.locator(IMAGE_INPUT).set_input_files(str(path.resolve()))
        expect(self.body.locator("img")).to_have_count(count + 1, timeout=60000)
        self.page.wait_for_function("""selector => [...document.querySelectorAll(selector + ' img')]
            .every(img => img.complete && img.naturalWidth > 0)""", arg=BODY, timeout=60000)
        self.body.press("Control+End")
        self.body.press("ArrowDown")
        self.body.press("Enter")

    def verify(self, title, blocks):
        expect(self.page.locator(TITLE)).to_have_text(title)
        # Captions and image UI controls are not part of the authored text.
        actual = self.body.locator('[data-block="true"]:not([contenteditable="false"])').evaluate_all(
            "els => els.filter(e => !e.querySelector('img')).map(e => e.innerText).join('\\n')")
        expected = "\n".join(b.text for b in blocks if not isinstance(b, ImageBlock))
        if normalized(actual) != normalized(expected):
            raise UserActionRequired("Текст в редакторе отличается от сборки: перенос остановлен до публикации.")
        expect(self.body.locator("img")).to_have_count(sum(isinstance(b, ImageBlock) for b in blocks))
        signature = """root => [...root.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li,[data-block=true]')]
            .filter(e => !e.parentElement.closest('[data-block=true]'))
            .map(e => {
                if (e.querySelector('img')) return {image:true};
                const clean = s => s.replace(/\\s+/g, ' ').trim();
                return {tag: e.tagName === 'DIV' ? 'P' : e.tagName,
                    list: e.tagName === 'LI' ? (e.closest('ol') ? 'ordered' : 'unordered') : null,
                    text: clean(e.innerText || e.textContent || ''),
                    bold: [...e.querySelectorAll('strong,b,[style*="font-weight: bold"]')].map(e=>clean(e.textContent)),
                    italic: [...e.querySelectorAll('em,i,[style*="font-style: italic"]')].map(e=>clean(e.textContent))};
            }).filter(e => e.image || e.text)"""
        wanted = []
        for block in blocks:
            if isinstance(block, ImageBlock):
                wanted.append({"image": True})
            else:
                wanted.extend(self.page.evaluate("""({html, signature}) => {
                    const document = new DOMParser().parseFromString(html, 'text/html');
                    return (0, eval)('(' + signature + ')')(document.body);
                }""", {"html": block.html, "signature": signature}))
        observed = self.body.evaluate(signature)
        if observed != wanted:
            raise UserActionRequired("Изменились порядок блоков или форматирование. Публикация остановлена.")
        return {"blocks": observed, "image_urls": self.body.locator("img").evaluate_all("els => els.map(e => e.src)")}

    def wait_saved(self):
        expect(self.page.get_by_text(re.compile(r"^(Сохранено |Черновик создан )"))).to_be_visible(timeout=60000)

    def prepare_schedule(self, scheduled_at, cover_index):
        """Open settings and verify them; never click the final submit button here."""
        moment = publication_time(scheduled_at)
        self.check_stop()
        self.wait_saved()
        self.page.get_by_role("button", name="Опубликовать", exact=True).click()
        modal = self.page.locator(PUBLICATION_MODAL)
        expect(modal).to_be_visible()
        modal.locator('[class*="cover-picker__coverContainer"]').nth(cover_index).click()
        toggle = modal.locator('input[class*="checkbox-v2__input"]')
        if not toggle.is_checked():
            modal.locator('[class*="checkbox-input__title"]').filter(has_text=re.compile("^Опубликовать позже$")).click()
        date = modal.locator('input[type="text"][readonly]')
        date.click()
        desired = f"{MONTHS[moment.month - 1]} {moment.year}"
        calendars = modal.locator('[class*="datepicker-calendar__datepickerCalendar-"]')
        for _ in range(24):
            self.check_stop()
            titles = modal.locator('[class*="datepicker-calendar-header__title"]').all_inner_texts()
            if desired in titles:
                calendar = calendars.nth(titles.index(desired))
                day = calendar.get_by_test_id("datepicker-calendar-day").filter(has_text=re.compile(f"^{moment.day}$"))
                if "__isAvailable-" not in (day.get_attribute("class") or ""):
                    raise UserActionRequired("Выбранный день недоступен в календаре Дзена.")
                day.click()
                break
            modal.get_by_role("button", name="Следующий месяц", exact=True).click()
        else:
            raise UserActionRequired("Дата не найдена в календаре Дзена.")
        time = modal.locator('input[name="delayedTime"]')
        time.fill(moment.strftime("%H:%M"))
        time.press("Tab")
        expect(date).to_have_value(f"{moment.day} {MONTHS_DATE[moment.month - 1]} {moment.year}")
        expect(time).to_have_value(moment.strftime("%H:%M"))
        expect(modal.get_by_text("GMT+3", exact=True)).to_be_visible()
        expect(modal.get_by_role("button", name="Опубликовать позже", exact=True)).to_be_enabled()
        return {"date": date.input_value(), "time": time.input_value(), "timezone": "GMT+3"}
