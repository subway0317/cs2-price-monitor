from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2_price_monitor.models import Holding, Item, Transaction, TransactionAuditLog, TransactionType, utc_now


class InventoryHistoryError(ValueError):
    """Raised when a transaction edit would create negative historical inventory."""


@dataclass(frozen=True)
class InvalidTransactionQuantity:
    """Read-only diagnostic row for invalid transaction quantities."""

    transaction_id: int
    item_name: str
    transaction_type: str
    quantity: str
    occurred_at: datetime


def get_transactions(session: Session, limit: int | None = None) -> list[Transaction]:
    """Return transactions in reverse chronological order."""
    query = select(Transaction).order_by(Transaction.occurred_at.desc(), Transaction.id.desc())
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query).all())


def get_transaction(session: Session, transaction_id: int) -> Transaction:
    """Return one transaction or raise a clear error."""
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise ValueError(f"Transaction {transaction_id} not found")
    return transaction


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
    normalized_type = _normalize_transaction_type(transaction_type)
    normalized_quantity = parse_transaction_quantity(quantity)
    _validate_item_is_selectable(session, item_id)
    if unit_price < 0 or fee < 0:
        raise ValueError("Price and fee must be non-negative")

    tx = Transaction(
        item_id=item_id,
        transaction_type=normalized_type.value,
        quantity=normalized_quantity,
        unit_price=unit_price,
        fee=fee,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        notes=notes,
    )
    session.add(tx)
    session.flush()
    rebuild_holding(session, item_id)
    return tx


def update_transaction(
    session: Session,
    transaction_id: int,
    *,
    item_id: int,
    transaction_type: TransactionType | str,
    quantity: Decimal,
    unit_price: Decimal,
    occurred_at: datetime,
    notes: str | None = None,
) -> Transaction:
    """Update an existing transaction and rebuild affected holdings atomically."""
    normalized_type = _normalize_transaction_type(transaction_type)
    normalized_quantity = parse_transaction_quantity(quantity)
    _validate_transaction_fields(item_id, normalized_type, normalized_quantity, unit_price)
    _validate_item_is_selectable(session, item_id)
    transaction = get_transaction(session, transaction_id)
    before_data = transaction_to_dict(transaction)
    affected_item_ids = {transaction.item_id, item_id}

    with session.begin_nested():
        _validate_item_history_with_replacement(
            session,
            transaction.item_id,
            transaction_id,
            proposed_item_id=item_id,
            proposed_type=normalized_type,
            proposed_quantity=normalized_quantity,
            proposed_unit_price=unit_price,
            proposed_fee=Decimal(transaction.fee or 0),
            proposed_occurred_at=occurred_at,
        )
        if item_id != transaction.item_id:
            _validate_item_history_with_replacement(
                session,
                item_id,
                transaction_id,
                proposed_item_id=item_id,
                proposed_type=normalized_type,
                proposed_quantity=normalized_quantity,
                proposed_unit_price=unit_price,
                proposed_fee=Decimal(transaction.fee or 0),
                proposed_occurred_at=occurred_at,
            )

        transaction.item_id = item_id
        transaction.transaction_type = normalized_type.value
        transaction.quantity = normalized_quantity
        transaction.unit_price = unit_price
        transaction.occurred_at = occurred_at
        transaction.notes = notes
        transaction.updated_at = utc_now()
        session.flush()
        after_data = transaction_to_dict(transaction)
        session.add(
            TransactionAuditLog(
                transaction_id=transaction_id,
                action="UPDATE",
                before_data=before_data,
                after_data=after_data,
            )
        )
        for affected_item_id in sorted(affected_item_ids):
            rebuild_holding(session, affected_item_id)
        session.flush()
    return transaction


def delete_transaction(session: Session, transaction_id: int) -> Transaction:
    """Delete a transaction and rebuild affected holdings atomically."""
    transaction = get_transaction(session, transaction_id)
    before_data = transaction_to_dict(transaction)
    affected_item_id = transaction.item_id

    with session.begin_nested():
        _validate_item_history_without_transaction(session, affected_item_id, transaction_id)
        session.add(
            TransactionAuditLog(
                transaction_id=transaction_id,
                action="DELETE",
                before_data=before_data,
                after_data=None,
            )
        )
        session.delete(transaction)
        rebuild_holding(session, affected_item_id)
        session.flush()
    return transaction


