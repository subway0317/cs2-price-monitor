from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from cs2_price_monitor.models import Transaction, TransactionAuditLog, TransactionType
from cs2_price_monitor.providers.manual import ManualPriceProvider
from cs2_price_monitor.services.analytics import calculate_portfolio
from cs2_price_monitor.services.items import add_item
from cs2_price_monitor.services.prices import record_price_quote
from cs2_price_monitor.services.transactions import (
    delete_transaction,
    list_invalid_transaction_quantities,
    parse_transaction_quantity,
    record_transaction,
    update_transaction,
)


def test_moving_weighted_average_cost(session):
    item = add_item(session, "AK-47 | Redline (Field-Tested)")
    record_transaction(session, item.id, TransactionType.BUY, Decimal("2"), Decimal("100"))
    holding = record_transaction(
        session, item.id, TransactionType.BUY, Decimal("1"), Decimal("160"), fee=Decimal("5")
    ).item.holding

    assert holding.quantity == Decimal("3.000000")
    assert holding.avg_cost == Decimal("121.666667")


def test_sell_reduces_quantity_and_keeps_average_cost(session):
    item = add_item(session, "M4A1-S | Printstream (Field-Tested)")
    record_transaction(session, item.id, TransactionType.BUY, Decimal("2"), Decimal("200"))
    tx = record_transaction(session, item.id, TransactionType.SELL, Decimal("1"), Decimal("250"))

    assert tx.item.holding.quantity == Decimal("1.000000")
    assert tx.item.holding.avg_cost == Decimal("200.000000")
    assert tx.item.holding.realized_profit == Decimal("50.000000")


def test_create_integer_quantities_succeeds(session):
    item = add_item(session, "AK-47 | Vulcan (Field-Tested)")

    for quantity in [1, 2, 10]:
        record_transaction(session, item.id, TransactionType.BUY, quantity, Decimal("100"))

    assert item.holding.quantity == Decimal("13.000000")


@pytest.mark.parametrize(
    "quantity",
    [
        0,
        -1,
        Decimal("0.000001"),
        Decimal("1.5"),
        Decimal("1.000001"),
        True,
        "nan",
        "Infinity",
    ],
)
def test_create_rejects_invalid_quantities(session, quantity):
    item = add_item(session, "M4A4 | The Emperor (Field-Tested)")

    with pytest.raises(ValueError, match="交易数量必须是大于等于 1 的整数"):
        record_transaction(session, item.id, TransactionType.BUY, quantity, Decimal("100"))


def test_sell_rejects_same_invalid_quantity_rules(session):
    item = add_item(session, "P90 | Asiimov (Field-Tested)")
    record_transaction(session, item.id, TransactionType.BUY, 2, Decimal("100"))

    with pytest.raises(ValueError, match="交易数量必须是大于等于 1 的整数"):
        record_transaction(session, item.id, TransactionType.SELL, Decimal("1.5"), Decimal("120"))


def test_parse_quantity_does_not_truncate_fractional_values():
    with pytest.raises(ValueError, match="交易数量必须是大于等于 1 的整数"):
        parse_transaction_quantity(Decimal("1.9"))


def test_update_buy_quantity_recalculates_inventory(session):
    item = add_item(session, "AK-47 | Slate (Field-Tested)")
    buy = record_transaction(session, item.id, TransactionType.BUY, Decimal("10"), Decimal("2"))
    record_transaction(session, item.id, TransactionType.SELL, Decimal("4"), Decimal("5"))

    update_transaction(
        session,
        buy.id,
        item_id=item.id,
        transaction_type=TransactionType.BUY,
        quantity=Decimal("8"),
        unit_price=Decimal("2"),
        occurred_at=buy.occurred_at,
    )

    assert item.holding.quantity == Decimal("4.000000")
    assert item.holding.avg_cost == Decimal("2.000000")
    assert item.holding.realized_profit == Decimal("12.000000")


