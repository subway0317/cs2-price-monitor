from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from cs2_price_monitor.providers.base import PriceProvider, PriceQuote


class ManualPriceProvider(PriceProvider):
    """Manual BUFF price entry provider."""

    name = "manual"

    def __init__(
        self,
        lowest_listing_price: Decimal,
        highest_buy_order_price: Decimal,
        listing_count: int,
        currency: str = "CNY",
        captured_at: datetime | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> None:
        self.lowest_listing_price = lowest_listing_price
        self.highest_buy_order_price = highest_buy_order_price
        self.listing_count = listing_count
        self.currency = currency
        self.captured_at = captured_at or datetime.now(timezone.utc)
        self.raw_payload = raw_payload or {}

    def get_quote(self, item_id: int) -> PriceQuote:
        return PriceQuote(
            item_id=item_id,
            provider=self.name,
            lowest_listing_price=self.lowest_listing_price,
            highest_buy_order_price=self.highest_buy_order_price,
            listing_count=self.listing_count,
            currency=self.currency,
            captured_at=self.captured_at,
            raw_payload=self.raw_payload,
        )
