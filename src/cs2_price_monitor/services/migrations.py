from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class MigrationResult:
    """Result of one schema migration run."""

    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def migrate_sqlite_schema(engine: Engine) -> MigrationResult:
    """Apply idempotent SQLite schema migrations."""
    if engine.dialect.name != "sqlite":
        raise ValueError("Schema migration currently supports SQLite only")

    applied: list[str] = []
    skipped: list[str] = []
    with engine.begin() as connection:
        if _has_column(connection, "daily_price_summaries", "currency"):
            skipped.append("daily_price_summaries.currency")
        else:
            connection.execute(
                text(
                    "ALTER TABLE daily_price_summaries "
                    "ADD COLUMN currency VARCHAR(12) NOT NULL DEFAULT 'CNY'"
                )
            )
            applied.append("daily_price_summaries.currency")

        _ensure_daily_summary_currency_index(connection, applied, skipped)
        _relax_price_snapshot_nullable_columns(connection, applied, skipped)
        _ensure_manual_holdings_table(connection, applied, skipped)
        _ensure_transaction_updated_at(connection, applied, skipped)
        _ensure_transaction_audit_logs_table(connection, applied, skipped)

    return MigrationResult(applied=applied, skipped=skipped)


def _has_column(connection, table_name: str, column_name: str) -> bool:
    """Return whether a SQLite table contains a column."""
    rows = connection.execute(text(f"PRAGMA table_info({table_name})")).mappings()
    return any(row["name"] == column_name for row in rows)


def _ensure_daily_summary_currency_index(connection, applied: list[str], skipped: list[str]) -> None:
    """Create the new uniqueness index when it is missing."""
    index_name = "uq_daily_item_date_currency_idx"
    rows = connection.execute(text("PRAGMA index_list(daily_price_summaries)")).mappings()
    if any(row["name"] == index_name for row in rows):
        skipped.append(index_name)
        return
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_item_date_currency_idx "
            "ON daily_price_summaries (item_id, summary_date, currency)"
        )
    )
    applied.append(index_name)


def _relax_price_snapshot_nullable_columns(connection, applied: list[str], skipped: list[str]) -> None:
    """Rebuild price_snapshots when legacy columns are still NOT NULL."""
    nullable_columns = {
        "lowest_listing_price",
        "highest_buy_order_price",
        "listing_count",
    }
    rows = list(connection.execute(text("PRAGMA table_info(price_snapshots)")).mappings())
    if not rows:
        skipped.append("price_snapshots.nullable_price_columns")
        return
    needs_rebuild = any(
        row["name"] in nullable_columns and row["notnull"] == 1 for row in rows
    )
    if not needs_rebuild:
        skipped.append("price_snapshots.nullable_price_columns")
        return

    connection.execute(text("PRAGMA foreign_keys=OFF"))
    connection.execute(
        text(
            """
            CREATE TABLE price_snapshots_new (
                id INTEGER NOT NULL PRIMARY KEY,
                item_id INTEGER NOT NULL,
                provider VARCHAR(80) NOT NULL,
                lowest_listing_price NUMERIC(18, 6),
                highest_buy_order_price NUMERIC(18, 6),
                listing_count INTEGER,
                currency VARCHAR(12) NOT NULL,
                captured_at DATETIME NOT NULL,
                is_suspicious BOOLEAN NOT NULL,
                suspicion_reason TEXT,
                raw_payload TEXT,
                FOREIGN KEY(item_id) REFERENCES items (id)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO price_snapshots_new (
                id, item_id, provider, lowest_listing_price, highest_buy_order_price,
                listing_count, currency, captured_at, is_suspicious, suspicion_reason, raw_payload
            )
            SELECT
                id, item_id, provider, lowest_listing_price, highest_buy_order_price,
                listing_count, currency, captured_at, is_suspicious, suspicion_reason, raw_payload
            FROM price_snapshots
            """
        )
    )
    connection.execute(text("DROP TABLE price_snapshots"))
    connection.execute(text("ALTER TABLE price_snapshots_new RENAME TO price_snapshots"))
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_price_snapshots_item_id ON price_snapshots (item_id)")
    )
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_price_snapshots_provider ON price_snapshots (provider)")
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_price_snapshots_captured_at "
            "ON price_snapshots (captured_at)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_price_snapshots_is_suspicious "
            "ON price_snapshots (is_suspicious)"
        )
    )
    connection.execute(text("PRAGMA foreign_keys=ON"))
    applied.append("price_snapshots.nullable_price_columns")


