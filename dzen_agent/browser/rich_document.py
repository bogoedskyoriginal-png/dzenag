"""Translate the saved visual assembly into editor operations without rewriting text."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt

from dzen_agent.errors import UserActionRequired

MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": False})


@dataclass(frozen=True)
class TextBlock:
    html: str
    text: str


@dataclass(frozen=True)
class ImageBlock:
    path: Path
    sha256: str


def text_block(markdown: str) -> TextBlock:
    tokens = MARKDOWN.parse(markdown)
    if any(t.type in ("fence", "code_block", "hr") for t in tokens):
        raise UserActionRequired("Редактор ещё не проверен для кода и разделителей. Проверьте статью вручную.")
    for token in tokens:
        if any(child.type == "image" for child in token.children or []):
            raise UserActionRequired("Изображения должны поступать из визуальной сборки, а не из Markdown-ссылок.")
    lines = []
    for token in tokens:
        if token.type == "inline":
            lines.append("".join(
                "\n" if child.type in ("softbreak", "hardbreak") else child.content
                for child in token.children or []
                if child.type in ("text", "code_inline", "softbreak", "hardbreak")
            ))
    return TextBlock(MARKDOWN.renderer.render(tokens, MARKDOWN.options, {}), "\n".join(lines))


def load_document(reference: str | Path, title: str, body: str) -> list[TextBlock | ImageBlock]:
    document = json.loads(Path(reference).read_text(encoding="utf-8"))
    if (document.get("headline") != title or document.get("original_markdown") != body
            or document.get("original_content_hash") != hashlib.sha256(body.encode()).hexdigest()):
        raise UserActionRequired("Визуальная сборка относится к другой редакции статьи. Повторите сборку и QA.")
    blocks = document.get("blocks", [])
    if "".join(b.get("markdown", "") for b in blocks if b.get("type") == "text") != body:
        raise UserActionRequired("Сборка потеряла или продублировала фрагменты текста.")
    result = []
    for block in blocks:
        if block.get("type") == "text":
            if block["markdown"].strip():
                result.append(text_block(block["markdown"]))
        elif block.get("type") == "image":
            path = Path(block["local_file_ref"])
            if not path.is_file():
                raise UserActionRequired(f"Не найден файл иллюстрации: {path.name}")
            result.append(ImageBlock(path, hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            raise UserActionRequired("Неизвестный блок визуальной сборки. Перенос остановлен.")
    return result


def paste_html(field, block: TextBlock):
    """Use the editor's paste handler, so React/Draft owns the resulting document."""
    field.focus()
    field.evaluate("""(element, block) => {
        const data = new DataTransfer();
        data.setData('text/html', block.html);
        data.setData('text/plain', block.text);
        element.dispatchEvent(new ClipboardEvent('paste', {
            bubbles: true, cancelable: true, clipboardData: data
        }));
    }""", {"html": block.html, "text": block.text})