def validate_inventory_history(
    session: Session,
    item_id: int,
    *,
    excluded_transaction_id: int | None = None,
    proposed_transaction: dict[str, Any] | None = None,
) -> None:
    """Validate that an item's chronological transactions never go below zero."""
    rows = _history_rows(
        session,
        item_id,
        excluded_transaction_id=excluded_transaction_id,
        proposed_transaction=proposed_transaction,
    )
    _raise_if_negative_inventory(rows)


def list_invalid_transaction_quantities(session: Session) -> list[InvalidTransactionQuantity]:
    """Return transactions whose quantities do not satisfy the integer item rule."""
    rows = session.execute(
        select(Transaction, Item.market_name)
        .join(Item, Transaction.item_id == Item.id)
        .order_by(Transaction.occurred_at, Transaction.id)
    ).all()
    invalid: list[InvalidTransactionQuantity] = []
    for transaction, item_name in rows:
        if is_valid_transaction_quantity(transaction.quantity):
            continue
        invalid.append(
            InvalidTransactionQuantity(
                transaction_id=transaction.id,
                item_name=item_name,
                transaction_type=transaction.transaction_type,
                quantity=str(transaction.quantity),
                occurred_at=transaction.occurred_at,
            )
        )
    return invalid


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


class InventoryCalculationService:
    """Service wrapper for transaction-source inventory calculations."""

    def __init__(self, session: Session):
        self.session = session

    def validate_inventory_history(
        self,
        item_id: int,
        *,
        excluded_transaction_id: int | None = None,
        proposed_transaction: dict[str, Any] | None = None,
    ) -> None:
        validate_inventory_history(
            self.session,
            item_id,
            excluded_transaction_id=excluded_transaction_id,
            proposed_transaction=proposed_transaction,
        )

    def recalculate_item_inventory(self, item_id: int) -> Holding:
        return rebuild_holding(self.session, item_id)


class TransactionService:
    """Service wrapper for transaction history operations."""

    def __init__(self, session: Session):
        self.session = session

    def get_transactions(self, limit: int | None = None) -> list[Transaction]:
        return get_transactions(self.session, limit)

    def get_transaction(self, transaction_id: int) -> Transaction:
        return get_transaction(self.session, transaction_id)

    def update_transaction(self, transaction_id: int, changes: dict[str, Any]) -> Transaction:
        return update_transaction(self.session, transaction_id, **changes)

    def delete_transaction(self, transaction_id: int) -> Transaction:
        return delete_transaction(self.session, transaction_id)

    def list_invalid_transaction_quantities(self) -> list[InvalidTransactionQuantity]:
        return list_invalid_transaction_quantities(self.session)


def transaction_to_dict(transaction: Transaction) -> dict[str, Any]:
    """Serialize a transaction into audit-safe primitive values."""
    return {
        "id": transaction.id,
        "item_id": transaction.item_id,
        "transaction_type": transaction.transaction_type,
        "quantity": str(Decimal(transaction.quantity)),
        "unit_price": str(Decimal(transaction.unit_price)),
        "fee": str(Decimal(transaction.fee or 0)),
        "notes": transaction.notes,
        "occurred_at": _datetime_to_iso(transaction.occurred_at),
        "created_at": _datetime_to_iso(transaction.created_at),
        "updated_at": _datetime_to_iso(getattr(transaction, "updated_at", None)),
    }


def _validate_transaction_fields(
    item_id: int,
    transaction_type: TransactionType,
    quantity: Decimal,
    unit_price: Decimal,
) -> None:
    if item_id <= 0:
        raise ValueError("Item is required")
    parse_transaction_quantity(quantity)
    if unit_price < 0:
        raise ValueError("Unit price must be non-negative")
    if transaction_type not in (TransactionType.BUY, TransactionType.SELL):
        raise ValueError("Transaction type must be BUY or SELL")


def _validate_item_is_selectable(session: Session, item_id: int) -> None:
    item = session.get(Item, item_id)
    if item is None or not item.market_name.strip():
        raise ValueError("Item name is required")


