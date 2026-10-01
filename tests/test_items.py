from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from cs2_price_monitor.models import Alert, DailyPriceSummary, Holding, PriceSnapshot, Transaction, TransactionType
from cs2_price_monitor.providers.manual import ManualPriceProvider
from cs2_price_monitor.services.items import (
    add_item,
    delete_item_with_related_data,
    list_enabled_items,
    set_item_active,
    update_item_metadata,
)
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


def test_delete_item_removes_related_rows_without_touching_other_items(session):
    wrong = add_item(session, "AK47 | Redline (Filed-Tested)")
    correct = add_item(session, "AK-47 | Redline (Field-Tested)")

    record_transaction(session, wrong.id, TransactionType.BUY, Decimal("1"), Decimal("100"))
    record_transaction(session, correct.id, TransactionType.BUY, Decimal("1"), Decimal("200"))
    record_price_quote(
        session,
        ManualPriceProvider(Decimal("10"), Decimal("9"), 3).get_quote(wrong.id),
    )
    record_price_quote(
        session,
        ManualPriceProvider(Decimal("20"), Decimal("18"), 4).get_quote(correct.id),
    )
    session.add(
        DailyPriceSummary(
            item_id=wrong.id,
            summary_date=date(2026, 7, 12),
            currency="CNY",
            open_price=Decimal("10"),
            high_price=Decimal("10"),
            low_price=Decimal("10"),
            close_price=Decimal("10"),
            median_price=Decimal("10"),
            snapshot_count=1,
        )
    )
    session.add(
        DailyPriceSummary(
            item_id=correct.id,
            summary_date=date(2026, 7, 12),
            currency="CNY",
            open_price=Decimal("20"),
            high_price=Decimal("20"),
            low_price=Decimal("20"),
            close_price=Decimal("20"),
            median_price=Decimal("20"),
            snapshot_count=1,
        )
    )
    session.add(Alert(item_id=wrong.id, severity="warning", message="wrong"))
    session.add(Alert(item_id=correct.id, severity="warning", message="correct"))
    session.flush()

    result = delete_item_with_related_data(session, wrong.id)

    assert result.market_name == "AK47 | Redline (Filed-Tested)"
    assert result.price_snapshots == 1
    assert result.daily_price_summaries == 1
    assert result.transactions == 1
    assert result.holdings == 1
    assert result.alerts == 1
    assert session.get(type(wrong), wrong.id) is None
    assert session.get(type(correct), correct.id) is not None
    assert session.scalar(select(PriceSnapshot).where(PriceSnapshot.item_id == correct.id)) is not None
    assert session.scalar(select(Transaction).where(Transaction.item_id == correct.id)) is not None
    assert session.scalar(select(DailyPriceSummary).where(DailyPriceSummary.item_id == correct.id)) is not None
    assert session.scalar(select(Holding).where(Holding.item_id == correct.id)) is not None
    assert session.scalar(select(Alert).where(Alert.item_id == correct.id)) is not None


def test_delete_item_raises_for_missing_item(session):
    with pytest.raises(ValueError, match="Item 999 not found"):
        delete_item_with_related_data(session, 999)
