from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from cs2_price_monitor.config import (
    ProviderMode,
    SteamWebApiConfig,
    SteamWebApiFieldPaths,
)
from cs2_price_monitor.providers.steamwebapi import (
    SteamWebApiConfigurationError,
    SteamWebApiHttpError,
    SteamWebApiItemRef,
    SteamWebApiParseError,
    SteamWebApiProvider,
)


def _config(**overrides) -> SteamWebApiConfig:
    values = {
        "api_key": "test-key",
        "base_url": "https://steamwebapi.test",
        "timeout_seconds": 2.0,
        "provider_mode": ProviderMode.API,
        "market": "buff",
        "price_endpoint_template": "/quote/{market_name}",
        "auth_header_name": "X-API-Key",
        "auth_query_param_name": None,
        "field_paths": SteamWebApiFieldPaths(
            lowest_listing_price="data.lowest",
            highest_buy_order_price="data.highest_buy",
            listing_count="data.listings",
            currency="data.currency",
            captured_at="data.captured_at",
        ),
    }
    values.update(overrides)
    return SteamWebApiConfig(**values)


def _item_refs() -> dict[int, SteamWebApiItemRef]:
    return {
        1: SteamWebApiItemRef(
            item_id=1,
            market_name="AK-47 | Redline (Field-Tested)",
            buff_name="AK-47 Redline",
        )
    }


def _provider(handler, config: SteamWebApiConfig | None = None) -> SteamWebApiProvider:
    transport = httpx.MockTransport(handler)
    return SteamWebApiProvider(config=config or _config(), items=_item_refs(), transport=transport)


def _synthetic_payload() -> dict:
    path = Path(__file__).parent / "fixtures" / "steamwebapi" / "synthetic_success.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_parses_synthetic_success_response_into_price_quote():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "test-key"
        assert str(request.url).endswith("AK-47%20%7C%20Redline%20%28Field-Tested%29")
        return httpx.Response(200, json=_synthetic_payload())

    provider = _provider(handler)

    quote = provider.get_quote(1)

    assert quote.item_id == 1
    assert quote.provider == "steamwebapi:buff"
    assert quote.lowest_listing_price == Decimal("123.45")
    assert quote.highest_buy_order_price == Decimal("120.00")
    assert quote.listing_count == 17
    assert quote.currency == "CNY"


@pytest.mark.parametrize("status_code", [401, 429, 500])
def test_http_errors_are_reported(status_code: int):
    provider = _provider(lambda request: httpx.Response(status_code, json={"error": "synthetic"}))

    with pytest.raises(SteamWebApiHttpError, match=f"HTTP {status_code}"):
        provider.get_quote(1)


def test_timeout_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    provider = _provider(handler)

    with pytest.raises(SteamWebApiHttpError, match="ReadTimeout"):
        provider.get_quote(1)


def test_invalid_json_is_reported():
    provider = _provider(lambda request: httpx.Response(200, content=b"{not-json"))

    with pytest.raises(SteamWebApiParseError, match="invalid JSON"):
        provider.get_quote(1)


def test_missing_item_is_reported():
    provider = _provider(lambda request: httpx.Response(200, json=_synthetic_payload()))

    with pytest.raises(SteamWebApiConfigurationError, match="Item 999"):
        provider.get_quote(999)


def test_missing_response_field_is_reported():
    payload = _synthetic_payload()
    del payload["data"]["highest_buy"]
    provider = _provider(lambda request: httpx.Response(200, json=payload))

    with pytest.raises(SteamWebApiParseError, match="data.highest_buy"):
        provider.get_quote(1)


def test_missing_api_config_is_reported():
    config = _config(api_key=None, base_url="", price_endpoint_template=None)

    with pytest.raises(SteamWebApiConfigurationError, match="STEAMWEBAPI_API_KEY"):
        SteamWebApiProvider(config=config, items=_item_refs())
