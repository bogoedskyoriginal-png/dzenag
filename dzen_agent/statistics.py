from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from dzen_agent.store import Store, dumps, now_iso

ALIASES = {
    "title": ("название", "заголовок", "название публикации", "название статьи", "публикация", "title", "name"),
    "url": ("ссылка", "url", "ссылка на публикацию", "ссылка на статью"),
    "date": ("дата", "дата публикации", "published_at", "date"),
    "views": ("просмотры", "просмотры публикации", "views"),
    "impressions": ("показы", "impressions"),
    "reads": ("дочитывания", "дочитывания и просмотры", "reads"),
    "likes": ("лайки", "likes", "нравится"),
    "comments": ("комментарии", "comments"),
    "subscribers": ("подписки", "подписчики", "subscribers"),
    "ctr": ("ctr", "ctr, %", "ctr (%)", "кликабельность"),
    "opens": ("opens", "открытия"),
    "completions": ("completions",),
    "subscriptions": ("subscriptions",),
    "unsubscribes": ("unsubscribes", "отписки"),
    "read_time_seconds": ("read_time_seconds", "время чтения, сек"),
    "article_id": ("article_id", "id публикации"),
    "status": ("status", "статус"),
}
METRICS = set(ALIASES) - {"title", "url", "date", "article_id", "status"}


@dataclass
class CSVPreview:
    columns: list[str]
    rows: list[dict]
    mapping: dict[str, str]
    encoding: str
    delimiter: str
    sha256: str
    source_format: str = "csv"
    sheet_name: str | None = None
    header_row: int = 1
    row_numbers: list[int] = field(default_factory=list)
    source_sha256: str | None = None


def detect_mapping(columns: list[str]) -> dict[str, str]:
    return {column: key for column in columns for key, aliases in ALIASES.items()
            if " ".join(column.lower().replace("ё", "е").split()) in aliases}


def preview_statistics(raw: bytes, filename: str, *, sheet_name=None, header_row=None) -> CSVPreview:
    if Path(filename).suffix.lower() == ".xlsx":
        from dzen_agent.statistics_excel import preview_excel
        return preview_excel(raw, sheet_name=sheet_name, header_row=header_row)
    if Path(filename).suffix.lower() not in (".csv", ".tsv", ".txt"):
        raise ValueError("Поддерживаются XLSX, CSV, TSV и TXT.")
    return preview_csv(raw)


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
    targets = [mapping.get(column, column) for column in preview.columns]
    if len(set(targets)) != len(targets):
        raise ValueError("Сопоставление перезапишет другую колонку. Назначьте колонкам разные показатели.")
    result = []
    for position, row in enumerate(preview.rows):
        index = preview.row_numbers[position] if preview.row_numbers else position + 2
        normalized = {mapping.get(k, k): v for k, v in row.items()}
        if preview.source_format == "xlsx":
            for column in set(mapping.values()) - METRICS:
                if normalized[column] is None:
                    normalized[column] = ""
                elif not isinstance(normalized[column], str):
                    normalized[column] = str(normalized[column])
        for column in set(mapping.values()) & METRICS:
            original = normalized[column]
            value = re.sub(r"[\s\u00a0%]", "", str(original if original is not None else "")).replace(",", ".")
            if value in ("", "-", "—"):
                normalized[column] = None
                continue
            try:
                number = float(value)
                if number < 0 or not __import__('math').isfinite(number):
                    raise ValueError()
                normalized[column] = original if isinstance(original, int) and not isinstance(original, bool) else number
            except ValueError as error:
                raise ValueError(f"Строка {index}, {column}: ожидается число.") from error
        result.append(normalized)
    return result


def import_csv(store: Store, filename: str, preview: CSVPreview, mapping=None) -> int:
    existing = store.one("SELECT id FROM statistics_imports WHERE sha256=?", (preview.sha256,))
    if existing:
        return existing["id"]
    rows = normalize_rows(preview, mapping)
    metadata = {}
    if preview.source_format == "xlsx":
        metadata["source"] = {"format": "xlsx", "sheet_name": preview.sheet_name,
            "header_row": preview.header_row, "file_sha256": preview.source_sha256,
            "percent_values": "percentage_points"}
    return store.execute(
        "INSERT INTO statistics_imports(filename,sha256,encoding,delimiter,columns_json,rows_json,"
        "created_at,metadata_json) VALUES(?,?,?,?,?,?,?,?)",
        (Path(filename).name, preview.sha256, preview.encoding, preview.delimiter,
         dumps(mapping if mapping is not None else preview.mapping), dumps(rows), now_iso(), dumps(metadata)),
    )


# The existing name stays callable for old integrations; both formats share validation and storage.
import_statistics = import_csv


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
