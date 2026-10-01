from __future__ import annotations

import pytest
from sqlalchemy import select

from cs2_price_monitor.catalog import get_all_items, search_items
from cs2_price_monitor.models import Item
from cs2_price_monitor.services.items import add_item, delete_item_with_related_data


def _names(keyword: str) -> list[str]:
    """Return market names for a test search."""
    return [item.market_hash_name for item in search_items(keyword)]


def test_catalog_loads_items_with_required_metadata():
    items = get_all_items()

    assert items
    assert all(item.market_hash_name for item in items)
    assert all(item.item_type for item in items)


def test_english_partial_search_finds_breakout_case():
    assert _names("break")[0] == "Operation Breakout Weapon Case"


def test_alias_search_finds_phoenix_case():
    assert _names("phoenix")[0] == "Operation Phoenix Weapon Case"


def test_chinese_search_finds_breakout_case():
    assert "Operation Breakout Weapon Case" in _names("凤凰大")


def test_chinese_shared_alias_returns_chroma_cases():
    names = _names("幻彩")

    assert "Chroma Case" in names
    assert "Chroma 2 Case" in names
    assert "Chroma 3 Case" in names


def test_partial_alias_search_finds_huntsman_case():
    assert _names("hunts")[0] == "Huntsman Weapon Case"


def test_search_is_case_insensitive():
    assert _names("BrEaKoUt")[0] == "Operation Breakout Weapon Case"


def test_search_ignores_spaces():
    assert _names("chroma2")[0] == "Chroma 2 Case"
    assert _names("chroma 2")[0] == "Chroma 2 Case"


def test_missing_keyword_returns_empty_list():
    assert search_items("not-a-real-cs2-item") == []


def test_catalog_search_finds_item_not_in_empty_database(session):
    assert session.scalars(select(Item)).all() == []

    names = _names("break")

    assert "Operation Breakout Weapon Case" in names


def test_add_catalog_result_creates_monitored_item(session):
    catalog_item = search_items("break")[0]

    added = add_item(
        session,
        catalog_item.market_hash_name,
        catalog_item.display_name_cn,
        catalog_item.item_type,
    )

    stored = session.get(Item, added.id)
    assert stored is not None
    assert stored.market_name == "Operation Breakout Weapon Case"
    assert stored.buff_name == catalog_item.display_name_cn
    assert stored.category == catalog_item.item_type


def test_duplicate_catalog_add_is_rejected(session):
    catalog_item = search_items("break")[0]
    add_item(session, catalog_item.market_hash_name, catalog_item.display_name_cn, catalog_item.item_type)

    with pytest.raises(ValueError, match="already exists"):
        add_item(
            session,
            catalog_item.market_hash_name,
            catalog_item.display_name_cn,
            catalog_item.item_type,
        )


def test_catalog_search_still_finds_item_after_local_delete(session):
    catalog_item = search_items("break")[0]
    added = add_item(
        session,
        catalog_item.market_hash_name,
        catalog_item.display_name_cn,
        catalog_item.item_type,
    )
    delete_item_with_related_data(session, added.id)

    names = _names("break")

    assert "Operation Breakout Weapon Case" in names
    assert session.scalar(select(Item).where(Item.market_name == catalog_item.market_hash_name)) is None


def test_catalog_search_results_do_not_depend_on_database_contents(session):
    before = _names("幻彩")
    add_item(session, "Some Local Only Item")
    after = _names("幻彩")

    assert before == after
