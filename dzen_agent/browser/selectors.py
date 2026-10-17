"""Dzen selectors are isolated here because the external UI can change."""

TITLE_SELECTORS = (
    "textarea[placeholder*='Заголовок']",
    "input[placeholder*='Заголовок']",
    "[contenteditable='true'][data-placeholder*='Заголовок']",
)

BODY_SELECTORS = (
    "[contenteditable='true'][data-placeholder*='текст']",
    "[contenteditable='true'][data-placeholder*='Текст']",
    "div.ProseMirror[contenteditable='true']",
)

# Publication controls are intentionally absent. V0 must never activate them.

