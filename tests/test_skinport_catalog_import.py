from __future__ import annotations

from sqlalchemy import select

from cs2_price_monitor.catalog import get_all_items, search_items
from cs2_price_monitor.models import Item, PriceSnapshot
from cs2_price_monitor.ui.streamlit_app import _catalog_display_name


def _catalog_map():
    """Build a lookup by market_hash_name and version."""
    return {(item.market_hash_name, item.version): item for item in get_all_items()}


def test_skinport_catalog_loads_20006_records():
    assert len(get_all_items()) == 20006


def test_market_hash_name_and_version_are_not_merged():
    catalog = _catalog_map()

    assert ("★ Bayonet | Doppler (Factory New)", "Phase 1") in catalog
    assert ("★ Bayonet | Doppler (Factory New)", "Phase 2") in catalog
    assert ("★ Bayonet | Doppler (Factory New)", "Sapphire") in catalog
    assert ("★ Bayonet | Doppler (Factory New)", "Ruby") in catalog
    assert len(
        {
            catalog[("★ Bayonet | Doppler (Factory New)", "Phase 1")].catalog_id,
            catalog[("★ Bayonet | Doppler (Factory New)", "Phase 2")].catalog_id,
            catalog[("★ Bayonet | Doppler (Factory New)", "Sapphire")].catalog_id,
            catalog[("★ Bayonet | Doppler (Factory New)", "Ruby")].catalog_id,
        }
    ) == 4


def test_stattrak_and_normal_versions_are_not_merged():
    names = {item.market_hash_name for item in get_all_items()}

    assert "AK-47 | Redline (Field-Tested)" in names
    assert "StatTrak™ AK-47 | Redline (Field-Tested)" in names


def test_different_wear_levels_are_not_merged():
    names = {item.market_hash_name for item in get_all_items()}

    assert "AK-47 | Redline (Field-Tested)" in names
    assert "AK-47 | Redline (Minimal Wear)" in names


def test_curated_chinese_names_and_aliases_are_preserved():
    catalog = _catalog_map()
    item = catalog[("Operation Breakout Weapon Case", "")]

    assert item.display_name_cn == "凤凰大行动武器箱"
    assert "breakout" in item.aliases
    assert "凤凰大行动" in item.aliases


def test_empty_sqlite_database_still_searches_full_catalog(session):
    assert session.scalars(select(Item)).all() == []

    results = search_items("sapphire", limit=20)

    assert any(item.version == "Sapphire" for item in results)


def test_catalog_search_does_not_depend_on_monitored_items(session):
    before = [item.catalog_id for item in search_items("ruby", limit=10)]
    session.add(Item(market_name="Local Only Item", is_active=True))
    session.flush()
    after = [item.catalog_id for item in search_items("ruby", limit=10)]

    assert before == after


def test_search_result_display_includes_version():
    item = next(item for item in search_items("phase 1", limit=20) if item.version == "Phase 1")

    assert _catalog_display_name(item).endswith("— Phase 1")


def test_skinport_static_prices_are_not_catalog_fields_or_snapshots(session):
    item = search_items("AK-47 | Redline", limit=1)[0]

    assert not hasattr(item, "suggested_price")
    assert not hasattr(item, "min_price")
    assert session.scalars(select(PriceSnapshot)).all() == []