def test_update_buy_unit_price_recalculates_average_cost(session):
    item = add_item(session, "M4A4 | 龍王 (Field-Tested)")
    first = record_transaction(session, item.id, TransactionType.BUY, Decimal("2"), Decimal("100"))
    record_transaction(session, item.id, TransactionType.BUY, Decimal("2"), Decimal("200"))

    update_transaction(
        session,
        first.id,
        item_id=item.id,
        transaction_type=TransactionType.BUY,
        quantity=Decimal("2"),
        unit_price=Decimal("150"),
        occurred_at=first.occurred_at,
    )

    assert item.holding.quantity == Decimal("4.000000")
    assert item.holding.avg_cost == Decimal("175.000000")


def test_update_buy_to_sell_recalculates_inventory(session):
    item = add_item(session, "USP-S | Kill Confirmed (Field-Tested)")
    record_transaction(
        session,
        item.id,
        TransactionType.BUY,
        Decimal("10"),
        Decimal("2"),
        occurred_at=_dt(2026, 1, 1),
    )
    tx = record_transaction(
        session,
        item.id,
        TransactionType.BUY,
        Decimal("3"),
        Decimal("3"),
        occurred_at=_dt(2026, 1, 2),
    )

    update_transaction(
        session,
        tx.id,
        item_id=item.id,
        transaction_type=TransactionType.SELL,
        quantity=Decimal("4"),
        unit_price=Decimal("5"),
        occurred_at=tx.occurred_at,
    )

    assert item.holding.quantity == Decimal("6.000000")
    assert item.holding.avg_cost == Decimal("2.000000")
    assert item.holding.realized_profit == Decimal("12.000000")


def test_update_sell_to_buy_recalculates_inventory(session):
    item = add_item(session, "AWP | Asiimov (Field-Tested)")
    record_transaction(session, item.id, TransactionType.BUY, Decimal("10"), Decimal("2"))
    tx = record_transaction(session, item.id, TransactionType.SELL, Decimal("4"), Decimal("5"))

    update_transaction(
        session,
        tx.id,
        item_id=item.id,
        transaction_type=TransactionType.BUY,
        quantity=Decimal("4"),
        unit_price=Decimal("5"),
        occurred_at=tx.occurred_at,
    )

    assert item.holding.quantity == Decimal("14.000000")
    assert item.holding.avg_cost == Decimal("2.857143")
    assert item.holding.realized_profit == Decimal("0.000000")


def test_update_transaction_item_recalculates_old_and_new_items(session):
    old_item = add_item(session, "P250 | See Ya Later (Field-Tested)")
    new_item = add_item(session, "Glock-18 | Vogue (Field-Tested)")
    tx = record_transaction(session, old_item.id, TransactionType.BUY, Decimal("5"), Decimal("10"))
    record_transaction(session, new_item.id, TransactionType.BUY, Decimal("2"), Decimal("20"))

    update_transaction(
        session,
        tx.id,
        item_id=new_item.id,
        transaction_type=TransactionType.BUY,
        quantity=Decimal("5"),
        unit_price=Decimal("10"),
        occurred_at=tx.occurred_at,
    )

    assert old_item.holding.quantity == Decimal("0.000000")
    assert old_item.holding.avg_cost == Decimal("0.000000")
    assert new_item.holding.quantity == Decimal("7.000000")
    assert new_item.holding.avg_cost == Decimal("12.857143")


def test_update_date_reorders_transactions_and_recalculates(session):
    item = add_item(session, "FAMAS | Mecha Industries (Minimal Wear)")
    record_transaction(
        session,
        item.id,
        TransactionType.BUY,
        Decimal("10"),
        Decimal("1"),
        occurred_at=_dt(2026, 1, 1),
    )
    buy = record_transaction(
        session,
        item.id,
        TransactionType.BUY,
        Decimal("10"),
        Decimal("3"),
        occurred_at=_dt(2026, 1, 3),
    )
    record_transaction(
        session,
        item.id,
        TransactionType.SELL,
        Decimal("5"),
        Decimal("5"),
        occurred_at=_dt(2026, 1, 2),
    )

    update_transaction(
        session,
        buy.id,
        item_id=item.id,
        transaction_type=TransactionType.BUY,
        quantity=Decimal("10"),
        unit_price=Decimal("3"),
        occurred_at=_dt(2025, 12, 31),
    )

    assert item.holding.quantity == Decimal("15.000000")
    assert item.holding.avg_cost == Decimal("2.000000")
    assert item.holding.realized_profit == Decimal("15.000000")


