from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from cs2_price_monitor.models import Alert, DailyPriceSummary, Holding, Item, ManualHolding, PriceSnapshot, Transaction


@dataclass(frozen=True)
class DeleteItemResult:
    """Counts of rows deleted with an item."""

    item_id: int
    market_name: str
    price_snapshots: int
    daily_price_summaries: int
    transactions: int
    holdings: int
    manual_holdings: int
    alerts: int


def add_item(
    session: Session,
    market_name: str,
    buff_name: str | None = None,
    category: str | None = None,
) -> Item:
    """Add a new item and reject duplicate market names."""
    normalized_name = market_name.strip()
    if not normalized_name:
        raise ValueError("market_hash_name is required")

    existing = session.scalar(select(Item).where(Item.market_name == normalized_name))
    if existing:
        raise ValueError("market_hash_name already exists")

    item = Item(market_name=normalized_name, buff_name=buff_name, category=category, is_active=True)
    session.add(item)
    session.flush()
    return item


def get_or_create_item(
    session: Session,
    market_name: str,
    buff_name: str | None = None,
    category: str | None = None,
) -> Item:
    """Find existing item or create it for imports."""
    normalized_name = market_name.strip()
    existing = session.scalar(select(Item).where(Item.market_name == normalized_name))
    if existing:
        return existing
    return add_item(session, normalized_name, buff_name, category)


def list_enabled_items(session: Session) -> list[Item]:
    """List enabled items for data entry pages."""
    return list(
        session.scalars(
            select(Item).where(Item.is_active.is_(True)).order_by(Item.market_name)
        ).all()
    )


def list_items(session: Session) -> list[Item]:
    """List all items."""
    return list(session.scalars(select(Item).order_by(Item.market_name)).all())


def update_item_metadata(
    session: Session,
    item_id: int,
    buff_name: str | None,
    category: str | None,
) -> Item:
    """Update Chinese name and category."""
    item = session.get(Item, item_id)
    if item is None:
        raise ValueError(f"Item {item_id} not found")
    item.buff_name = buff_name or None
    item.category = category or None
    session.flush()
    return item


def set_item_active(session: Session, item_id: int, is_active: bool) -> Item:
    """Enable or disable monitoring."""
    item = session.get(Item, item_id)
    if item is None:
        raise ValueError(f"Item {item_id} not found")
    item.is_active = is_active
    session.flush()
    return item


def delete_item_with_related_data(session: Session, item_id: int) -> DeleteItemResult:
    """Delete one item and all rows that reference it."""
    item = session.get(Item, item_id)
    if item is None:
        raise ValueError(f"Item {item_id} not found")

    result = DeleteItemResult(
        item_id=item.id,
        market_name=item.market_name,
        price_snapshots=_delete_by_item_id(session, PriceSnapshot, item.id),
        daily_price_summaries=_delete_by_item_id(session, DailyPriceSummary, item.id),
        transactions=_delete_by_item_id(session, Transaction, item.id),
        holdings=_delete_by_item_id(session, Holding, item.id),
        manual_holdings=_delete_by_item_id(session, ManualHolding, item.id),
        alerts=_delete_by_item_id(session, Alert, item.id),
    )
    session.delete(item)
    session.flush()
    return result


def _delete_by_item_id(session: Session, model, item_id: int) -> int:
    """Delete rows for one item_id and return the affected row count."""
    rowcount = session.execute(delete(model).where(model.item_id == item_id)).rowcount
    return int(rowcount or 0)
