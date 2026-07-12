from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2_price_monitor.models import PriceSnapshot, TransactionType
from cs2_price_monitor.providers.manual import ManualPriceProvider
from cs2_price_monitor.services.items import get_or_create_item
from cs2_price_monitor.services.prices import record_price_quote
from cs2_price_monitor.services.transactions import record_transaction


def import_price_csv(session: Session, path: Path) -> int:
    """Import manual price rows from CSV."""
    frame = pd.read_csv(path)
    count = 0
    for row in frame.to_dict("records"):
        item = get_or_create_item(session, str(row["market_name"]))
        provider = ManualPriceProvider(
            lowest_listing_price=Decimal(str(row["lowest_listing_price"])),
            highest_buy_order_price=Decimal(str(row["highest_buy_order_price"])),
            listing_count=int(row.get("listing_count", 0)),
            currency=str(row.get("currency", "CNY")),
            captured_at=_parse_datetime(row.get("captured_at")),
            raw_payload={"source": "csv"},
        )
        record_price_quote(session, provider.get_quote(item.id))
        count += 1
    return count


def import_transaction_csv(session: Session, path: Path) -> int:
    """Import transaction rows from CSV."""
    frame = pd.read_csv(path)
    count = 0
    for row in frame.to_dict("records"):
        item = get_or_create_item(session, str(row["market_name"]))
        record_transaction(
            session=session,
            item_id=item.id,
            transaction_type=TransactionType(str(row["transaction_type"])),
            quantity=Decimal(str(row["quantity"])),
            unit_price=Decimal(str(row["unit_price"])),
            fee=Decimal(str(row.get("fee", 0))),
            occurred_at=_parse_datetime(row.get("occurred_at")),
            notes=str(row.get("notes", "")) or None,
        )
        count += 1
    return count


def export_price_history(session: Session, path: Path) -> None:
    """Export price history to CSV."""
    rows = session.scalars(select(PriceSnapshot).order_by(PriceSnapshot.captured_at)).all()
    frame = pd.DataFrame(
        [
            {
                "item_id": row.item_id,
                "provider": row.provider,
                "lowest_listing_price": row.lowest_listing_price,
                "highest_buy_order_price": row.highest_buy_order_price,
                "listing_count": row.listing_count,
                "currency": row.currency,
                "captured_at": row.captured_at,
                "is_suspicious": row.is_suspicious,
                "suspicion_reason": row.suspicion_reason,
            }
            for row in rows
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def _parse_datetime(value: object) -> datetime:
    if value is None or str(value) == "nan":
        return datetime.now(timezone.utc)
    parsed = pd.to_datetime(value, utc=True).to_pydatetime()
    return parsed
