from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from cs2_price_monitor.config import PROJECT_ROOT, load_settings
from cs2_price_monitor.catalog.catalog_importer import (
    import_catalog_file,
    load_catalog_stats,
    parse_field_map,
)
from cs2_price_monitor.db import build_engine, build_session_factory
from cs2_price_monitor.db import init_db
from cs2_price_monitor.services.automatic_sync import (
    DEFAULT_SYNC_STATUS_PATH,
    build_automatic_sync_status,
    write_automatic_sync_status,
)
from cs2_price_monitor.services.migrations import migrate_sqlite_schema
from cs2_price_monitor.services.sync import SyncItemStatus, sync_all_enabled_items
from cs2_price_monitor.services.transactions import list_invalid_transaction_quantities


def main(argv: list[str] | None = None) -> int:
    """Command line entrypoint."""
    parser = argparse.ArgumentParser(description="CS2 price monitor tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Create database tables")
    subparsers.add_parser("migrate-db", help="Apply SQLite schema migrations")
    subparsers.add_parser(
        "diagnose-transaction-quantities",
        help="List transactions with non-integer or non-positive quantities",
    )
    sync_parser = subparsers.add_parser("sync-all", help="Run one one-shot API price sync")
    sync_parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Retries per item for transient HTTP errors",
    )
    sync_parser.add_argument(
        "--retry-base-seconds",
        type=float,
        default=2.0,
        help="Linear retry wait base in seconds",
    )
    sync_parser.add_argument(
        "--status-file",
        default=str(DEFAULT_SYNC_STATUS_PATH),
        help="Last automatic sync status JSON path",
    )

    catalog_parser = subparsers.add_parser("catalog", help="Catalog tools")
    catalog_subparsers = catalog_parser.add_subparsers(dest="catalog_command", required=True)
    import_parser = catalog_subparsers.add_parser("import", help="Import catalog JSON or CSV")
    import_parser.add_argument("--input", required=True, help="Raw input JSON or CSV path")
    import_parser.add_argument(
        "--output",
        default="data/catalog_generated/catalog_data.json",
        help="Normalized catalog output path",
    )
    import_parser.add_argument(
        "--field",
        action="append",
        default=[],
        help="Field mapping in target=source format",
    )
    import_parser.add_argument(
        "--report",
        default="data/catalog_generated/catalog_import_report.json",
        help="Catalog import report output path",
    )
    stats_parser = catalog_subparsers.add_parser("stats", help="Show catalog stats")
    stats_parser.add_argument(
        "--catalog",
        default="data/catalog_generated/catalog_data.json",
        help="Normalized catalog path",
    )
    args = parser.parse_args(argv)

    if args.command == "init-db":
        init_db()
        print("Database initialized.")
        return 0
    elif args.command == "migrate-db":
        result = migrate_sqlite_schema(build_engine())
        print("Applied migrations:", ", ".join(result.applied) or "none")
        print("Skipped migrations:", ", ".join(result.skipped) or "none")
        return 0
    elif args.command == "diagnose-transaction-quantities":
        return run_diagnose_transaction_quantities_command()
    elif args.command == "sync-all":
        return run_sync_all_command(
            max_retries=args.max_retries,
            retry_base_seconds=args.retry_base_seconds,
            status_file=Path(args.status_file),
        )
    elif args.command == "catalog" and args.catalog_command == "import":
        result = import_catalog_file(
            input_path=Path(args.input),
            output_path=Path(args.output),
            field_map=parse_field_map(args.field),
            report_path=Path(args.report),
        )
        _print_catalog_stats(result.stats)
        print(f"Catalog written to: {args.output}")
        return 0
    elif args.command == "catalog" and args.catalog_command == "stats":
        _print_catalog_stats(load_catalog_stats(Path(args.catalog)))
        return 0
    return 2


def run_diagnose_transaction_quantities_command() -> int:
    """Print transactions whose quantities are not positive integers."""
    engine = build_engine()
    init_db(engine)
    migrate_sqlite_schema(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        rows = list_invalid_transaction_quantities(session)
    if not rows:
        print("No invalid transaction quantities found.")
        return 0
    print("Invalid transaction quantities:")
    print("transaction_id,item_name,transaction_type,current_quantity,transaction_time")
    for row in rows:
        print(
            f"{row.transaction_id},"
            f"{row.item_name},"
            f"{row.transaction_type},"
            f"{row.quantity},"
            f"{row.occurred_at}"
        )
    return 1


def run_sync_all_command(
    *,
    max_retries: int = 3,
    retry_base_seconds: float = 2.0,
    status_file: Path = DEFAULT_SYNC_STATUS_PATH,
) -> int:
    """Run one API sync and return a process exit code."""
    started_at = datetime.now(timezone.utc)
    print(f"Sync started at: {started_at.isoformat()}")
    settings = load_settings()
    engine = build_engine(settings.database.url)
    init_db(engine)
    migrate_sqlite_schema(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        summary = sync_all_enabled_items(
            session,
            settings,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
        )
        session.commit()
    ended_at = datetime.now(timezone.utc)
    status = build_automatic_sync_status(
        started_at=started_at,
        ended_at=ended_at,
        summary=summary,
    )
    write_automatic_sync_status(status, _resolve_output_path(status_file))
    _print_sync_summary(summary, started_at, ended_at)
    return 1 if summary.failure_count else 0


def _resolve_output_path(path: Path) -> Path:
    """Resolve relative output paths from the project root."""
    return path if path.is_absolute() else PROJECT_ROOT / path


def _print_sync_summary(summary, started_at: datetime, ended_at: datetime) -> None:
    """Print a non-sensitive one-shot sync summary."""
    print(f"Sync ended at: {ended_at.isoformat()}")
    print(f"Total items: {len(summary.results)}")
    print(f"Successful items: {summary.success_count + summary.suspicious_count}")
    print(f"Failed items: {summary.failure_count}")
    print(f"Skipped items: {summary.skipped_count}")
    print(f"Started: {started_at.isoformat()}")
    print(f"Ended: {ended_at.isoformat()}")
    failed = [result for result in summary.results if result.status == SyncItemStatus.FAILED]
    if failed:
        print("Failures:")
        for result in failed:
            print(f"- item_id={result.item_id} market_name={result.market_name}: {result.message}")


def _print_catalog_stats(stats) -> None:
    """Print catalog stats. / 输出目录统计。"""
    print(f"Catalog total: {stats.valid_count}")
    print(f"Missing Chinese names: {stats.missing_cn_count}")
    print(f"Duplicate records: {stats.duplicate_count}")
    print(f"Invalid records: {stats.invalid_count}")
    print("Item types:")
    for item_type, count in stats.item_type_counts.items():
        print(f"  {item_type}: {count}")


if __name__ == "__main__":
    sys.exit(main())
