from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class PriceQuote:
    """Unified provider quote."""

    item_id: int
    provider: str
    lowest_listing_price: Decimal | None
    highest_buy_order_price: Decimal | None
    listing_count: int | None
    currency: str
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_payload: dict[str, Any] = field(default_factory=dict)


class PriceProvider(ABC):
    """Abstract price provider."""

    name: str

    @abstractmethod
    def get_quote(self, item_id: int) -> PriceQuote:
        """Return one normalized quote."""
