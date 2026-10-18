import hashlib
import io
import json
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

from dzen_agent.analytics_input import build_analytics_input, validate_metadata
from dzen_agent.config import PROJECT_ROOT, get_settings
from dzen_agent.statistics import import_statistics, normalize_rows, preview_csv, preview_statistics
from dzen_agent.statistics_excel import excel_sheets, preview_excel
from dzen_agent.store import Store, dumps


def book_bytes(sheets, formats=None):
    """Disposable, in-memory Excel test fixtures; never save or edit a user's workbook."""
    book = Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(row)
        for coordinate, number_format in (formats or {}).get(name, {}).items():
            sheet[coordinate].number_format = number_format
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def test_excel_dates_counts_percentages_and_all_unknown_columns(tmp_path):
    raw = book_bytes({"Материалы": [
        ["Статистика канала Дзен"], ["Период: октябрь 2026"], [],
        ["Публикация", "Дата публикации", "Просмотры", "Показы", "CTR, %", "Новый показатель", "article_id"],
        ["Кино", datetime(2026, 10, 12, 6, 0), 1234, 2000, 0.035, 18.125, 123456789012345],
        ["Архив", datetime(2026, 10, 13, 16, 0), 0, None, None, None, "a2"]]},
        {"Материалы": {"E5": "0.0%"}})
    original = tmp_path / "original.xlsx"
    original.write_bytes(raw)
    before = hashlib.sha256(raw).hexdigest()
    preview = preview_statistics(original.read_bytes(), original.name)
    assert preview.header_row == 4
    assert preview.sheet_name == "Материалы"
    rows = normalize_rows(preview)
    assert rows[0] == {"title": "Кино", "date": "2026-10-12T06:00:00", "views": 1234,
        "impressions": 2000, "ctr": 3.5, "Новый показатель": 18.125, "article_id": "123456789012345"}
    assert rows[1]["views"] == 0 and rows[1]["impressions"] is None and rows[1]["ctr"] is None
    store = Store(tmp_path / "stats.db")
    store.initialize()
    import_id = import_statistics(store, original.name, preview)
    saved = store.one("SELECT * FROM statistics_imports WHERE id=?", (import_id,))
    assert json.loads(saved["rows_json"]) == rows
    metadata = json.loads(saved["metadata_json"])
    assert metadata["source"]["sheet_name"] == "Материалы"
    assert metadata["source"]["file_sha256"] == before
    validate_metadata(metadata)
    assert import_statistics(store, "renamed.xlsx", preview) == import_id
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before


def test_excel_sheet_selection_and_distinct_import_identity(tmp_path):
    raw = book_bytes({"Статьи": [["Заголовок", "Просмотры"], ["Статья", 10]],
                      "Видео": [["Заголовок", "Просмотры"], ["Видео", 20]]})
    assert excel_sheets(raw) == ["Статьи", "Видео"]
    with pytest.raises(ValueError, match="Выберите лист"):
        preview_excel(raw)
    store = Store(tmp_path / "stats.db")
    store.initialize()
    first = import_statistics(store, "stats.xlsx", preview_excel(raw, sheet_name="Статьи"))
    second = import_statistics(store, "stats.xlsx", preview_excel(raw, sheet_name="Видео"))
    assert first != second
    assert len(store.rows("SELECT * FROM statistics_imports")) == 2
    with pytest.raises(ValueError, match="не найден"):
        preview_excel(raw, sheet_name="Missing")


def test_excel_and_csv_pass_the_same_normalized_data_to_analytics(tmp_path):
    columns = ["article_id", "title", "impressions", "opens", "completions", "views", "CTR"]
    values = ["a", "Кино", 1000, 100, 80, 125, "10%"]
    excel = preview_excel(book_bytes({"Stats": [columns, values]}))
    csv = preview_csv((";".join(columns) + "\n" + ";".join(map(str, values))).encode())
    assert normalize_rows(excel) == normalize_rows(csv)
    store = Store(tmp_path / "stats.db")
    store.initialize()
    excel_id = import_statistics(store, "stats.xlsx", excel)
    csv_id = import_statistics(store, "stats.csv", csv)
    for import_id in (excel_id, csv_id):
        metadata = json.loads(store.one("SELECT metadata_json FROM statistics_imports WHERE id=?", (import_id,))["metadata_json"])
        metadata["measurement_window"] = "first_7d"
        metadata["metric_definitions"] = {"ctr_compatible": True}
        store.execute("UPDATE statistics_imports SET metadata_json=? WHERE id=?", (dumps(metadata), import_id))
    from_excel = build_analytics_input(store, excel_id)
    from_csv = build_analytics_input(store, csv_id)
    assert from_excel["articles"] == from_csv["articles"]
    assert from_excel["analysis_config"]["validated_funnel"] == from_csv["analysis_config"]["validated_funnel"]
    assert from_excel["analysis_config"]["validated_funnel"]["ctr"] == 0.1
    assert from_excel["run_context"]["period"]["start"] is None


