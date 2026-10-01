from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from cs2_price_monitor.config import CSInventoryApiConfig
from cs2_price_monitor.providers.csinventoryapi import (
    CSInventoryApiItemRef,
    CSInventoryApiProvider,
    SyntheticCSInventoryApiProvider,
)
from cs2_price_monitor.providers.errors import (
    ApiConfigurationError,
    ApiDecodeError,
    ApiHttpError,
)


def _config(**overrides) -> CSInventoryApiConfig:
    values = {
        "api_key": "test-key",
        "base_url": "https://csinventoryapi.test",
        "timeout_seconds": 2.0,
        "market": "buff163",
        "app_id": "730",
        "currency": "cny",
        "price_endpoint_template": "/api/v2/prices/{market_name}",
        "auth_header_name": None,
        "auth_query_param_name": "api_key",
    }
    values.update(overrides)
    return CSInventoryApiConfig(**values)


def _item_refs() -> dict[int, CSInventoryApiItemRef]:
    return {
        1: CSInventoryApiItemRef(
            item_id=1,
            market_name="AK-47 | Redline (Field-Tested)",
            buff_name="AK-47 Redline",
        )
    }


def _real_success_payload() -> dict:
    path = Path(__file__).parent / "fixtures" / "csinventoryapi" / "real_success_sanitized.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _provider(handler, synthetic: bool = False):
    provider_type = SyntheticCSInventoryApiProvider if synthetic else CSInventoryApiProvider
    return provider_type(config=_config(), items=_item_refs(), transport=httpx.MockTransport(handler))


def test_real_sanitized_response_maps_to_price_quote():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/prices/AK-47 | Redline (Field-Tested)"
        assert request.url.params["api_key"] == "test-key"
        assert request.url.params["source"] == "buff163"
        assert request.url.params["app_id"] == "730"
        assert request.headers["accept"] == "application/json"
        return httpx.Response(200, json=_real_success_payload())

    quote = _provider(handler).get_quote(1)

    assert quote.provider == "csinventoryapi:buff163"
    assert quote.lowest_listing_price == Decimal("202.53")
    assert quote.highest_buy_order_price == Decimal("199.01")
    assert quote.listing_count == 9319
    assert quote.currency == "CNY"


def test_configured_currency_selects_matching_cents_map():
    provider = CSInventoryApiProvider(
        config=_config(currency="usd"),
        items=_item_refs(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_real_success_payload())
        ),
    )

    quote = provider.get_quote(1)

    assert quote.lowest_listing_price == Decimal("29.87")
    assert quote.highest_buy_order_price == Decimal("29.35")
    assert quote.currency == "USD"


@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
def test_http_errors_are_reported(status_code: int):
    provider = _provider(lambda request: httpx.Response(status_code, json={"error": "synthetic"}))

    with pytest.raises(ApiHttpError, match=f"HTTP {status_code}"):
        provider.get_quote(1)


def test_timeout_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    provider = _provider(handler)

    with pytest.raises(ApiHttpError, match="ReadTimeout"):
        provider.get_quote(1)


def test_invalid_json_is_reported():
    provider = _provider(lambda request: httpx.Response(200, content=b"{not-json"))

    with pytest.raises(ApiDecodeError, match="invalid JSON"):
        provider.get_quote(1)


def test_missing_item_is_reported():
    provider = _provider(lambda request: httpx.Response(200, json=_real_success_payload()))

    with pytest.raises(ApiConfigurationError, match="Item 999"):
        provider.get_quote(999)


def test_missing_price_fields_remain_none():
    payload = _real_success_payload()
    del payload["sell_price_cents"]
    del payload["quantity"]
    provider = _provider(lambda request: httpx.Response(200, json=payload))

    quote = provider.get_quote(1)

    assert quote.lowest_listing_price is None
    assert quote.listing_count is None
    assert quote.highest_buy_order_price == Decimal("199.01")


def test_missing_configured_currency_price_is_none():
    provider = CSInventoryApiProvider(
        config=_config(currency="jpy"),
        items=_item_refs(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_real_success_payload())
        ),
    )

    quote = provider.get_quote(1)

    assert quote.lowest_listing_price is None
    assert quote.highest_buy_order_price is None
    assert quote.currency == "JPY"


def test_missing_api_config_is_reported():
    config = _config(api_key=None, base_url="", price_endpoint_template=None)

    with pytest.raises(ApiConfigurationError, match="CS2_MONITOR_CSINVENTORY_API_KEY"):
        CSInventoryApiProvider(config=config, items=_item_refs())
