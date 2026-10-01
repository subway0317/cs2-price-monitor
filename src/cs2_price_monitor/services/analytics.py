from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2_price_monitor.models import DailyPriceSummary, Holding, Item, ManualHolding, PriceSnapshot


@dataclass(frozen=True)
class PortfolioValuation:
    item_id: int
    market_name: str
    catalog_id: str | None
    version: str | None
    transaction_quantity: Decimal
    manual_quantity: Decimal
    quantity: Decimal
    avg_cost: Decimal | None
    known_cost: Decimal | None
    unknown_cost_quantity: Decimal
    currency: str
    listing_price: Decimal
    buy_order_price: Decimal
    listing_value: Decimal
    quick_sell_value: Decimal
    unrealized_profit: Decimal | None
    non_calculable_value: Decimal
    change_24h: Decimal | None
    change_7d: Decimal | None
    change_30d: Decimal | None


@dataclass(frozen=True)
class PricePoint:
    captured_at: datetime
    lowest_listing_price: Decimal | None
    highest_buy_order_price: Decimal | None
    listing_count: int | None
    currency: str
    is_suspicious: bool
    ma_7d: Decimal | None
    ma_30d: Decimal | None


@dataclass(frozen=True)
class HoldingAnalysis:
    quantity: Decimal
    avg_cost: Decimal
    listing_value: Decimal
    quick_sell_value: Decimal
    unrealized_profit: Decimal | None
    unrealized_profit_rate: Decimal | None


@dataclass(frozen=True)
class DailySummaryRow:
    summary_date: date
    currency: str
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    median_price: Decimal
    snapshot_count: int


@dataclass(frozen=True)
class ItemAnalysis:
    item_id: int
    market_name: str
    current_lowest_listing_price: Decimal | None
    current_highest_buy_order_price: Decimal | None
    change_24h: Decimal | None
    change_7d: Decimal | None
    change_30d: Decimal | None
    bid_ask_spread_rate: Decimal | None
    holding: HoldingAnalysis | None
    price_points: list[PricePoint]
    suspicious_points: list[PricePoint]
    daily_summaries: list[DailySummaryRow]


def calculate_portfolio(session: Session) -> list[PortfolioValuation]:
    """Calculate valuation and returns."""
    positions = _portfolio_positions(session)
    results: list[PortfolioValuation] = []
    for position in positions:
        item_id = int(position["item_id"])
        item = session.get(Item, item_id)
        latest = session.scalar(
            select(PriceSnapshot)
            .where(PriceSnapshot.item_id == item_id)
            .where(PriceSnapshot.is_suspicious.is_(False))
            .where(PriceSnapshot.lowest_listing_price.is_not(None))
            .where(PriceSnapshot.highest_buy_order_price.is_not(None))
            .order_by(PriceSnapshot.captured_at.desc(), PriceSnapshot.id.desc())
            .limit(1)
        )
        if item is None or latest is None:
            continue
        quantity = Decimal(position["quantity"])
        transaction_quantity = Decimal(position["transaction_quantity"])
        manual_quantity = Decimal(position["manual_quantity"])
        known_cost = position["known_cost"]
        unknown_cost_quantity = Decimal(position["unknown_cost_quantity"])
        known_quantity = quantity - unknown_cost_quantity
        avg_cost = known_cost / known_quantity if known_cost is not None and known_quantity > 0 else None
        listing_price = Decimal(latest.lowest_listing_price)
        buy_order_price = Decimal(latest.highest_buy_order_price)
        latest_date = _as_utc(latest.captured_at).date()
        unrealized_profit = None
        if known_cost is not None and latest.currency == "CNY":
            unrealized_profit = known_quantity * listing_price - known_cost
        non_calculable_value = unknown_cost_quantity * listing_price
        results.append(
            PortfolioValuation(
                item_id=item_id,
                market_name=item.market_name,
                catalog_id=position["catalog_id"],
                version=position["version"],
                transaction_quantity=transaction_quantity,
                manual_quantity=manual_quantity,
                quantity=quantity,
                avg_cost=avg_cost,
                known_cost=known_cost,
                unknown_cost_quantity=unknown_cost_quantity,
                currency=latest.currency,
                listing_price=listing_price,
                buy_order_price=buy_order_price,
                listing_value=quantity * listing_price,
                quick_sell_value=quantity * buy_order_price,
                unrealized_profit=unrealized_profit,
                non_calculable_value=non_calculable_value,
                change_24h=_change_since(
                    session,
                    item_id,
                    latest_date - timedelta(days=1),
                    listing_price,
                    latest.currency,
                ),
                change_7d=_change_since(
                    session,
                    item_id,
                    latest_date - timedelta(days=7),
                    listing_price,
                    latest.currency,
                ),
                change_30d=_change_since(
                    session,
                    item_id,
                    latest_date - timedelta(days=30),
                    listing_price,
                    latest.currency,
                ),
            )
        )
    return results


