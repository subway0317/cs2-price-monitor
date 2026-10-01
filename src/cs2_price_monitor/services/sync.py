from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from cs2_price_monitor.config import ProviderMode, Settings
from cs2_price_monitor.models import PriceSnapshot
from cs2_price_monitor.providers.base import PriceProvider
from cs2_price_monitor.providers.errors import ApiHttpError, PriceProviderError
from cs2_price_monitor.providers.factory import create_api_price_provider
from cs2_price_monitor.services.items import list_enabled_items
from cs2_price_monitor.services.prices import record_price_quote, regenerate_daily_summaries


class SyncItemStatus(StrEnum):
    """Possible status values for one item sync."""

    SUCCESS = "success"
    FAILED = "failed"
    SUSPICIOUS = "suspicious"


@dataclass(frozen=True)
class SyncItemResult:
    """Result for one item sync attempt."""

    item_id: int
    market_name: str
    status: SyncItemStatus
    message: str
    snapshot_id: int | None = None
    provider: str | None = None
    is_suspicious: bool = False


@dataclass(frozen=True)
class SyncSummary:
    """Aggregated sync result."""

    mode: ProviderMode
    success_count: int
    failure_count: int
    suspicious_count: int
    skipped_count: int
    results: list[SyncItemResult]


def sync_all_enabled_items(
    session: Session,
    settings: Settings,
    provider: PriceProvider | None = None,
    max_retries: int = 0,
    retry_base_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> SyncSummary:
    """Sync all enabled items from the configured API provider."""
    items = list_enabled_items(session)
    if settings.api.mode == ProviderMode.MANUAL:
        return SyncSummary(
            mode=settings.api.mode,
            success_count=0,
            failure_count=len(items),
            suspicious_count=0,
            skipped_count=0,
            results=[
                SyncItemResult(
                    item_id=item.id,
                    market_name=item.market_name,
                    status=SyncItemStatus.FAILED,
                    message="API sync is disabled in manual mode",
                )
                for item in items
            ],
        )

    api_provider = provider or create_api_price_provider(settings, items)
    owns_provider = provider is None
    results: list[SyncItemResult] = []
    saved_any = False
    try:
        for item in items:
            try:
                quote = _get_quote_with_retries(
                    api_provider,
                    item.id,
                    max_retries=max_retries,
                    retry_base_seconds=retry_base_seconds,
                    sleep=sleep,
                )
                snapshot = record_price_quote(session, quote)
                saved_any = True
                results.append(_success_result(item.id, item.market_name, snapshot))
            except (PriceProviderError, ValueError) as exc:
                results.append(
                    SyncItemResult(
                        item_id=item.id,
                        market_name=item.market_name,
                        status=SyncItemStatus.FAILED,
                        message=str(exc),
                    )
                )
    finally:
        if owns_provider and hasattr(api_provider, "close"):
            api_provider.close()

    if saved_any:
        regenerate_daily_summaries(session)
    return SyncSummary(
        mode=settings.api.mode,
        success_count=sum(result.status == SyncItemStatus.SUCCESS for result in results),
        failure_count=sum(result.status == SyncItemStatus.FAILED for result in results),
        suspicious_count=sum(result.status == SyncItemStatus.SUSPICIOUS for result in results),
        skipped_count=0,
        results=results,
    )


def _get_quote_with_retries(
    provider: PriceProvider,
    item_id: int,
    *,
    max_retries: int,
    retry_base_seconds: float,
    sleep: Callable[[float], None],
):
    """Fetch one quote and retry only transient provider errors."""
    attempts = max(0, max_retries) + 1
    for attempt_index in range(attempts):
        try:
            return provider.get_quote(item_id)
        except PriceProviderError as exc:
            is_last_attempt = attempt_index >= attempts - 1
            if is_last_attempt or not _is_retryable_provider_error(exc):
                raise
            delay = retry_base_seconds * (attempt_index + 1)
            if delay > 0:
                sleep(delay)
    raise RuntimeError("unreachable retry loop state")


def _is_retryable_provider_error(exc: PriceProviderError) -> bool:
    """Return whether an error is likely transient."""
    if not isinstance(exc, ApiHttpError):
        return False
    message = str(exc)
    retryable_markers = (
        "HTTP 429",
        "HTTP 500",
        "HTTP 502",
        "HTTP 503",
        "HTTP 504",
        "TimeoutException",
        "TransportError",
        "ConnectError",
        "ReadError",
        "WriteError",
        "PoolTimeout",
    )
    return any(marker in message for marker in retryable_markers)


def _success_result(item_id: int, market_name: str, snapshot: PriceSnapshot) -> SyncItemResult:
    """Build a success result from a saved snapshot."""
    is_suspicious = bool(snapshot.is_suspicious)
    return SyncItemResult(
        item_id=item_id,
        market_name=market_name,
        status=SyncItemStatus.SUSPICIOUS if is_suspicious else SyncItemStatus.SUCCESS,
        message=snapshot.suspicion_reason if is_suspicious else "Saved",
        snapshot_id=snapshot.id,
        provider=snapshot.provider,
        is_suspicious=is_suspicious,
    )