def _ensure_manual_holdings_table(connection, applied: list[str], skipped: list[str]) -> None:
    """Create manual_holdings for opening inventory entries."""
    rows = connection.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='manual_holdings'")
    ).mappings()
    if any(row["name"] == "manual_holdings" for row in rows):
        skipped.append("manual_holdings")
        return

    connection.execute(
        text(
            """
            CREATE TABLE manual_holdings (
                id INTEGER NOT NULL PRIMARY KEY,
                item_id INTEGER NOT NULL,
                catalog_id VARCHAR(120),
                version VARCHAR(120),
                quantity NUMERIC(18, 6) NOT NULL,
                unit_cost NUMERIC(18, 6),
                acquired_at DATETIME,
                source VARCHAR(80) NOT NULL DEFAULT 'steam_manual',
                note TEXT,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT ck_manual_holdings_quantity_positive CHECK (quantity > 0),
                CONSTRAINT ck_manual_holdings_unit_cost_non_negative
                    CHECK (unit_cost IS NULL OR unit_cost >= 0),
                FOREIGN KEY(item_id) REFERENCES items (id)
            )
            """
        )
    )
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_manual_holdings_item_id ON manual_holdings (item_id)")
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_manual_holdings_catalog_id "
            "ON manual_holdings (catalog_id)"
        )
    )
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_manual_holdings_version ON manual_holdings (version)")
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_manual_holdings_is_active "
            "ON manual_holdings (is_active)"
        )
    )
    applied.append("manual_holdings")


def _ensure_transaction_updated_at(connection, applied: list[str], skipped: list[str]) -> None:
    """Add transactions.updated_at for editable transaction history."""
    if _has_column(connection, "transactions", "updated_at"):
        skipped.append("transactions.updated_at")
        return
    if not _has_table(connection, "transactions"):
        skipped.append("transactions.updated_at")
        return
    connection.execute(text("ALTER TABLE transactions ADD COLUMN updated_at DATETIME"))
    connection.execute(text("UPDATE transactions SET updated_at = created_at WHERE updated_at IS NULL"))
    applied.append("transactions.updated_at")


def _ensure_transaction_audit_logs_table(connection, applied: list[str], skipped: list[str]) -> None:
    """Create audit logs for transaction updates and deletes."""
    if _has_table(connection, "transaction_audit_logs"):
        skipped.append("transaction_audit_logs")
        return

    connection.execute(
        text(
            """
            CREATE TABLE transaction_audit_logs (
                audit_id INTEGER NOT NULL PRIMARY KEY,
                transaction_id INTEGER NOT NULL,
                action VARCHAR(20) NOT NULL,
                before_data JSON NOT NULL,
                after_data JSON,
                changed_at DATETIME NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_transaction_audit_logs_transaction_id "
            "ON transaction_audit_logs (transaction_id)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_transaction_audit_logs_action "
            "ON transaction_audit_logs (action)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_transaction_audit_logs_changed_at "
            "ON transaction_audit_logs (changed_at)"
        )
    )
    applied.append("transaction_audit_logs")


def _has_table(connection, table_name: str) -> bool:
    """Return whether a SQLite table exists."""
    rows = connection.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:table_name"),
        {"table_name": table_name},
    ).mappings()
    return any(row["name"] == table_name for row in rows)
