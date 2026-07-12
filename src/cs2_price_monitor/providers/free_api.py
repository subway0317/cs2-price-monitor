from __future__ import annotations

from cs2_price_monitor.providers.base import PriceProvider, PriceQuote


class FreeApiProvider(PriceProvider):
    """Reserved free API provider."""

    name = "free_api"

    def get_quote(self, item_id: int) -> PriceQuote:
        raise NotImplementedError("Free API provider is reserved for future integration.")
