import pytest

from dzen_agent.statistics import import_csv, normalize_rows, preview_csv
from dzen_agent.store import Store


@pytest.mark.parametrize("encoding,separator", [("utf-8-sig", ";"), ("cp1251", ";"),
                                                ("utf-16", "\t"), ("utf-8", ",")])
def test_csv_encodings(encoding, separator):
    raw = separator.join(["Заголовок", "Просмотры"]) + "\n" + separator.join(["Статья", "1234"])
    preview = preview_csv(raw.encode(encoding))
    assert normalize_rows(preview)[0] == {"title": "Статья", "views": 1234}


def test_decimal_and_duplicate_import(tmp_path):
    preview = preview_csv('sep=;\nЗаголовок;CTR;Просмотры\nТема;"3,5%";"1 234"'.encode())
    assert normalize_rows(preview)[0]["ctr"] == 3.5
    store = Store(tmp_path / "test.db")
    store.initialize()
    assert import_csv(store, "a.csv", preview) == import_csv(store, "a.csv", preview)


def test_custom_mapping_and_malformed_csv():
    preview = preview_csv(b"custom;count\na;2\n")
    assert normalize_rows(preview, {"custom": "title", "count": "views"})[0]["views"] == 2
    with pytest.raises(ValueError):
        preview_csv(b"title;views\na;1;extra")
    with pytest.raises(ValueError):
        normalize_rows(preview)
