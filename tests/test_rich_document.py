import hashlib
import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from dzen_agent.browser.rich_document import ImageBlock, load_document, paste_html, text_block
from dzen_agent.browser.editor import DzenEditor, publication_time
from dzen_agent.errors import UserActionRequired


def test_rich_paste_on_local_page():
    block = text_block("## Раздел\n\nАбзац **жирный** и *курсив*.\n\n- Первый\n- Второй")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        page.set_content("""<div contenteditable='true'></div><script>
        document.querySelector('div').addEventListener('paste', e => {
          e.preventDefault(); e.target.innerHTML = e.clipboardData.getData('text/html');
        });</script>""")
        paste_html(page.locator("div[contenteditable]"), block)
        assert page.locator("h2").inner_text() == "Раздел"
        assert page.locator("strong").inner_text() == "жирный"
        assert page.locator("em").inner_text() == "курсив"
        assert page.locator("li").all_inner_texts() == ["Первый", "Второй"]
        assert "**" not in page.locator("body").inner_text()
        browser.close()


def assembly(tmp_path, body="Текст"):
    image = tmp_path / "photo.png"
    image.write_bytes(b"fixture")
    document = {"headline": "Тест", "original_markdown": body,
                "original_content_hash": hashlib.sha256(body.encode()).hexdigest(),
                "blocks": [{"type": "image", "local_file_ref": str(image)},
                           {"type": "text", "markdown": body}]}
    path = tmp_path / "article.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path, document


def test_assembly_preserves_order_and_image_hash(tmp_path):
    path, _ = assembly(tmp_path)
    blocks = load_document(path, "Тест", "Текст")
    assert isinstance(blocks[0], ImageBlock)
    assert blocks[0].sha256 == hashlib.sha256(b"fixture").hexdigest()
    assert blocks[1].text == "Текст"


@pytest.mark.parametrize("mutation", ["stale", "duplicate", "missing_image", "unknown"])
def test_invalid_assembly_stops_before_browser(tmp_path, mutation):
    path, document = assembly(tmp_path)
    if mutation == "stale":
        document["original_content_hash"] = "old"
    elif mutation == "duplicate":
        document["blocks"].append({"type": "text", "markdown": "Текст"})
    elif mutation == "missing_image":
        (tmp_path / "photo.png").unlink()
    else:
        document["blocks"].append({"type": "unknown"})
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(UserActionRequired):
        load_document(path, "Тест", "Текст")


def test_unsafe_html_is_escaped_and_remote_images_refused():
    assert "<script>" not in text_block("<script>alert(1)</script>").html
    with pytest.raises(UserActionRequired):
        text_block("![photo](https://example.com/a.png)")


def test_local_editor_transfer_and_schedule_preparation_never_submits(tmp_path):
    from PIL import Image

    image = tmp_path / "photo.png"
    Image.new("RGB", (160, 90), "blue").save(image)
    blocks = [text_block("## Раздел\n\nНачало **жирный** и *курсив*."),
              ImageBlock(image, "test"), text_block("Конец.\n\n- Один\n- Два")]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        page.set_content(Path("tests/fixtures/dzen_studio.html").read_text(encoding="utf-8"))
        editor = DzenEditor(page)
        editor.replace("Технический тест", blocks)
        editor.wait_saved()
        result = editor.prepare_schedule("2099-01-31T16:00:00+03:00", 0)
        assert result == {"date": "31 января 2099", "time": "16:00", "timezone": "GMT+3"}
        assert page.evaluate("window.finalSaves") == 0
        # Lost formatting must fail even when all text and image counts still match.
        page.locator("strong").evaluate("e => e.replaceWith(...e.childNodes)")
        with pytest.raises(UserActionRequired, match="форматирование"):
            editor.verify("Технический тест", blocks)
        browser.close()


@pytest.mark.parametrize("value", ["2000-01-01T12:00:00+03:00", "2099-01-01T12:00:00"])
def test_immediate_or_ambiguous_schedule_is_rejected(value):
    with pytest.raises(UserActionRequired):
        publication_time(value)
