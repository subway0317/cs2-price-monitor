from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from statistics import median

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from cs2_price_monitor.config import load_settings
from cs2_price_monitor.models import Alert, DailyPriceSummary, PriceSnapshot
from cs2_price_monitor.providers.base import PriceQuote


def record_price_quote(session: Session, quote: PriceQuote) -> PriceSnapshot:
    """Persist one independent price history row."""
    previous = session.scalar(
        select(PriceSnapshot)
        .where(PriceSnapshot.item_id == quote.item_id)
        .where(PriceSnapshot.currency == quote.currency)
        .where(PriceSnapshot.is_suspicious.is_(False))
        .where(PriceSnapshot.lowest_listing_price.is_not(None))
        .order_by(PriceSnapshot.captured_at.desc(), PriceSnapshot.id.desc())
        .limit(1)
    )
    is_suspicious, reason = _detect_suspicious_price(quote, previous)

    snapshot = PriceSnapshot(
        item_id=quote.item_id,
        provider=quote.provider,
        lowest_listing_price=quote.lowest_listing_price,
        highest_buy_order_price=quote.highest_buy_order_price,
        listing_count=quote.listing_count,
        currency=quote.currency,
        captured_at=quote.captured_at,
        is_suspicious=is_suspicious,
        suspicion_reason=reason,
        raw_payload=json.dumps(quote.raw_payload, ensure_ascii=False),
    )
    session.add(snapshot)
    if is_suspicious:
        session.add(
            Alert(
                item_id=quote.item_id,
                severity="warning",
                message=f"Suspicious price snapshot: {reason}",
            )
        )
    session.flush()
    return snapshot


def _detect_suspicious_price(
    quote: PriceQuote, previous: PriceSnapshot | None
) -> tuple[bool, str | None]:
    """Compare against the previous normal listing price."""
    if previous is None:
        return False, None
    if quote.lowest_listing_price is None or previous.lowest_listing_price is None:
        return False, None
    if quote.currency != previous.currency:
        return False, None
    previous_price = Decimal(previous.lowest_listing_price)
    current_price = quote.lowest_listing_price
    if previous_price <= 0:
        return False, None
    settings = load_settings()
    if current_price > previous_price * settings.pricing.suspicious_high_multiplier:
        return True, "price is more than 3x previous value"
    if current_price < previous_price * settings.pricing.suspicious_low_multiplier:
        return True, "price is lower than one third of previous value"
    return False, None


def regenerate_daily_summaries(session: Session) -> list[DailyPriceSummary]:
    """Regenerate daily OHLC and median summaries from normal snapshots."""
    snapshots = session.scalars(
        select(PriceSnapshot)
        .where(PriceSnapshot.is_suspicious.is_(False))
        .where(PriceSnapshot.lowest_listing_price.is_not(None))
        .order_by(PriceSnapshot.item_id, PriceSnapshot.captured_at)
    ).all()
    grouped: dict[tuple[int, date, str], list[PriceSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        grouped[(snapshot.item_id, snapshot.captured_at.date(), snapshot.currency)].append(snapshot)

    session.execute(delete(DailyPriceSummary))
    summaries: list[DailyPriceSummary] = []
    for (item_id, summary_date, currency), rows in grouped.items():
        prices = [Decimal(row.lowest_listing_price) for row in rows]
        summary = DailyPriceSummary(
            item_id=item_id,
            summary_date=summary_date,
            currency=currency,
            open_price=prices[0],
            high_price=max(prices),
            low_price=min(prices),
            close_price=prices[-1],
            median_price=Decimal(str(median(prices))),
            snapshot_count=len(rows),
        )
        session.add(summary)
        summaries.append(summary)
    session.flush()
    return summaries
