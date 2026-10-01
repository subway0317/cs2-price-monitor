from __future__ import annotations

from sqlalchemy import create_engine, text

from cs2_price_monitor.services.migrations import migrate_sqlite_schema


def test_migrate_adds_daily_summary_currency_and_is_idempotent():
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE daily_price_summaries (
                    id INTEGER NOT NULL PRIMARY KEY,
                    item_id INTEGER NOT NULL,
                    summary_date DATE NOT NULL,
                    open_price NUMERIC(18, 6) NOT NULL,
                    high_price NUMERIC(18, 6) NOT NULL,
                    low_price NUMERIC(18, 6) NOT NULL,
                    close_price NUMERIC(18, 6) NOT NULL,
                    median_price NUMERIC(18, 6) NOT NULL,
                    snapshot_count INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO daily_price_summaries (
                    id, item_id, summary_date, open_price, high_price, low_price,
                    close_price, median_price, snapshot_count, created_at, updated_at
                ) VALUES (
                    1, 10, '2026-07-12', 100, 110, 90, 105, 102, 3,
                    '2026-07-12 00:00:00', '2026-07-12 00:00:00'
                )
                """
            )
        )

    first = migrate_sqlite_schema(engine)
    second = migrate_sqlite_schema(engine)

    with engine.connect() as connection:
        columns = {
            row["name"]: row
            for row in connection.execute(text("PRAGMA table_info(daily_price_summaries)")).mappings()
        }
        row = connection.execute(
            text("SELECT item_id, summary_date, currency FROM daily_price_summaries")
        ).mappings().one()

    assert "daily_price_summaries.currency" in first.applied
    assert "daily_price_summaries.currency" in second.skipped
    assert columns["currency"]["type"] == "VARCHAR(12)"
    assert columns["currency"]["notnull"] == 1
    assert columns["currency"]["dflt_value"] == "'CNY'"
    assert row["item_id"] == 10
    assert row["summary_date"] == "2026-07-12"
    assert row["currency"] == "CNY"


def test_migrate_relaxes_price_snapshot_nullable_columns_and_preserves_rows():
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE price_snapshots (
                    id INTEGER NOT NULL PRIMARY KEY,
                    item_id INTEGER NOT NULL,
                    provider VARCHAR(80) NOT NULL,
                    lowest_listing_price NUMERIC(18, 6) NOT NULL,
                    highest_buy_order_price NUMERIC(18, 6) NOT NULL,
                    listing_count INTEGER NOT NULL,
                    currency VARCHAR(12) NOT NULL,
                    captured_at DATETIME NOT NULL,
                    is_suspicious BOOLEAN NOT NULL,
                    suspicion_reason TEXT,
                    raw_payload TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE daily_price_summaries (
                    id INTEGER NOT NULL PRIMARY KEY,
                    item_id INTEGER NOT NULL,
                    summary_date DATE NOT NULL,
                    currency VARCHAR(12) NOT NULL DEFAULT 'CNY',
                    open_price NUMERIC(18, 6) NOT NULL,
                    high_price NUMERIC(18, 6) NOT NULL,
                    low_price NUMERIC(18, 6) NOT NULL,
                    close_price NUMERIC(18, 6) NOT NULL,
                    median_price NUMERIC(18, 6) NOT NULL,
                    snapshot_count INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO price_snapshots (
                    id, item_id, provider, lowest_listing_price, highest_buy_order_price,
                    listing_count, currency, captured_at, is_suspicious
                ) VALUES (
                    1, 10, 'manual', 100, 95, 5, 'CNY', '2026-07-12 00:00:00', 0
                )
                """
            )
        )

    result = migrate_sqlite_schema(engine)

    with engine.connect() as connection:
        columns = {
            row["name"]: row
            for row in connection.execute(text("PRAGMA table_info(price_snapshots)")).mappings()
        }
        row = connection.execute(
            text("SELECT provider, lowest_listing_price, listing_count FROM price_snapshots")
        ).mappings().one()

    assert "price_snapshots.nullable_price_columns" in result.applied
    assert columns["lowest_listing_price"]["notnull"] == 0
    assert columns["highest_buy_order_price"]["notnull"] == 0
    assert columns["listing_count"]["notnull"] == 0
    assert row["provider"] == "manual"
    assert row["listing_count"] == 5


