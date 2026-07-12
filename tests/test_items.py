from __future__ import annotations

from decimal import Decimal

import pytest

from cs2_price_monitor.models import PriceSnapshot, Transaction, TransactionType
from cs2_price_monitor.providers.manual import ManualPriceProvider
from cs2_price_monitor.services.items import add_item, list_enabled_items, set_item_active, update_item_metadata
from cs2_price_monitor.services.prices import record_price_quote
from cs2_price_monitor.services.transactions import record_transaction


def test_add_item_rejects_duplicate_market_hash_name(session):
    add_item(session, "AK-47 | Redline (Field-Tested)")

    with pytest.raises(ValueError, match="already exists"):
        add_item(session, "AK-47 | Redline (Field-Tested)")


def test_list_enabled_items_excludes_disabled_items(session):
    enabled = add_item(session, "AWP | Asiimov (Battle-Scarred)")
    disabled = add_item(session, "USP-S | Kill Confirmed (Field-Tested)")
    set_item_active(session, disabled.id, False)

    rows = list_enabled_items(session)

    assert [row.id for row in rows] == [enabled.id]


def test_update_item_metadata(session):
    item = add_item(session, "M4A1-S | Printstream (Field-Tested)")

    update_item_metadata(session, item.id, "印花集", "rifle")

    assert item.buff_name == "印花集"
    assert item.category == "rifle"


def test_disabling_item_preserves_history(session):
    item = add_item(session, "Desert Eagle | Blaze (Factory New)")
    record_transaction(session, item.id, TransactionType.BUY, Decimal("1"), Decimal("500"))
    record_price_quote(
        session,
        ManualPriceProvider(Decimal("650"), Decimal("620"), 3).get_quote(item.id),
    )

    set_item_active(session, item.id, False)

    assert session.query(Transaction).filter_by(item_id=item.id).count() == 1
    assert session.query(PriceSnapshot).filter_by(item_id=item.id).count() == 1
    assert item.is_active is False

