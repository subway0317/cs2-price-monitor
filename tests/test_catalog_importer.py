from __future__ import annotations

import csv
import json
from time import perf_counter

from cs2_price_monitor.catalog import item_catalog
from cs2_price_monitor.catalog.catalog_importer import (
    import_catalog_file,
    normalize_catalog_rows,
)
from cs2_price_monitor.catalog import catalog_loader


def _raw_item(index: int, **overrides):
    """Build one raw test item. / 构造一条测试原始数据。"""
    row = {
        "market_hash_name": f"Test Skin {index} (Field-Tested)",
        "display_name_en": f"Test Skin {index} (Field-Tested)",
        "display_name_cn": "",
        "item_type": "Weapon",
        "aliases": [f"testskin{index}", "shared"],
        "price_supported": True,
    }
    row.update(overrides)
    return row


def test_importer_handles_1000_json_rows_and_writes_catalog(tmp_path):
    raw_path = tmp_path / "raw.json"
    output_path = tmp_path / "catalog_data.json"
    raw_rows = [_raw_item(index) for index in range(1000)]
    raw_path.write_text(json.dumps(raw_rows), encoding="utf-8")

    result = import_catalog_file(raw_path, output_path)
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert result.stats.total_input == 1000
    assert result.stats.valid_count == 1000
    assert result.stats.invalid_count == 0
    assert len(written) == 1000


def test_duplicate_market_hash_name_merges_aliases_and_cn_name():
    rows = [
        _raw_item(1, market_hash_name="Duplicate Item", aliases=["alpha"], display_name_cn=""),
        _raw_item(2, market_hash_name="Duplicate Item", aliases=["beta"], display_name_cn="重复物品"),
    ]

    result = normalize_catalog_rows(rows)

    assert result.stats.valid_count == 1
    assert result.stats.duplicate_count == 1
    assert result.rows[0]["market_hash_name"] == "Duplicate Item"
    assert result.rows[0]["display_name_cn"] == "重复物品"
    assert result.rows[0]["aliases"] == ["alpha", "beta"]


def test_variants_are_not_merged_by_base_skin_name():
    rows = [
        _raw_item(1, market_hash_name="AK-47 | Redline (Field-Tested)"),
        _raw_item(2, market_hash_name="AK-47 | Redline (Minimal Wear)"),
        _raw_item(3, market_hash_name="StatTrak™ AK-47 | Redline (Field-Tested)"),
    ]

    result = normalize_catalog_rows(rows)
    names = {row["market_hash_name"] for row in result.rows}

    assert result.stats.valid_count == 3
    assert "AK-47 | Redline (Field-Tested)" in names
    assert "AK-47 | Redline (Minimal Wear)" in names
    assert "StatTrak™ AK-47 | Redline (Field-Tested)" in names


def test_invalid_empty_market_name_is_ignored_and_unknown_type_becomes_other():
    rows = [
        _raw_item(1, market_hash_name=""),
        _raw_item(2, market_hash_name="Unknown Type Item", item_type="Guessed Skin"),
    ]

    result = normalize_catalog_rows(rows)

    assert result.stats.invalid_count == 1
    assert result.stats.valid_count == 1
    assert result.rows[0]["item_type"] == "Other"


def test_csv_import_supports_field_mapping(tmp_path):
    raw_path = tmp_path / "raw.csv"
    output_path = tmp_path / "catalog_data.json"
    with raw_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "english", "kind", "search_terms"])
        writer.writeheader()
        writer.writerow(
            {
                "name": "CSV Item",
                "english": "CSV Item",
                "kind": "Case",
                "search_terms": "csv;case",
            }
        )

    result = import_catalog_file(
        raw_path,
        output_path,
        field_map={
            "market_hash_name": "name",
            "display_name_en": "english",
            "item_type": "kind",
            "aliases": "search_terms",
        },
    )

    assert result.stats.valid_count == 1
    assert result.rows[0]["aliases"] == ["csv", "case"]


def test_generated_catalog_search_handles_empty_cn_and_result_limit(tmp_path, monkeypatch):
    generated_path = tmp_path / "catalog_data.json"
    rows = [_raw_item(index, aliases=[f"bulk-search-{index}"]) for index in range(100)]
    generated_path.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(catalog_loader, "DEFAULT_GENERATED_CATALOG_PATH", generated_path)
    item_catalog._load_catalog.cache_clear()

    results = item_catalog.search_items("bulk", limit=20)

    assert len(results) == 20
    assert all(item.display_name_cn == "" for item in results)
    assert results[0].market_hash_name.startswith("Test Skin")


def test_search_10000_generated_catalog_rows_is_reasonable(tmp_path, monkeypatch):
    generated_path = tmp_path / "catalog_data.json"
    rows = [_raw_item(index, aliases=[f"perfalias{index}"]) for index in range(10000)]
    generated_path.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(catalog_loader, "DEFAULT_GENERATED_CATALOG_PATH", generated_path)
    item_catalog._load_catalog.cache_clear()

    start = perf_counter()
    results = item_catalog.search_items("perfalias9999", limit=20)
    elapsed = perf_counter() - start

    assert results[0].market_hash_name == "Test Skin 9999 (Field-Tested)"
    assert elapsed < 1.0
