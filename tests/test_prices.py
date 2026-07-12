from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from cs2_price_monitor.models import Alert, DailyPriceSummary
from cs2_price_monitor.providers.manual import ManualPriceProvider
from cs2_price_monitor.services.items import add_item
from cs2_price_monitor.services.prices import record_price_quote, regenerate_daily_summaries


def test_records_suspicious_price_and_alert(session):
    item = add_item(session, "AWP | Asiimov (Battle-Scarred)")
    first = ManualPriceProvider(Decimal("100"), Decimal("95"), 20)
    second = ManualPriceProvider(Decimal("350"), Decimal("90"), 18)

    record_price_quote(session, first.get_quote(item.id))
    snapshot = record_price_quote(session, second.get_quote(item.id))

    alert = session.scalar(select(Alert).where(Alert.item_id == item.id))
    assert snapshot.is_suspicious is True
    assert alert is not None


def test_regenerates_daily_ohlc_and_median(session):
    item = add_item(session, "USP-S | Kill Confirmed (Field-Tested)")
    for price in ["10", "13", "12"]:
        provider = ManualPriceProvider(
            Decimal(price),
            Decimal("9"),
            5,
            captured_at=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
        )
        record_price_quote(session, provider.get_quote(item.id))

    summaries = regenerate_daily_summaries(session)
    summary = session.scalar(select(DailyPriceSummary).where(DailyPriceSummary.item_id == item.id))

    assert len(summaries) == 1
    assert summary.open_price == Decimal("10.000000")
    assert summary.high_price == Decimal("13.000000")
    assert summary.low_price == Decimal("10.000000")
    assert summary.close_price == Decimal("12.000000")
    assert summary.median_price == Decimal("12.000000")


def test_daily_summary_excludes_suspicious_snapshots(session):
    item = add_item(session, "M4A4 | The Emperor (Field-Tested)")
    normal = ManualPriceProvider(
        Decimal("100"),
        Decimal("95"),
        5,
        captured_at=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
    )
    suspicious = ManualPriceProvider(
        Decimal("500"),
        Decimal("90"),
        5,
        captured_at=datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc),
    )

    record_price_quote(session, normal.get_quote(item.id))
    snapshot = record_price_quote(session, suspicious.get_quote(item.id))
    summaries = regenerate_daily_summaries(session)

    assert snapshot.is_suspicious is True
    assert len(summaries) == 1
    assert summaries[0].open_price == Decimal("100.000000")
    assert summaries[0].close_price == Decimal("100.000000")
    assert summaries[0].snapshot_count == 1