def test_excel_manual_header_mapping_and_missing_header_preserve_values():
    raw = book_bytes({"Stats": [["Описание"], ["Материал", "Число", None], ["Кино", 42, 18.5]]})
    preview = preview_excel(raw, header_row=2)
    assert preview.columns == ["Материал", "Число", "Колонка 3"]
    assert normalize_rows(preview, {"Материал": "title", "Число": "views"}) == [
        {"title": "Кино", "views": 42, "Колонка 3": 18.5}]
    for header in (0, 51, True, "2", 5):
        with pytest.raises(ValueError):
            preview_excel(raw, header_row=header)


@pytest.mark.parametrize("value", [-1, "не число", True])
def test_invalid_excel_metrics_show_actual_source_row(value):
    raw = book_bytes({"Stats": [["Описание"], [], ["Заголовок", "Просмотры"], ["Кино", value]]})
    preview = preview_excel(raw)
    with pytest.raises(ValueError, match="Строка 4, views"):
        normalize_rows(preview)


def test_excel_formula_requires_a_cached_result_and_errors_are_not_zero():
    raw = book_bytes({"Stats": [["Заголовок", "Просмотры"], ["Кино", "=1+2"]]})
    with pytest.raises(ValueError, match="B2.*сохранённого результата"):
        preview_excel(raw)
    output = io.BytesIO()
    with ZipFile(io.BytesIO(raw)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                root = ET.fromstring(data)
                namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                cell = next(c for c in root.iter(namespace + "c") if c.get("r") == "B2")
                cell.find(namespace + "v").text = "3"
                data = ET.tostring(root)
            target.writestr(item, data)
    assert normalize_rows(preview_excel(output.getvalue()))[0]["views"] == 3
    error = book_bytes({"Stats": [["Заголовок", "Просмотры"], ["Кино", "#DIV/0!"]]})
    with pytest.raises(ValueError, match="B2.*ошибка Excel"):
        preview_excel(error)


def test_excel_durations_large_counts_and_literal_percent_formats():
    raw = book_bytes({"Stats": [["title", "views", "read_time_seconds", "CTR"],
        ["Кино", 123456789012345, timedelta(minutes=5), 3.5]]},
        {"Stats": {"C2": "[h]:mm:ss", "D2": '0.0"%"'}})
    row = normalize_rows(preview_excel(raw))[0]
    assert row["views"] == 123456789012345
    assert row["read_time_seconds"] == 300
    assert row["ctr"] == 3.5


@pytest.mark.parametrize("raw", [b"", b"not an excel workbook", b"PK\x03\x04broken"])
def test_bad_excel_has_a_friendly_error(raw):
    with pytest.raises(ValueError, match="XLSX"):
        excel_sheets(raw)


def test_empty_duplicate_headers_and_mapping_collisions_are_not_silently_imported():
    with pytest.raises(ValueError, match="пуст"):
        preview_excel(book_bytes({"Empty": []}))
    with pytest.raises(ValueError, match="нет строк"):
        preview_excel(book_bytes({"Stats": [["title", "views"]]}))
    with pytest.raises(ValueError, match="одинаковые"):
        preview_excel(book_bytes({"Stats": [["views", "views"], [1, 2]]}))
    preview = preview_excel(book_bytes({"Stats": [["custom", "views"], [1, 2]]}))
    with pytest.raises(ValueError, match="перезапишет"):
        normalize_rows(preview, {"custom": "views"})


def test_original_text_formats_still_dispatch_without_excel():
    for name in ["stats.csv", "stats.tsv", "stats.txt"]:
        assert normalize_rows(preview_statistics(b"title\tviews\na\t12", name))[0]["views"] == 12
    with pytest.raises(ValueError, match="Поддерживаются"):
        preview_statistics(b"binary", "stats.xls")


def test_excel_sheet_selection_mapping_and_import_in_streamlit(tmp_path):
    raw = book_bytes({"Другой лист": [["title", "views"], ["Other", 1]],
                      "Материалы": [["Мой заголовок", "Мой показатель"], ["Кино", 123]]})
    store = Store(tmp_path / "ui.db")
    store.initialize()
    config = replace(get_settings(), database_path=store.database_path, aitunnel_api_key=None)
    upload = SimpleNamespace(name="original.xlsx", getvalue=lambda: raw)
    with patch("dzen_agent.config.get_settings", return_value=config), patch("streamlit.file_uploader", return_value=upload):
        app = AppTest.from_file(str(PROJECT_ROOT / "dzen_agent/ui.py"), default_timeout=20).run()
        app.sidebar.radio[0].set_value("Статистика").run()
        next(x for x in app.selectbox if x.label == "Лист Excel").set_value("Материалы").run()
        assert not app.exception and not app.error
        next(x for x in app.selectbox if x.label == "Мой заголовок").set_value("title")
        next(x for x in app.selectbox if x.label == "Мой показатель").set_value("views")
        next(x for x in app.button if x.label == "Сохранить импорт").click().run()
        assert not app.exception and not app.error
        row = store.one("SELECT * FROM statistics_imports")
        assert json.loads(row["rows_json"]) == [{"title": "Кино", "views": 123}]
        assert json.loads(row["metadata_json"])["source"]["sheet_name"] == "Материалы"
        next(x for x in app.selectbox if x.label == "Лист Excel").set_value("Другой лист").run()
        assert any(frame.value.iloc[0].tolist() == ["Other", 1] for frame in app.dataframe if len(frame.value) == 1)
