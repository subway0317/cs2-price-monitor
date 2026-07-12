from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cs2_price_monitor.models import TransactionType
from cs2_price_monitor.providers.manual import ManualPriceProvider
from cs2_price_monitor.services.analytics import calculate_item_analysis, calculate_portfolio
from cs2_price_monitor.services.items import add_item
from cs2_price_monitor.services.prices import record_price_quote, regenerate_daily_summaries
from cs2_price_monitor.services.transactions import record_transaction


def test_calculates_portfolio_values(session):
    item = add_item(session, "Desert Eagle | Blaze (Factory New)")
    record_transaction(session, item.id, TransactionType.BUY, Decimal("2"), Decimal("500"))
    record_price_quote(
        session,
        ManualPriceProvider(Decimal("650"), Decimal("620"), 3).get_quote(item.id),
    )
    regenerate_daily_summaries(session)

    rows = calculate_portfolio(session)

    assert len(rows) == 1
    assert rows[0].listing_value == Decimal("1300.000000000000")
    assert rows[0].quick_sell_value == Decimal("1240.000000000000")
    assert rows[0].unrealized_profit == Decimal("300.000000000000")


def test_portfolio_uses_latest_normal_snapshot(session):
    item = add_item(session, "Glock-18 | Fade (Factory New)")
    record_transaction(session, item.id, TransactionType.BUY, Decimal("1"), Decimal("100"))
    record_price_quote(
        session,
        ManualPriceProvider(Decimal("200"), Decimal("190"), 3).get_quote(item.id),
    )
    suspicious = record_price_quote(
        session,
        ManualPriceProvider(Decimal("1000"), Decimal("900"), 3).get_quote(item.id),
    )

    rows = calculate_portfolio(session)

    assert suspicious.is_suspicious is True
    assert rows[0].listing_price == Decimal("200.000000")


def test_item_analysis_uses_normal_snapshots_for_changes_and_valuation(session):
    item = add_item(session, "AK-47 | Redline (Field-Tested)")
    latest_time = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    snapshots = [
        (latest_time - timedelta(days=30), "100", "90", False),
        (latest_time - timedelta(days=7, minutes=5), "110", "100", False),
        (latest_time - timedelta(days=7), "999", "1", True),
        (latest_time - timedelta(hours=24), "120", "112", False),
        (latest_time, "132", "120", False),
    ]
    for captured_at, lowest, highest_buy, suspicious in snapshots:
        snapshot = record_price_quote(
            session,
            ManualPriceProvider(
                Decimal(lowest),
                Decimal(highest_buy),
                10,
                captured_at=captured_at,
            ).get_quote(item.id),
        )
        snapshot.is_suspicious = suspicious

    record_transaction(session, item.id, TransactionType.BUY, Decimal("2"), Decimal("100"))
    regenerate_daily_summaries(session)

    analysis = calculate_item_analysis(session, item.id)

    assert analysis.current_lowest_listing_price == Decimal("132.000000")
    assert analysis.current_highest_buy_order_price == Decimal("120.000000")
    assert analysis.change_24h == Decimal("0.1")
    assert analysis.change_7d == Decimal("0.2")
    assert analysis.change_30d == Decimal("0.32")
    assert analysis.bid_ask_spread_rate == Decimal("0.09090909090909090909090909091")
    assert len(analysis.price_points) == 4
    assert len(analysis.suspicious_points) == 1
    assert analysis.price_points[-1].ma_7d == Decimal("126.000000")
    assert analysis.holding is not None
    assert analysis.holding.listing_value == Decimal("264.000000000000")
    assert analysis.holding.quick_sell_value == Decimal("240.000000000000")
    assert analysis.holding.unrealized_profit == Decimal("40.000000000000")
    assert analysis.holding.unrealized_profit_rate == Decimal("0.2")


def test_item_analysis_returns_none_for_insufficient_change_data(session):
    item = add_item(session, "M4A1-S | Printstream (Field-Tested)")
    record_price_quote(
        session,
        ManualPriceProvider(Decimal("200"), Decimal("190"), 2).get_quote(item.id),
    )

    analysis = calculate_item_analysis(session, item.id)

    assert analysis.change_24h is None
    assert analysis.change_7d is None
    assert analysis.change_30d is None


def test_different_currency_does_not_calculate_changes_or_profit(session):
    item = add_item(session, "USP-S | Orion (Factory New)")
    earlier = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    latest = earlier + timedelta(days=1)
    record_transaction(session, item.id, TransactionType.BUY, Decimal("1"), Decimal("100"))
    record_price_quote(
        session,
        ManualPriceProvider(
            Decimal("100"),
            Decimal("95"),
            3,
            currency="CNY",
            captured_at=earlier,
        ).get_quote(item.id),
    )
    record_price_quote(
        session,
        ManualPriceProvider(
            Decimal("20"),
            Decimal("18"),
            3,
            currency="USD",
            captured_at=latest,
        ).get_quote(item.id),
    )
    regenerate_daily_summaries(session)

    rows = calculate_portfolio(session)
    analysis = calculate_item_analysis(session, item.id)

    assert rows[0].currency == "USD"
    assert rows[0].unrealized_profit is None
    assert rows[0].change_24h is None
    assert analysis.change_24h is None
    assert analysis.holding is not None
    assert analysis.holding.unrealized_profit is None
