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
    ApiSchemaNotConfiguredError,
)


def _config(**overrides) -> CSInventoryApiConfig:
    values = {
        "api_key": "test-key",
        "base_url": "https://csinventoryapi.test",
        "timeout_seconds": 2.0,
        "market": "buff163",
        "price_endpoint_template": "/quote/{market_name}",
        "auth_header_name": "X-API-Key",
        "auth_query_param_name": None,
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


def _synthetic_payload() -> dict:
    path = Path(__file__).parent / "fixtures" / "csinventoryapi" / "synthetic_success.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _provider(handler, synthetic: bool = False):
    provider_type = SyntheticCSInventoryApiProvider if synthetic else CSInventoryApiProvider
    return provider_type(config=_config(), items=_item_refs(), transport=httpx.MockTransport(handler))


def test_real_provider_raises_schema_not_configured_after_decoding_json():
    provider = _provider(lambda request: httpx.Response(200, json=_synthetic_payload()))

    with pytest.raises(ApiSchemaNotConfiguredError, match="schema is not configured"):
        provider.get_quote(1)


def test_synthetic_success_response_maps_to_price_quote():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "test-key"
        assert str(request.url).endswith("AK-47%20%7C%20Redline%20%28Field-Tested%29")
        return httpx.Response(200, json=_synthetic_payload())

    quote = _provider(handler, synthetic=True).get_quote(1)

    assert quote.provider == "csinventoryapi:buff163"
    assert quote.lowest_listing_price == Decimal("123.45")
    assert quote.highest_buy_order_price == Decimal("120.00")
    assert quote.listing_count == 17
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
    provider = _provider(lambda request: httpx.Response(200, json=_synthetic_payload()))

    with pytest.raises(ApiConfigurationError, match="Item 999"):
        provider.get_quote(999)


def test_missing_price_fields_remain_none_in_synthetic_adapter():
    payload = _synthetic_payload()
    del payload["synthetic_quote"]["lowest_listing_price"]
    del payload["synthetic_quote"]["listing_count"]
    provider = _provider(lambda request: httpx.Response(200, json=payload), synthetic=True)

    quote = provider.get_quote(1)

    assert quote.lowest_listing_price is None
    assert quote.listing_count is None
    assert quote.highest_buy_order_price == Decimal("120.00")


def test_missing_currency_is_schema_error():
    payload = _synthetic_payload()
    del payload["synthetic_quote"]["currency"]
    provider = _provider(lambda request: httpx.Response(200, json=payload), synthetic=True)

    with pytest.raises(ApiSchemaNotConfiguredError, match="currency"):
        provider.get_quote(1)


def test_missing_api_config_is_reported():
    config = _config(api_key=None, base_url="", price_endpoint_template=None)

    with pytest.raises(ApiConfigurationError, match="CS2_MONITOR_CSINVENTORY_API_KEY"):
        CSInventoryApiProvider(config=config, items=_item_refs())
