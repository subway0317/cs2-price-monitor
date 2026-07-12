from __future__ import annotations

from cs2_price_monitor.providers.base import PriceProvider, PriceQuote


class PaidApiProvider(PriceProvider):
    """Reserved paid API provider."""

    name = "paid_api"

    def get_quote(self, item_id: int) -> PriceQuote:
        raise NotImplementedError("Paid API provider is reserved for future integration.")
