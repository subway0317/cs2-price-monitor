from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2_price_monitor.models import Holding, Transaction, TransactionType


def record_transaction(
    session: Session,
    item_id: int,
    transaction_type: TransactionType,
    quantity: Decimal,
    unit_price: Decimal,
    fee: Decimal = Decimal("0"),
    occurred_at: datetime | None = None,
    notes: str | None = None,
) -> Transaction:
    """Record buy or sell and update moving weighted average cost."""
    if quantity <= 0:
        raise ValueError("Quantity must be positive")
    if unit_price < 0 or fee < 0:
        raise ValueError("Price and fee must be non-negative")

    tx = Transaction(
        item_id=item_id,
        transaction_type=transaction_type.value,
        quantity=quantity,
        unit_price=unit_price,
        fee=fee,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        notes=notes,
    )
    session.add(tx)
    session.flush()
    rebuild_holding(session, item_id)
    return tx


def rebuild_holding(session: Session, item_id: int) -> Holding:
    """Rebuild holding from transaction history."""
    holding = session.scalar(select(Holding).where(Holding.item_id == item_id))
    if holding is None:
        holding = Holding(item_id=item_id, quantity=Decimal("0"), avg_cost=Decimal("0"))
        session.add(holding)

    quantity = Decimal("0")
    avg_cost = Decimal("0")
    realized_profit = Decimal("0")
    transactions = session.scalars(
        select(Transaction)
        .where(Transaction.item_id == item_id)
        .order_by(Transaction.occurred_at, Transaction.id)
    ).all()

    for tx in transactions:
        tx_qty = Decimal(tx.quantity)
        tx_price = Decimal(tx.unit_price)
        tx_fee = Decimal(tx.fee or 0)
        if tx.transaction_type == TransactionType.BUY.value:
            total_cost = quantity * avg_cost + tx_qty * tx_price + tx_fee
            quantity += tx_qty
            avg_cost = total_cost / quantity if quantity else Decimal("0")
        elif tx.transaction_type == TransactionType.SELL.value:
            if tx_qty > quantity:
                raise ValueError("Cannot sell more than current holding")
            proceeds = tx_qty * tx_price - tx_fee
            realized_profit += proceeds - tx_qty * avg_cost
            quantity -= tx_qty
            if quantity == 0:
                avg_cost = Decimal("0")
        else:
            raise ValueError(f"Unsupported transaction type: {tx.transaction_type}")

    holding.quantity = quantity
    holding.avg_cost = avg_cost
    holding.realized_profit = realized_profit
    session.flush()
    return holding
