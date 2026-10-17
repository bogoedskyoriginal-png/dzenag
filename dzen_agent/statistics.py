from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

from dzen_agent.store import Store, dumps, now_iso

ALIASES = {
    "title": ("название", "заголовок", "название публикации", "название статьи", "title", "name"),
    "url": ("ссылка", "url", "ссылка на публикацию", "ссылка на статью"),
    "date": ("дата", "дата публикации", "published_at", "date"),
    "views": ("просмотры", "просмотры публикации", "views"),
    "impressions": ("показы", "impressions"),
    "reads": ("дочитывания", "дочитывания и просмотры", "reads"),
    "likes": ("лайки", "likes", "нравится"),
    "comments": ("комментарии", "comments"),
    "subscribers": ("подписки", "подписчики", "subscribers"),
    "ctr": ("ctr", "ctr, %", "ctr (%)", "кликабельность"),
}
METRICS = set(ALIASES) - {"title", "url", "date"}


@dataclass
class CSVPreview:
    columns: list[str]
    rows: list[dict]
    mapping: dict[str, str]
    encoding: str
    delimiter: str
    sha256: str


def detect_mapping(columns: list[str]) -> dict[str, str]:
    return {column: key for column in columns for key, aliases in ALIASES.items()
            if column.strip().lower().replace("ё", "е") in aliases}


def preview_csv(raw: bytes) -> CSVPreview:
    if not raw or len(raw) > 20 * 1024 * 1024:
        raise ValueError("Загрузите непустой CSV размером до 20 МБ.")
    encodings = ["utf-16"] if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ["utf-8-sig", "cp1251"]
    text, encoding = "", ""
    for candidate in encodings:
        try:
            text = raw.decode(candidate)
            encoding = candidate
            break
        except UnicodeError:
            continue
    if not text or "\x00" in text:
        raise ValueError("Не удалось прочитать CSV. Сохраните его в UTF-8 или Windows-1251.")
    lines = text.splitlines()
    separator = None
    if lines and lines[0].lower().startswith("sep="):
        separator = lines.pop(0)[4:5]
    # Allow export descriptions before the real header; preserve quoted multiline fields.
    best = None
    for index in range(min(10, len(lines))):
        for delim in ([separator] if separator else [";", ",", "\t", "|"]):
            header = next(csv.reader([lines[index]], delimiter=delim), [])
            score = len(detect_mapping(header)) * 100 + min(len(header), 50)
            if len(header) > 1 and (best is None or score > best[0]):
                best = score, index, delim
    if best is None:
        raise ValueError("Не найден заголовок таблицы. Нужны как минимум две колонки.")
    _, index, delimiter = best
    try:
        reader = csv.DictReader(io.StringIO("\n".join(lines[index:])), delimiter=delimiter, strict=True)
        original = reader.fieldnames or []
        columns = [c.strip() for c in original]
        if len(set(columns)) != len(columns) or any(not c for c in columns):
            raise ValueError("В CSV есть пустые или одинаковые названия колонок.")
        rows = []
        for line_number, row in enumerate(reader, index + 2):
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f"Строка {line_number}: число значений не совпадает с заголовком.")
            if any(v.strip() for v in row.values()):
                rows.append({k.strip(): v.strip() for k, v in row.items()})
            if len(rows) > 50000:
                raise ValueError("Слишком много строк: максимум 50 000.")
    except csv.Error as error:
        raise ValueError("CSV повреждён: проверьте кавычки и разделители.") from error
    if not rows:
        raise ValueError("В CSV нет строк со статистикой.")
    return CSVPreview(columns, rows, detect_mapping(columns), encoding, delimiter,
                      hashlib.sha256(raw).hexdigest())


def normalize_rows(preview: CSVPreview, mapping: dict[str, str] | None = None) -> list[dict]:
    mapping = preview.mapping if mapping is None else mapping
    if not mapping or not set(mapping.values()).intersection(METRICS):
        raise ValueError("Укажите хотя бы одну колонку показателей: просмотры, показы или другую.")
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("Одному показателю назначено несколько колонок.")
    if not set(mapping).issubset(preview.columns) or not set(mapping.values()).issubset(ALIASES):
        raise ValueError("Некорректное сопоставление колонок.")
    result = []
    for index, row in enumerate(preview.rows, 2):
        normalized = {mapping.get(k, k): v for k, v in row.items()}
        for column in set(mapping.values()) & METRICS:
            value = re.sub(r"[\s\u00a0%]", "", str(normalized[column])).replace(",", ".")
            if value in ("", "-", "—"):
                normalized[column] = None
                continue
            try:
                number = float(value)
                if number < 0 or not __import__('math').isfinite(number):
                    raise ValueError()
                normalized[column] = number
            except ValueError as error:
                raise ValueError(f"Строка {index}, {column}: ожидается число.") from error
        result.append(normalized)
    return result


def import_csv(store: Store, filename: str, preview: CSVPreview, mapping=None) -> int:
    existing = store.one("SELECT id FROM statistics_imports WHERE sha256=?", (preview.sha256,))
    if existing:
        return existing["id"]
    rows = normalize_rows(preview, mapping)
    return store.execute(
        "INSERT INTO statistics_imports(filename,sha256,encoding,delimiter,columns_json,rows_json,"
        "created_at) VALUES(?,?,?,?,?,?,?)",
        (Path(filename).name, preview.sha256, preview.encoding, preview.delimiter,
         dumps(mapping or preview.mapping), dumps(rows), now_iso()),
    )


def statistics_context(row: dict | None) -> dict:
    if not row:
        return {"available": False}
    rows = json.loads(row["rows_json"])
    totals = {key: sum(r.get(key) or 0 for r in rows if isinstance(r.get(key), (int, float)))
              for key in METRICS - {"ctr"}}
    # Bounded context with transparent sampling; full import is retained in SQLite.
    ranked = sorted(rows, key=lambda r: r.get("views") or r.get("reads") or 0, reverse=True)
    sample = ranked if len(ranked) <= 80 else ranked[:60] + ranked[-20:]
    return {"import_id": row["id"], "imported_at": row["created_at"], "row_count": len(rows),
            "totals": totals, "rows": sample, "sampled": len(sample) < len(rows)}