def test_update_rejects_negative_historical_inventory(session):
    item = add_item(session, "Galil AR | Chromatic Aberration (Field-Tested)")
    buy = record_transaction(
        session,
        item.id,
        TransactionType.BUY,
        Decimal("10"),
        Decimal("1"),
        occurred_at=_dt(2026, 1, 1),
    )
    record_transaction(
        session,
        item.id,
        TransactionType.SELL,
        Decimal("8"),
        Decimal("2"),
        occurred_at=_dt(2026, 1, 5),
    )

    with pytest.raises(ValueError, match="2026-01-05"):
        update_transaction(
            session,
            buy.id,
            item_id=item.id,
            transaction_type=TransactionType.BUY,
            quantity=Decimal("5"),
            unit_price=Decimal("1"),
            occurred_at=buy.occurred_at,
        )

    assert item.holding.quantity == Decimal("2.000000")


def test_update_rejects_invalid_quantities(session):
    item = add_item(session, "Galil AR | Eco (Field-Tested)")
    tx = record_transaction(session, item.id, TransactionType.BUY, 2, Decimal("100"))

    with pytest.raises(ValueError, match="交易数量必须是大于等于 1 的整数"):
        update_transaction(
            session,
            tx.id,
            item_id=item.id,
            transaction_type=TransactionType.BUY,
            quantity=Decimal("1.000001"),
            unit_price=Decimal("100"),
            occurred_at=tx.occurred_at,
        )

    assert item.holding.quantity == Decimal("2.000000")


def test_invalid_quantity_diagnostic_lists_bad_rows(session):
    item = add_item(session, "MP7 | Bloodsport (Field-Tested)")
    session.add(
        Transaction(
            item_id=item.id,
            transaction_type=TransactionType.BUY.value,
            quantity=Decimal("0.000001"),
            unit_price=Decimal("100"),
            fee=Decimal("0"),
            occurred_at=_dt(2026, 1, 1),
        )
    )
    session.flush()

    rows = list_invalid_transaction_quantities(session)

    assert len(rows) == 1
    assert rows[0].item_name == item.market_name
    assert rows[0].quantity == "0.000001"


def test_delete_buy_rejects_negative_historical_inventory(session):
    item = add_item(session, "MP9 | Food Chain (Minimal Wear)")
    buy = record_transaction(
        session,
        item.id,
        TransactionType.BUY,
        Decimal("10"),
        Decimal("1"),
        occurred_at=_dt(2026, 1, 1),
    )
    record_transaction(
        session,
        item.id,
        TransactionType.SELL,
        Decimal("8"),
        Decimal("2"),
        occurred_at=_dt(2026, 1, 5),
    )

    with pytest.raises(ValueError, match="2026-01-05"):
        delete_transaction(session, buy.id)

    assert item.holding.quantity == Decimal("2.000000")


def test_delete_sell_restores_inventory(session):
    item = add_item(session, "Five-SeveN | Fairy Tale (Field-Tested)")
    record_transaction(session, item.id, TransactionType.BUY, Decimal("10"), Decimal("2"))
    sell = record_transaction(session, item.id, TransactionType.SELL, Decimal("4"), Decimal("5"))

    delete_transaction(session, sell.id)

    assert item.holding.quantity == Decimal("10.000000")
    assert item.holding.avg_cost == Decimal("2.000000")
    assert item.holding.realized_profit == Decimal("0.000000")


