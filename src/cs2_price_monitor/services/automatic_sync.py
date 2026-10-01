from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cs2_price_monitor.config import PROJECT_ROOT
from cs2_price_monitor.services.sync import SyncItemStatus, SyncSummary


DEFAULT_SYNC_STATUS_PATH = PROJECT_ROOT / "logs" / "last_automatic_sync.json"


@dataclass(frozen=True)
class AutomaticSyncStatus:
    """Last one-shot automatic sync status for UI display."""

    started_at: str
    ended_at: str
    result: str
    total_items: int
    successful_items: int
    failed_items: int
    skipped_items: int
    failures: list[dict[str, Any]]


def build_automatic_sync_status(
    *,
    started_at: datetime,
    ended_at: datetime,
    summary: SyncSummary,
) -> AutomaticSyncStatus:
    """Build a serializable status object from one sync run."""
    failures = [
        {
            "item_id": result.item_id,
            "market_name": result.market_name,
            "reason": result.message,
        }
        for result in summary.results
        if result.status == SyncItemStatus.FAILED
    ]
    failed_items = summary.failure_count
    total_items = len(summary.results)
    return AutomaticSyncStatus(
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        result="success" if failed_items == 0 else "failed",
        total_items=total_items,
        successful_items=summary.success_count + summary.suspicious_count,
        failed_items=failed_items,
        skipped_items=summary.skipped_count,
        failures=failures,
    )


def write_automatic_sync_status(
    status: AutomaticSyncStatus,
    path: Path = DEFAULT_SYNC_STATUS_PATH,
) -> None:
    """Write the status file atomically enough for a local JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(status.__dict__, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def read_automatic_sync_status(
    path: Path = DEFAULT_SYNC_STATUS_PATH,
) -> AutomaticSyncStatus | None:
    """Read the last automatic sync status if it exists."""
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return AutomaticSyncStatus(
        started_at=str(payload.get("started_at", "")),
        ended_at=str(payload.get("ended_at", "")),
        result=str(payload.get("result", "unknown")),
        total_items=int(payload.get("total_items", 0)),
        successful_items=int(payload.get("successful_items", 0)),
        failed_items=int(payload.get("failed_items", 0)),
        skipped_items=int(payload.get("skipped_items", 0)),
        failures=list(payload.get("failures", [])),
    )