def _portfolio_positions(session: Session) -> list[dict[str, object]]:
    """Return transaction and manual quantities grouped by valuation identity."""
    positions: dict[tuple[int, str | None, str | None], dict[str, object]] = {}
    holdings = session.scalars(select(Holding).where(Holding.quantity > 0)).all()
    for holding in holdings:
        key = (holding.item_id, None, None)
        quantity = Decimal(holding.quantity)
        avg_cost = Decimal(holding.avg_cost)
        positions[key] = {
            "item_id": holding.item_id,
            "catalog_id": None,
            "version": None,
            "transaction_quantity": quantity,
            "manual_quantity": Decimal("0"),
            "quantity": quantity,
            "known_cost": quantity * avg_cost,
            "unknown_cost_quantity": Decimal("0"),
        }

    manual_rows = session.scalars(
        select(ManualHolding).where(ManualHolding.is_active.is_(True))
    ).all()
    for manual in manual_rows:
        key = _manual_portfolio_key(manual)
        position = positions.setdefault(
            key,
            {
                "item_id": manual.item_id,
                "catalog_id": key[1],
                "version": key[2],
                "transaction_quantity": Decimal("0"),
                "manual_quantity": Decimal("0"),
                "quantity": Decimal("0"),
                "known_cost": None,
                "unknown_cost_quantity": Decimal("0"),
            },
        )
        quantity = Decimal(manual.quantity)
        position["manual_quantity"] = Decimal(position["manual_quantity"]) + quantity
        position["quantity"] = Decimal(position["quantity"]) + quantity
        if manual.unit_cost is None:
            position["unknown_cost_quantity"] = Decimal(position["unknown_cost_quantity"]) + quantity
        else:
            current_known_cost = position["known_cost"]
            position["known_cost"] = (current_known_cost or Decimal("0")) + quantity * Decimal(
                manual.unit_cost
            )

    return [
        position
        for position in positions.values()
        if Decimal(position["quantity"]) > 0
    ]


def _manual_portfolio_key(manual: ManualHolding) -> tuple[int, str | None, str | None]:
    """Keep versioned catalog holdings separate from market-name-only rows."""
    version = (manual.version or "").strip()
    if version:
        return (manual.item_id, manual.catalog_id, version)
    return (manual.item_id, None, None)


def _change_since(
    session: Session, item_id: int, since_date: date, current_price: Decimal, currency: str
) -> Decimal | None:
    summary = session.scalar(
        select(DailyPriceSummary)
        .where(
            DailyPriceSummary.item_id == item_id,
            DailyPriceSummary.summary_date <= since_date,
            DailyPriceSummary.currency == currency,
        )
        .order_by(DailyPriceSummary.summary_date.desc())
        .limit(1)
    )
    if summary is None or Decimal(summary.close_price) == 0:
        return None
    return (current_price - Decimal(summary.close_price)) / Decimal(summary.close_price)


def portfolio_dataframe(rows: list[PortfolioValuation]) -> pd.DataFrame:
    """Convert valuations to DataFrame."""
    return pd.DataFrame([row.__dict__ for row in rows])


def calculate_item_analysis(session: Session, item_id: int, max_snapshots: int | None = None) -> ItemAnalysis:
    """Build item analysis data for the Streamlit page."""
    item = session.get(Item, item_id)
    if item is None:
        raise ValueError(f"Item {item_id} not found")

    snapshot_query = (
        select(PriceSnapshot)
        .where(PriceSnapshot.item_id == item_id)
        .order_by(PriceSnapshot.captured_at.desc(), PriceSnapshot.id.desc())
    )
    if max_snapshots is not None:
        snapshot_query = snapshot_query.limit(max_snapshots)
    snapshots = sorted(
        session.scalars(snapshot_query).all(),
        key=lambda snapshot: (_as_utc(snapshot.captured_at), snapshot.id),
    )
    normal_snapshots = [
        snapshot
        for snapshot in snapshots
        if not snapshot.is_suspicious and snapshot.lowest_listing_price is not None
    ]
    price_points = _build_price_points(normal_snapshots)
    suspicious_points = _build_price_points([snapshot for snapshot in snapshots if snapshot.is_suspicious])
    latest = normal_snapshots[-1] if normal_snapshots else None
    holding = session.scalar(select(Holding).where(Holding.item_id == item_id))

    holding_analysis = None
    if (
        latest is not None
        and holding is not None
        and Decimal(holding.quantity) > 0
        and latest.highest_buy_order_price is not None
    ):
        quantity = Decimal(holding.quantity)
        avg_cost = Decimal(holding.avg_cost)
        lowest_price = Decimal(latest.lowest_listing_price)
        buy_order_price = Decimal(latest.highest_buy_order_price)
        quick_sell_value = quantity * buy_order_price
        remaining_cost = quantity * avg_cost
        unrealized_profit = quick_sell_value - remaining_cost if latest.currency == "CNY" else None
        holding_analysis = HoldingAnalysis(
            quantity=quantity,
            avg_cost=avg_cost,
            listing_value=quantity * lowest_price,
            quick_sell_value=quick_sell_value,
            unrealized_profit=unrealized_profit,
            unrealized_profit_rate=(
                unrealized_profit / remaining_cost
                if unrealized_profit is not None and remaining_cost != Decimal("0")
                else None
            ),
        )

    return ItemAnalysis(
        item_id=item.id,
        market_name=item.market_name,
        current_lowest_listing_price=Decimal(latest.lowest_listing_price) if latest else None,
        current_highest_buy_order_price=(
            Decimal(latest.highest_buy_order_price)
            if latest and latest.highest_buy_order_price is not None
            else None
        ),
        change_24h=_snapshot_change(normal_snapshots, timedelta(hours=24)),
        change_7d=_snapshot_change(normal_snapshots, timedelta(days=7)),
        change_30d=_snapshot_change(normal_snapshots, timedelta(days=30)),
        bid_ask_spread_rate=_bid_ask_spread_rate(latest),
        holding=holding_analysis,
        price_points=price_points,
        suspicious_points=suspicious_points,
        daily_summaries=_daily_summary_rows(session, item_id),
    )


