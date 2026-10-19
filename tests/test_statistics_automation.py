import json
from unittest.mock import Mock

import pytest
from openpyxl import Workbook

from dzen_agent.analytics_input import prepare_analytics_input
from dzen_agent.browser.scheduling import DzenScheduling
from dzen_agent.statistics import import_csv, preview_csv
from dzen_agent.store import Store


@pytest.mark.parametrize("filter_type", ["by-event", "by-publication"])
def test_observed_export_retains_original_import_and_honest_period(tmp_path, filter_type):
    store = Store(tmp_path / "stats.db")
    store.initialize()
    old_id = import_csv(store, "old.csv", preview_csv(b"title;views\nOld;42"))
    old = store.one("SELECT * FROM statistics_imports WHERE id=?", (old_id,))
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Дата публикации", "Публикация", "Показы", "Открытия", "Дочитывания и просмотры", "Дополнительный показатель"])
    sheet.append(["2026-10-01", "Первый фильм", 200, 20, 15, 17])
    sheet.append(["2026-10-02", "Второй фильм", 100, 10, 8, 19])
    sheet.append([None, "Всего", 300, 30, 23, 36])
    path = tmp_path / "original.xlsx"
    workbook.save(path)
    original = path.read_bytes()
    adapter = Mock(verified=True)
    adapter.collect_statistics.return_value = {"path": str(path), "from":"2026-09-19", "to":"2026-10-18", "filter_type":filter_type}
    dzen = DzenScheduling(store, adapter)
    imported = dzen.collect_statistics()
    row = store.one("SELECT * FROM statistics_imports WHERE id=?", (imported,))
    assert row["id"] != old_id
    assert store.one("SELECT * FROM statistics_imports WHERE id=?", (old_id,)) == old
    assert path.read_bytes() == original
    context, _ = prepare_analytics_input(store, imported)
    assert context["analysis_config"]["output_requirements"]["articles_total"] == 2
    metadata = json.loads(row["metadata_json"])
    if filter_type == "by-event":
        assert metadata["measurement_window"] == "calendar_period"
        assert metadata["period"]["start"] == "2026-09-19" and metadata["period"]["as_of"] is None
    else:
        assert metadata["measurement_window"] == "unknown"
        assert metadata["period"]["start"] is None and metadata["period"]["as_of"] is None
    assert dzen.collect_statistics() == imported
    assert len(store.rows("SELECT * FROM statistics_imports")) == 2