def test_migrate_creates_manual_holdings_table_and_is_idempotent():
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE items (
                    id INTEGER NOT NULL PRIMARY KEY,
                    market_name VARCHAR(255) NOT NULL,
                    buff_name VARCHAR(255),
                    category VARCHAR(80),
                    is_active BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE daily_price_summaries (
                    id INTEGER NOT NULL PRIMARY KEY,
                    item_id INTEGER NOT NULL,
                    summary_date DATE NOT NULL,
                    currency VARCHAR(12) NOT NULL DEFAULT 'CNY',
                    open_price NUMERIC(18, 6) NOT NULL,
                    high_price NUMERIC(18, 6) NOT NULL,
                    low_price NUMERIC(18, 6) NOT NULL,
                    close_price NUMERIC(18, 6) NOT NULL,
                    median_price NUMERIC(18, 6) NOT NULL,
                    snapshot_count INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE price_snapshots (
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
                    raw_payload TEXT
                )
                """
            )
        )

    first = migrate_sqlite_schema(engine)
    second = migrate_sqlite_schema(engine)

    with engine.connect() as connection:
        columns = {
            row["name"]: row
            for row in connection.execute(text("PRAGMA table_info(manual_holdings)")).mappings()
        }

    assert "manual_holdings" in first.applied
    assert "manual_holdings" in second.skipped
    assert columns["item_id"]["notnull"] == 1
    assert columns["catalog_id"]["notnull"] == 0
    assert columns["version"]["notnull"] == 0
    assert columns["unit_cost"]["notnull"] == 0
    assert columns["acquired_at"]["notnull"] == 0
    assert columns["is_active"]["dflt_value"] == "1"


def test_migrate_adds_transaction_edit_schema_and_is_idempotent():
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE items (
                    id INTEGER NOT NULL PRIMARY KEY,
                    market_name VARCHAR(255) NOT NULL,
                    buff_name VARCHAR(255),
                    category VARCHAR(80),
                    is_active BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE transactions (
                    id INTEGER NOT NULL PRIMARY KEY,
                    item_id INTEGER NOT NULL,
                    transaction_type VARCHAR(10) NOT NULL,
                    quantity NUMERIC(18, 6) NOT NULL,
                    unit_price NUMERIC(18, 6) NOT NULL,
                    fee NUMERIC(18, 6) NOT NULL,
                    notes TEXT,
                    occurred_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO transactions (
                    id, item_id, transaction_type, quantity, unit_price, fee,
                    occurred_at, created_at
                ) VALUES (
                    1, 10, 'buy', 1, 100, 0,
                    '2026-07-12 10:00:00', '2026-07-12 11:00:00'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE daily_price_summaries (
                    id INTEGER NOT NULL PRIMARY KEY,
                    item_id INTEGER NOT NULL,
                    summary_date DATE NOT NULL,
                    currency VARCHAR(12) NOT NULL DEFAULT 'CNY',
                    open_price NUMERIC(18, 6) NOT NULL,
                    high_price NUMERIC(18, 6) NOT NULL,
                    low_price NUMERIC(18, 6) NOT NULL,
                    close_price NUMERIC(18, 6) NOT NULL,
                    median_price NUMERIC(18, 6) NOT NULL,
                    snapshot_count INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE price_snapshots (
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
                    raw_payload TEXT
                )
                """
            )
        )

    first = migrate_sqlite_schema(engine)
    second = migrate_sqlite_schema(engine)

    with engine.connect() as connection:
        transaction_columns = {
            row["name"]: row
            for row in connection.execute(text("PRAGMA table_info(transactions)")).mappings()
        }
        audit_columns = {
            row["name"]: row
            for row in connection.execute(text("PRAGMA table_info(transaction_audit_logs)")).mappings()
        }
        row = connection.execute(text("SELECT updated_at FROM transactions WHERE id = 1")).mappings().one()

    assert "transactions.updated_at" in first.applied
    assert "transaction_audit_logs" in first.applied
    assert "transactions.updated_at" in second.skipped
    assert "transaction_audit_logs" in second.skipped
    assert "updated_at" in transaction_columns
    assert row["updated_at"] == "2026-07-12 11:00:00"
    assert audit_columns["transaction_id"]["notnull"] == 1
    assert audit_columns["before_data"]["notnull"] == 1
