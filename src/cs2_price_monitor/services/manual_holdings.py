from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2_price_monitor.catalog import ItemInfo
from cs2_price_monitor.models import Item, ManualHolding, TransactionType, utc_now
from cs2_price_monitor.services.items import get_or_create_item
from cs2_price_monitor.services.transactions import record_transaction


@dataclass(frozen=True)
class ManualHoldingInput:
    """Input for an opening inventory row."""

    item_id: int
    quantity: Decimal
    catalog_id: str | None = None
    version: str | None = None
    unit_cost: Decimal | None = None
    acquired_at: datetime | None = None
    source: str = "steam_manual"
    note: str | None = None


def create_manual_holding(session: Session, data: ManualHoldingInput) -> ManualHolding:
    """Create an opening inventory row without creating a transaction."""
    _validate_manual_holding(data.quantity, data.unit_cost)
    holding = ManualHolding(
        item_id=data.item_id,
        catalog_id=_clean_optional(data.catalog_id),
        version=_clean_optional(data.version),
        quantity=data.quantity,
        unit_cost=data.unit_cost,
        acquired_at=data.acquired_at,
        source=(data.source or "steam_manual").strip() or "steam_manual",
        note=_clean_optional(data.note),
        is_active=True,
    )
    session.add(holding)
    session.flush()
    return holding


def create_manual_holding_from_catalog(
    session: Session,
    catalog_item: ItemInfo,
    quantity: Decimal,
    unit_cost: Decimal | None = None,
    acquired_at: datetime | None = None,
    source: str = "steam_manual",
    note: str | None = None,
    add_to_monitoring: bool = False,
) -> ManualHolding:
    """Create an opening inventory row and optionally enable monitoring."""
    existing_item = session.scalar(
        select(Item).where(Item.market_name == catalog_item.market_hash_name.strip())
    )
    item = get_or_create_item(
        session,
        catalog_item.market_hash_name,
        catalog_item.display_name_cn or None,
        catalog_item.item_type or None,
    )
    if existing_item is None and not add_to_monitoring:
        item.is_active = False
    elif add_to_monitoring and not item.is_active:
        item.is_active = True
    return create_manual_holding(
        session,
        ManualHoldingInput(
            item_id=item.id,
            catalog_id=catalog_item.catalog_id or None,
            version=catalog_item.version or None,
            quantity=quantity,
            unit_cost=unit_cost,
            acquired_at=acquired_at,
            source=source,
            note=note,
        ),
    )


def list_manual_holdings(session: Session, *, include_inactive: bool = False) -> list[ManualHolding]:
    """List manual opening inventory rows."""
    query = select(ManualHolding).order_by(ManualHolding.is_active.desc(), ManualHolding.id.desc())
    if not include_inactive:
        query = query.where(ManualHolding.is_active.is_(True))
    return list(session.scalars(query).all())


def update_manual_holding(
    session: Session,
    holding_id: int,
    *,
    quantity: Decimal,
    unit_cost: Decimal | None,
    acquired_at: datetime | None,
    source: str,
    note: str | None,
) -> ManualHolding:
    """Update an opening inventory row without touching transactions."""
    _validate_manual_holding(quantity, unit_cost)
    holding = _get_manual_holding(session, holding_id)
    holding.quantity = quantity
    holding.unit_cost = unit_cost
    holding.acquired_at = acquired_at
    holding.source = (source or "steam_manual").strip() or "steam_manual"
    holding.note = _clean_optional(note)
    holding.updated_at = utc_now()
    session.flush()
    return holding


def set_manual_holding_active(session: Session, holding_id: int, is_active: bool) -> ManualHolding:
    """Soft-delete or restore an opening inventory row."""
    holding = _get_manual_holding(session, holding_id)
    holding.is_active = is_active
    holding.updated_at = utc_now()
    session.flush()
    return holding


def convert_manual_holding_to_buy_transaction(
    session: Session,
    holding_id: int,
    *,
    quantity: Decimal,
    unit_price: Decimal,
    occurred_at: datetime,
    fee: Decimal = Decimal("0"),
    notes: str | None = None,
) -> ManualHolding:
    """Convert part or all of a manual row to a real historical buy transaction."""
    if quantity <= 0:
        raise ValueError("Quantity must be positive")
    holding = _get_manual_holding(session, holding_id)
    if not holding.is_active:
        raise ValueError("Manual holding is inactive")
    if quantity > Decimal(holding.quantity):
        raise ValueError("Cannot convert more than the manual holding quantity")
    record_transaction(
        session,
        holding.item_id,
        TransactionType.BUY,
        quantity,
        unit_price,
        fee=fee,
        occurred_at=occurred_at,
        notes=notes,
    )
    remaining = Decimal(holding.quantity) - quantity
    if remaining == 0:
        holding.is_active = False
    else:
        holding.quantity = remaining
    holding.updated_at = utc_now()
    session.flush()
    return holding


def _get_manual_holding(session: Session, holding_id: int) -> ManualHolding:
    holding = session.get(ManualHolding, holding_id)
    if holding is None:
        raise ValueError(f"Manual holding {holding_id} not found")
    return holding


def _validate_manual_holding(quantity: Decimal, unit_cost: Decimal | None) -> None:
    if quantity <= 0:
        raise ValueError("Quantity must be positive")
    if unit_cost is not None and unit_cost < 0:
        raise ValueError("Unit cost must be non-negative")


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
