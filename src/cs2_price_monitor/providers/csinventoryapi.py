from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from cs2_price_monitor.config import CSInventoryApiConfig
from cs2_price_monitor.providers.base import PriceProvider, PriceQuote
from cs2_price_monitor.providers.errors import (
    ApiConfigurationError,
    ApiDecodeError,
    ApiHttpError,
    ApiSchemaNotConfiguredError,
)


@dataclass(frozen=True)
class CSInventoryApiItemRef:
    """Item identifiers available to request templates."""

    item_id: int
    market_name: str
    buff_name: str | None = None


class CSInventoryApiProvider(PriceProvider):
    """Configurable CSInventoryAPI provider skeleton."""

    name = "csinventoryapi"

    def __init__(
        self,
        config: CSInventoryApiConfig,
        items: Mapping[int, CSInventoryApiItemRef],
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        validate_csinventoryapi_config(config)
        self.config = config
        self.items = dict(items)
        self.provider_name = f"{self.name}:{config.market}"
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client:
            self.client.close()

    def get_quote(self, item_id: int) -> PriceQuote:
        """Fetch and normalize one quote."""
        item = self.items.get(item_id)
        if item is None:
            raise ApiConfigurationError(f"Item {item_id} is not available for API sync")
        request = self._build_request(item)
        response = self._send_request(request)
        payload = self._decode_response(response)
        return self._map_quote(item_id=item_id, payload=payload)

    def _build_request(self, item: CSInventoryApiItemRef) -> httpx.Request:
        """Build the HTTP request from configured templates."""
        endpoint_template = self.config.price_endpoint_template
        if not endpoint_template:
            raise ApiConfigurationError(
                "CS2_MONITOR_CSINVENTORY_PRICE_ENDPOINT_TEMPLATE is required"
            )

        template_values = {
            "item_id": str(item.item_id),
            "market_name": quote(item.market_name, safe=""),
            "buff_name": quote(item.buff_name or item.market_name, safe=""),
        }
        try:
            endpoint = endpoint_template.format(**template_values)
        except KeyError as exc:
            raise ApiConfigurationError(
                f"Unsupported endpoint template variable: {exc.args[0]}"
            ) from exc

        headers: dict[str, str] = {}
        params: dict[str, str] = {}
        if self.config.auth_header_name:
            headers[self.config.auth_header_name] = self.config.api_key or ""
        if self.config.auth_query_param_name:
            params[self.config.auth_query_param_name] = self.config.api_key or ""
        return self.client.build_request("GET", endpoint, headers=headers, params=params)

    def _send_request(self, request: httpx.Request) -> httpx.Response:
        """Send the HTTP request."""
        try:
            response = self.client.send(request)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise ApiHttpError(f"CSInventoryAPI returned HTTP {status}") from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ApiHttpError(f"CSInventoryAPI request failed: {exc.__class__.__name__}") from exc

    def _decode_response(self, response: httpx.Response) -> dict[str, Any]:
        """Decode the API response as a JSON object."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiDecodeError("CSInventoryAPI returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ApiDecodeError("CSInventoryAPI JSON response must be an object")
        return payload

    def _map_quote(self, item_id: int, payload: dict[str, Any]) -> PriceQuote:
        """Map decoded JSON into PriceQuote after the real schema is known."""
        raise ApiSchemaNotConfiguredError(
            "CSInventoryAPI response schema is not configured. Provide sanitized real JSON "
            "and official field documentation before mapping PriceQuote fields."
        )


class SyntheticCSInventoryApiProvider(CSInventoryApiProvider):
    """Synthetic adapter used only by tests."""

    def _map_quote(self, item_id: int, payload: dict[str, Any]) -> PriceQuote:
        """Map the synthetic fixture into PriceQuote."""
        data = payload.get("synthetic_quote")
        if not isinstance(data, Mapping):
            raise ApiSchemaNotConfiguredError("Synthetic fixture missing synthetic_quote")
        if "currency" not in data:
            raise ApiSchemaNotConfiguredError("Synthetic fixture missing currency")
        return PriceQuote(
            item_id=item_id,
            provider=self.provider_name,
            lowest_listing_price=(
                Decimal(str(data["lowest_listing_price"]))
                if data.get("lowest_listing_price") is not None
                else None
            ),
            highest_buy_order_price=(
                Decimal(str(data["highest_buy_order_price"]))
                if data.get("highest_buy_order_price") is not None
                else None
            ),
            listing_count=(
                int(data["listing_count"]) if data.get("listing_count") is not None else None
            ),
            currency=str(data["currency"]),
            captured_at=datetime.now(timezone.utc),
            raw_payload={"synthetic_response": payload},
        )


def validate_csinventoryapi_config(config: CSInventoryApiConfig) -> None:
    """Validate the minimum config needed to call CSInventoryAPI."""
    missing: list[str] = []
    if not config.api_key:
        missing.append("CS2_MONITOR_CSINVENTORY_API_KEY")
    if not config.base_url:
        missing.append("CS2_MONITOR_CSINVENTORY_BASE_URL")
    if not config.price_endpoint_template:
        missing.append("CS2_MONITOR_CSINVENTORY_PRICE_ENDPOINT_TEMPLATE")
    if not (config.auth_header_name or config.auth_query_param_name):
        missing.append(
            "CS2_MONITOR_CSINVENTORY_AUTH_HEADER_NAME or "
            "CS2_MONITOR_CSINVENTORY_AUTH_QUERY_PARAM_NAME"
        )
    if missing:
        raise ApiConfigurationError(
            "Missing CSInventoryAPI configuration: " + ", ".join(missing)
        )