def parse_transaction_quantity(value: object) -> Decimal:
    """Parse and validate a CS2 transaction quantity as a positive integer Decimal."""
    if isinstance(value, bool):
        raise ValueError("交易数量必须是大于等于 1 的整数。")
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("交易数量必须是大于等于 1 的整数。") from exc
    if not is_valid_transaction_quantity(quantity):
        raise ValueError("交易数量必须是大于等于 1 的整数。")
    return quantity


def is_valid_transaction_quantity(value: object) -> bool:
    """Return whether a value is a finite positive integer quantity."""
    if isinstance(value, bool) or value is None:
        return False
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False
    return quantity.is_finite() and quantity >= 1 and quantity == quantity.to_integral_value()


def _normalize_transaction_type(value: TransactionType | str) -> TransactionType:
    if isinstance(value, TransactionType):
        return value
    normalized = str(value).strip().lower()
    try:
        return TransactionType(normalized)
    except ValueError as exc:
        raise ValueError("Transaction type must be BUY or SELL") from exc


def _validate_item_history_with_replacement(
    session: Session,
    item_id: int,
    transaction_id: int,
    *,
    proposed_item_id: int,
    proposed_type: TransactionType,
    proposed_quantity: Decimal,
    proposed_unit_price: Decimal,
    proposed_fee: Decimal,
    proposed_occurred_at: datetime,
) -> None:
    proposed_transaction = None
    if proposed_item_id == item_id:
        proposed_transaction = {
            "id": transaction_id,
            "item_id": proposed_item_id,
            "transaction_type": proposed_type.value,
            "quantity": proposed_quantity,
            "unit_price": proposed_unit_price,
            "fee": proposed_fee,
            "occurred_at": proposed_occurred_at,
        }
    validate_inventory_history(
        session,
        item_id,
        excluded_transaction_id=transaction_id,
        proposed_transaction=proposed_transaction,
    )


def _validate_item_history_without_transaction(
    session: Session,
    item_id: int,
    transaction_id: int,
) -> None:
    validate_inventory_history(
        session,
        item_id,
        excluded_transaction_id=transaction_id,
        proposed_transaction=None,
    )


def _history_rows(
    session: Session,
    item_id: int,
    *,
    excluded_transaction_id: int | None,
    proposed_transaction: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    transactions = session.scalars(
        select(Transaction)
        .where(Transaction.item_id == item_id)
        .order_by(Transaction.occurred_at, Transaction.id)
    ).all()
    rows: list[dict[str, Any]] = []
    for transaction in transactions:
        if excluded_transaction_id is not None and transaction.id == excluded_transaction_id:
            continue
        rows.append(transaction_to_dict(transaction))
    if proposed_transaction is not None:
        rows.append(_proposed_transaction_to_row(proposed_transaction))
    return sorted(rows, key=lambda row: (_as_datetime(row["occurred_at"]), int(row["id"] or 0)))


def _proposed_transaction_to_row(proposed: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": proposed.get("id", 0),
        "item_id": proposed["item_id"],
        "transaction_type": _normalize_transaction_type(proposed["transaction_type"]).value,
        "quantity": str(parse_transaction_quantity(proposed["quantity"])),
        "unit_price": str(Decimal(proposed["unit_price"])),
        "fee": str(Decimal(proposed.get("fee") or 0)),
        "notes": proposed.get("notes"),
        "occurred_at": _datetime_to_iso(proposed["occurred_at"]),
        "created_at": None,
        "updated_at": None,
    }


def _raise_if_negative_inventory(rows: list[dict[str, Any]]) -> None:
    quantity = Decimal("0")
    for row in rows:
        tx_qty = Decimal(row["quantity"])
        tx_type = _normalize_transaction_type(row["transaction_type"])
        if tx_type == TransactionType.BUY:
            quantity += tx_qty
        elif tx_type == TransactionType.SELL:
            quantity -= tx_qty
        if quantity < 0:
            occurred_at = _as_datetime(row["occurred_at"])
            raise InventoryHistoryError(
                "无法保存：修改后该物品在 "
                f"{occurred_at.date()} 的累计库存将变为 {quantity}。"
            )


def _datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _as_datetime(value: object) -> datetime:
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        raise ValueError(f"Invalid datetime value: {value}")
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)
