from __future__ import annotations

from typing import Iterable

import httpx

from cs2_price_monitor.config import ApiProviderName, ProviderMode, Settings
from cs2_price_monitor.models import Item
from cs2_price_monitor.providers.base import PriceProvider
from cs2_price_monitor.providers.csinventoryapi import (
    CSInventoryApiItemRef,
    CSInventoryApiProvider,
    validate_csinventoryapi_config,
)
from cs2_price_monitor.providers.manual import ManualPriceProvider
from cs2_price_monitor.providers.steamwebapi import (
    SteamWebApiItemRef,
    SteamWebApiProvider,
    validate_steamwebapi_config,
)


class PriceProviderFactoryError(ValueError):
    """Raised when a provider cannot be created from settings."""


def create_api_price_provider(
    settings: Settings,
    items: Iterable[Item],
    client: httpx.Client | None = None,
    transport: httpx.BaseTransport | None = None,
) -> PriceProvider:
    """Create the configured external API provider."""
    if settings.api.provider == ApiProviderName.CSINVENTORYAPI:
        refs = {
            item.id: CSInventoryApiItemRef(
                item_id=item.id,
                market_name=item.market_name,
                buff_name=item.buff_name,
            )
            for item in items
        }
        return CSInventoryApiProvider(
            config=settings.csinventoryapi,
            items=refs,
            client=client,
            transport=transport,
        )
    if settings.api.provider == ApiProviderName.STEAMWEBAPI:
        refs = {
            item.id: SteamWebApiItemRef(
                item_id=item.id,
                market_name=item.market_name,
                buff_name=item.buff_name,
            )
            for item in items
        }
        return SteamWebApiProvider(
            config=settings.steamwebapi,
            items=refs,
            client=client,
            transport=transport,
        )
    raise PriceProviderFactoryError(f"Unsupported API provider: {settings.api.provider}")


def validate_api_provider_settings(settings: Settings) -> None:
    """Validate external API settings without creating a client."""
    if settings.api.provider == ApiProviderName.CSINVENTORYAPI:
        validate_csinventoryapi_config(settings.csinventoryapi)
        return
    if settings.api.provider == ApiProviderName.STEAMWEBAPI:
        validate_steamwebapi_config(settings.steamwebapi)
        return
    raise PriceProviderFactoryError(f"Unsupported API provider: {settings.api.provider}")


def create_price_provider(settings: Settings, items: Iterable[Item]) -> PriceProvider | None:
    """Create a provider for the configured mode.

    Manual mode returns None because manual entry constructs ManualPriceProvider
    from user input and must not initialize external API clients.
    """
    if settings.api.mode == ProviderMode.MANUAL:
        return None
    if settings.api.mode in {ProviderMode.API, ProviderMode.HYBRID}:
        return create_api_price_provider(settings, items)
    raise PriceProviderFactoryError(f"Unsupported provider mode: {settings.api.mode}")


__all__ = [
    "ManualPriceProvider",
    "PriceProviderFactoryError",
    "create_api_price_provider",
    "create_price_provider",
    "validate_api_provider_settings",
]