def _snapshot_change(snapshots: list[PriceSnapshot], delta: timedelta) -> Decimal | None:
    if len(snapshots) < 2:
        return None
    latest = snapshots[-1]
    target = _as_utc(latest.captured_at) - delta
    baseline = _nearest_snapshot(snapshots[:-1], target)
    if baseline is None:
        return None
    if latest.lowest_listing_price is None or baseline.lowest_listing_price is None:
        return None
    if latest.currency != baseline.currency:
        return None
    baseline_price = Decimal(baseline.lowest_listing_price)
    if baseline_price == Decimal("0"):
        return None
    return (Decimal(latest.lowest_listing_price) - baseline_price) / baseline_price


def _nearest_snapshot(snapshots: list[PriceSnapshot], target: datetime) -> PriceSnapshot | None:
    if not snapshots:
        return None
    before_or_at = [snapshot for snapshot in snapshots if _as_utc(snapshot.captured_at) <= target]
    if before_or_at:
        return min(before_or_at, key=lambda snapshot: abs(_as_utc(snapshot.captured_at) - target))
    return min(snapshots, key=lambda snapshot: abs(_as_utc(snapshot.captured_at) - target))


def _bid_ask_spread_rate(snapshot: PriceSnapshot | None) -> Decimal | None:
    if snapshot is None:
        return None
    if snapshot.lowest_listing_price is None or snapshot.highest_buy_order_price is None:
        return None
    lowest_price = Decimal(snapshot.lowest_listing_price)
    if lowest_price == Decimal("0"):
        return None
    return (lowest_price - Decimal(snapshot.highest_buy_order_price)) / lowest_price


def _build_price_points(snapshots: list[PriceSnapshot]) -> list[PricePoint]:
    points: list[PricePoint] = []
    for snapshot in snapshots:
        preceding = snapshots[: len(points) + 1]
        points.append(
            PricePoint(
                captured_at=snapshot.captured_at,
                lowest_listing_price=(
                    Decimal(snapshot.lowest_listing_price)
                    if snapshot.lowest_listing_price is not None
                    else None
                ),
                highest_buy_order_price=(
                    Decimal(snapshot.highest_buy_order_price)
                    if snapshot.highest_buy_order_price is not None
                    else None
                ),
                listing_count=snapshot.listing_count,
                currency=snapshot.currency,
                is_suspicious=snapshot.is_suspicious,
                ma_7d=_moving_average(preceding, snapshot.captured_at, timedelta(days=7)),
                ma_30d=_moving_average(preceding, snapshot.captured_at, timedelta(days=30)),
            )
        )
    return points


def _moving_average(
    snapshots: list[PriceSnapshot], captured_at: datetime, window: timedelta
) -> Decimal | None:
    normalized_captured_at = _as_utc(captured_at)
    window_start = normalized_captured_at - window
    prices = [
        Decimal(snapshot.lowest_listing_price)
        for snapshot in snapshots
        if snapshot.lowest_listing_price is not None
        and window_start <= _as_utc(snapshot.captured_at) <= normalized_captured_at
        and snapshot.currency == snapshots[-1].currency
    ]
    if not prices:
        return None
    return sum(prices, Decimal("0")) / Decimal(len(prices))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _daily_summary_rows(session: Session, item_id: int) -> list[DailySummaryRow]:
    summaries = session.scalars(
        select(DailyPriceSummary)
        .where(DailyPriceSummary.item_id == item_id)
        .order_by(DailyPriceSummary.summary_date)
    ).all()
    return [
        DailySummaryRow(
            summary_date=summary.summary_date,
            currency=summary.currency,
            open_price=Decimal(summary.open_price),
            high_price=Decimal(summary.high_price),
            low_price=Decimal(summary.low_price),
            close_price=Decimal(summary.close_price),
            median_price=Decimal(summary.median_price),
            snapshot_count=summary.snapshot_count,
        )
        for summary in summaries
    ]
