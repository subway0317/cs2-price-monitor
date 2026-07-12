from __future__ import annotations

from decimal import Decimal

from cs2_price_monitor.models import TransactionType
from cs2_price_monitor.services.items import add_item
from cs2_price_monitor.services.transactions import record_transaction


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

