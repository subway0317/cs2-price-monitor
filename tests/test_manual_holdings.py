from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from cs2_price_monitor.catalog import ItemInfo
from cs2_price_monitor.models import Item, ManualHolding, Transaction, TransactionType
from cs2_price_monitor.providers.manual import ManualPriceProvider
from cs2_price_monitor.services.analytics import calculate_portfolio
from cs2_price_monitor.services.items import add_item
from cs2_price_monitor.services.manual_holdings import (
    ManualHoldingInput,
    create_manual_holding,
    create_manual_holding_from_catalog,
    set_manual_holding_active,
    update_manual_holding,
)
from cs2_price_monitor.services.prices import record_price_quote
from cs2_price_monitor.services.transactions import record_transaction


def test_can_record_opening_holding_without_cost_and_without_buy_transaction(session):
    item = add_item(session, "AK-47 | Redline (Field-Tested)")

    create_manual_holding(
        session,
        ManualHoldingInput(item_id=item.id, quantity=Decimal("2"), unit_cost=None),
    )

    assert session.scalar(select(Transaction)) is None
    stored = session.scalar(select(ManualHolding))
    assert stored is not None
    assert stored.unit_cost is None


def test_unknown_cost_is_not_treated_as_zero_cost(session):
    item = add_item(session, "AK-47 | Redline (Field-Tested)")
    create_manual_holding(
        session,
        ManualHoldingInput(item_id=item.id, quantity=Decimal("2"), unit_cost=None),
    )
    record_price_quote(session, ManualPriceProvider(Decimal("100"), Decimal("90"), 3).get_quote(item.id))

    rows = calculate_portfolio(session)

    assert rows[0].listing_value == Decimal("200.000000000000")
    assert rows[0].known_cost is None
    assert rows[0].avg_cost is None
    assert rows[0].unrealized_profit is None
    assert rows[0].unknown_cost_quantity == Decimal("2.000000")
    assert rows[0].non_calculable_value == Decimal("200.000000000000")


def test_existing_item_is_reused_when_adding_manual_holding_from_catalog(session):
    item = add_item(session, "Operation Breakout Weapon Case")
    catalog_item = _catalog_item("Operation Breakout Weapon Case", catalog_id="case-breakout")

    create_manual_holding_from_catalog(
        session,
        catalog_item,
        quantity=Decimal("1"),
        add_to_monitoring=True,
    )

    items = session.scalars(select(Item).where(Item.market_name == item.market_name)).all()
    assert len(items) == 1


def test_catalog_manual_holding_can_create_monitored_item(session):
    catalog_item = _catalog_item("Operation Phoenix Weapon Case", catalog_id="case-phoenix")

    holding = create_manual_holding_from_catalog(
        session,
        catalog_item,
        quantity=Decimal("1"),
        add_to_monitoring=True,
    )

    item = session.get(Item, holding.item_id)
    assert item is not None
    assert item.market_name == "Operation Phoenix Weapon Case"
    assert item.is_active is True


def test_catalog_manual_holding_without_monitoring_creates_inactive_item(session):
    catalog_item = _catalog_item("Chroma Case", catalog_id="case-chroma")

    holding = create_manual_holding_from_catalog(
        session,
        catalog_item,
        quantity=Decimal("1"),
        add_to_monitoring=False,
    )

    item = session.get(Item, holding.item_id)
    assert item is not None
    assert item.is_active is False


def test_same_special_version_is_identified_together_in_portfolio(session):
    catalog_item = _catalog_item(
        "★ Bayonet | Doppler (Factory New)",
        catalog_id="bayonet-doppler-phase-2",
        version="Phase 2",
    )
    first = create_manual_holding_from_catalog(session, catalog_item, quantity=Decimal("1"))
    create_manual_holding_from_catalog(session, catalog_item, quantity=Decimal("2"))
    record_price_quote(session, ManualPriceProvider(Decimal("1000"), Decimal("950"), 1).get_quote(first.item_id))

    rows = calculate_portfolio(session)

    assert len(rows) == 1
    assert rows[0].catalog_id == "bayonet-doppler-phase-2"
    assert rows[0].version == "Phase 2"
    assert rows[0].manual_quantity == Decimal("3.000000")


def test_different_special_versions_are_not_merged(session):
    phase_1 = _catalog_item(
        "★ Bayonet | Doppler (Factory New)",
        catalog_id="bayonet-doppler-phase-1",
        version="Phase 1",
    )
    phase_2 = _catalog_item(
        "★ Bayonet | Doppler (Factory New)",
        catalog_id="bayonet-doppler-phase-2",
        version="Phase 2",
    )
    holding = create_manual_holding_from_catalog(session, phase_1, quantity=Decimal("1"))
    create_manual_holding_from_catalog(session, phase_2, quantity=Decimal("1"))
    record_price_quote(session, ManualPriceProvider(Decimal("1000"), Decimal("950"), 1).get_quote(holding.item_id))

    rows = sorted(calculate_portfolio(session), key=lambda row: row.version or "")

    assert [row.version for row in rows] == ["Phase 1", "Phase 2"]
    assert [row.manual_quantity for row in rows] == [Decimal("1.000000"), Decimal("1.000000")]


def test_manual_and_transaction_holdings_are_summed_for_base_position(session):
    item = add_item(session, "M4A1-S | Printstream (Field-Tested)")
    record_transaction(session, item.id, TransactionType.BUY, Decimal("2"), Decimal("100"))
    create_manual_holding(
        session,
        ManualHoldingInput(item_id=item.id, quantity=Decimal("3"), unit_cost=Decimal("80")),
    )
    record_price_quote(session, ManualPriceProvider(Decimal("120"), Decimal("110"), 2).get_quote(item.id))

    rows = calculate_portfolio(session)

    assert len(rows) == 1
    assert rows[0].transaction_quantity == Decimal("2.000000")
    assert rows[0].manual_quantity == Decimal("3.000000")
    assert rows[0].quantity == Decimal("5.000000")
    assert rows[0].known_cost == Decimal("440.000000000000")


def test_inactive_manual_holding_is_excluded_from_portfolio(session):
    item = add_item(session, "USP-S | Orion (Factory New)")
    holding = create_manual_holding(
        session,
        ManualHoldingInput(item_id=item.id, quantity=Decimal("1"), unit_cost=Decimal("100")),
    )
    record_price_quote(session, ManualPriceProvider(Decimal("120"), Decimal("110"), 2).get_quote(item.id))

    set_manual_holding_active(session, holding.id, False)

    assert calculate_portfolio(session) == []


def test_updating_manual_holding_does_not_modify_transactions(session):
    item = add_item(session, "Desert Eagle | Blaze (Factory New)")
    holding = create_manual_holding(
        session,
        ManualHoldingInput(item_id=item.id, quantity=Decimal("1"), unit_cost=None),
    )

    update_manual_holding(
        session,
        holding.id,
        quantity=Decimal("2"),
        unit_cost=Decimal("500"),
        acquired_at=datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
        source="steam_manual",
        note="filled later",
    )

    assert session.scalar(select(Transaction)) is None
    assert holding.quantity == Decimal("2")
    assert holding.unit_cost == Decimal("500")
    assert holding.note == "filled later"


def _catalog_item(market_hash_name: str, *, catalog_id: str, version: str = "") -> ItemInfo:
    return ItemInfo(
        catalog_id=catalog_id,
        display_name_cn="",
        display_name_en=market_hash_name,
        market_hash_name=market_hash_name,
        version=version,
        item_type="Skin",
        aliases=(),
        price_supported=True,
    )