def test_edit_recalculates_portfolio_value_and_profit(session):
    item = add_item(session, "Desert Eagle | Printstream (Field-Tested)")
    buy = record_transaction(session, item.id, TransactionType.BUY, Decimal("2"), Decimal("100"))
    record_price_quote(
        session,
        ManualPriceProvider(Decimal("150"), Decimal("140"), 3).get_quote(item.id),
    )

    before = calculate_portfolio(session)[0]
    update_transaction(
        session,
        buy.id,
        item_id=item.id,
        transaction_type=TransactionType.BUY,
        quantity=Decimal("3"),
        unit_price=Decimal("120"),
        occurred_at=buy.occurred_at,
    )
    after = calculate_portfolio(session)[0]

    assert before.listing_value == Decimal("300.000000000000")
    assert before.unrealized_profit == Decimal("100.000000000000")
    assert after.listing_value == Decimal("450.000000000000")
    assert after.known_cost == Decimal("360.000000000000")
    assert after.unrealized_profit == Decimal("90.000000000000")


def test_edit_recalculates_realized_and_unrealized_profit(session):
    item = add_item(session, "MAC-10 | Disco Tech (Field-Tested)")
    buy = record_transaction(session, item.id, TransactionType.BUY, Decimal("10"), Decimal("10"))
    record_transaction(session, item.id, TransactionType.SELL, Decimal("4"), Decimal("15"))
    record_price_quote(
        session,
        ManualPriceProvider(Decimal("20"), Decimal("18"), 3).get_quote(item.id),
    )

    update_transaction(
        session,
        buy.id,
        item_id=item.id,
        transaction_type=TransactionType.BUY,
        quantity=Decimal("10"),
        unit_price=Decimal("12"),
        occurred_at=buy.occurred_at,
    )

    row = calculate_portfolio(session)[0]
    assert item.holding.realized_profit == Decimal("12.000000")
    assert row.unrealized_profit == Decimal("48.000000000000")


def test_update_rolls_back_when_database_operation_fails(session, monkeypatch):
    item = add_item(session, "Tec-9 | Fuel Injector (Factory New)")
    tx = record_transaction(session, item.id, TransactionType.BUY, Decimal("2"), Decimal("100"))
    session.commit()

    def fail_flush():
        raise RuntimeError("flush failed")

    monkeypatch.setattr(session, "flush", fail_flush)
    with pytest.raises(RuntimeError, match="flush failed"):
        update_transaction(
            session,
            tx.id,
            item_id=item.id,
            transaction_type=TransactionType.BUY,
            quantity=Decimal("5"),
            unit_price=Decimal("100"),
            occurred_at=tx.occurred_at,
        )

    session.rollback()
    monkeypatch.undo()
    session.expire_all()
    refreshed = session.get(type(tx), tx.id)
    assert refreshed.quantity == Decimal("2.000000")
    assert session.query(TransactionAuditLog).count() == 0


def test_audit_log_records_update_before_and_after(session):
    item = add_item(session, "Nova | Hyper Beast (Minimal Wear)")
    tx = record_transaction(session, item.id, TransactionType.BUY, Decimal("1"), Decimal("50"))

    update_transaction(
        session,
        tx.id,
        item_id=item.id,
        transaction_type=TransactionType.BUY,
        quantity=Decimal("2"),
        unit_price=Decimal("60"),
        occurred_at=tx.occurred_at,
        notes="fixed",
    )

    audit = session.query(TransactionAuditLog).one()
    assert audit.transaction_id == tx.id
    assert audit.action == "UPDATE"
    assert Decimal(audit.before_data["quantity"]) == Decimal("1")
    assert Decimal(audit.after_data["quantity"]) == Decimal("2")
    assert audit.after_data["notes"] == "fixed"


def test_audit_log_records_delete_before_data(session):
    item = add_item(session, "SSG 08 | Dragonfire (Factory New)")
    tx = record_transaction(session, item.id, TransactionType.BUY, Decimal("1"), Decimal("70"))

    delete_transaction(session, tx.id)

    audit = session.query(TransactionAuditLog).one()
    assert audit.transaction_id == tx.id
    assert audit.action == "DELETE"
    assert Decimal(audit.before_data["unit_price"]) == Decimal("70")
    assert audit.after_data is None


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=timezone.utc)
