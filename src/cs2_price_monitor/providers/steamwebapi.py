from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from cs2_price_monitor.config import SteamWebApiConfig
from cs2_price_monitor.providers.base import PriceProvider, PriceQuote
from cs2_price_monitor.providers.errors import PriceProviderError


class SteamWebApiError(PriceProviderError):
    """Base error for SteamWebAPI provider failures."""


class SteamWebApiConfigurationError(SteamWebApiError):
    """Raised when the provider lacks required configuration."""


class SteamWebApiHttpError(SteamWebApiError):
    """Raised for non-success HTTP responses."""


class SteamWebApiParseError(SteamWebApiError):
    """Raised when a response cannot be converted into PriceQuote."""


@dataclass(frozen=True)
class SteamWebApiItemRef:
    """Item identifiers available to request templates."""

    item_id: int
    market_name: str
    buff_name: str | None = None


class SteamWebApiProvider(PriceProvider):
    """Configurable SteamWebAPI provider skeleton."""

    name = "steamwebapi"

    def __init__(
        self,
        config: SteamWebApiConfig,
        items: Mapping[int, SteamWebApiItemRef],
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        validate_steamwebapi_config(config)
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
            raise SteamWebApiConfigurationError(f"Item {item_id} is not available for API sync")
        request = self._build_request(item)
        payload = self._send_request(request)
        return self._parse_response(item_id=item_id, payload=payload)

    def _build_request(self, item: SteamWebApiItemRef) -> httpx.Request:
        """Build the HTTP request from configured templates."""
        endpoint_template = self.config.price_endpoint_template
        if not endpoint_template:
            raise SteamWebApiConfigurationError("STEAMWEBAPI_PRICE_ENDPOINT_TEMPLATE is required")

        template_values = {
            "item_id": str(item.item_id),
            "market_name": quote(item.market_name, safe=""),
            "buff_name": quote(item.buff_name or item.market_name, safe=""),
        }
        try:
            endpoint = endpoint_template.format(**template_values)
        except KeyError as exc:
            raise SteamWebApiConfigurationError(
                f"Unsupported endpoint template variable: {exc.args[0]}"
            ) from exc

        headers: dict[str, str] = {}
        params: dict[str, str] = {}
        if self.config.auth_header_name:
            headers[self.config.auth_header_name] = self.config.api_key or ""
        if self.config.auth_query_param_name:
            params[self.config.auth_query_param_name] = self.config.api_key or ""

        return self.client.build_request("GET", endpoint, headers=headers, params=params)

    def _send_request(self, request: httpx.Request) -> dict[str, Any]:
        """Send an HTTP request and return parsed JSON."""
        try:
            response = self.client.send(request)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise SteamWebApiHttpError(f"SteamWebAPI returned HTTP {status}") from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise SteamWebApiHttpError(f"SteamWebAPI request failed: {exc.__class__.__name__}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamWebApiParseError("SteamWebAPI returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SteamWebApiParseError("SteamWebAPI JSON response must be an object")
        return payload

    def _parse_response(self, item_id: int, payload: dict[str, Any]) -> PriceQuote:
        """Convert configured JSON paths into PriceQuote."""
        fields = self.config.field_paths
        lowest = _decimal_at_path(payload, fields.lowest_listing_price, "lowest listing price")
        highest_buy = _decimal_at_path(
            payload, fields.highest_buy_order_price, "highest buy order price"
        )
        listing_count = _int_at_path(payload, fields.listing_count, "listing count")
        currency = _optional_str_at_path(payload, fields.currency) or "CNY"
        captured_at = _optional_datetime_at_path(payload, fields.captured_at) or datetime.now(
            timezone.utc
        )
        return PriceQuote(
            item_id=item_id,
            provider=self.provider_name,
            lowest_listing_price=lowest,
            highest_buy_order_price=highest_buy,
            listing_count=listing_count,
            currency=currency,
            captured_at=captured_at,
            raw_payload={"synthetic_or_raw_response": payload},
        )


def validate_steamwebapi_config(config: SteamWebApiConfig) -> None:
    """Validate the minimum config needed for API mode."""
    missing: list[str] = []
    if not config.api_key:
        missing.append("STEAMWEBAPI_API_KEY")
    if not config.base_url:
        missing.append("STEAMWEBAPI_BASE_URL")
    if not config.price_endpoint_template:
        missing.append("STEAMWEBAPI_PRICE_ENDPOINT_TEMPLATE")
    if not (config.auth_header_name or config.auth_query_param_name):
        missing.append("STEAMWEBAPI_AUTH_HEADER_NAME or STEAMWEBAPI_AUTH_QUERY_PARAM_NAME")

    field_paths = config.field_paths
    required_paths = {
        "STEAMWEBAPI_FIELD_LOWEST_LISTING_PRICE": field_paths.lowest_listing_price,
        "STEAMWEBAPI_FIELD_HIGHEST_BUY_ORDER_PRICE": field_paths.highest_buy_order_price,
        "STEAMWEBAPI_FIELD_LISTING_COUNT": field_paths.listing_count,
    }
    missing.extend(name for name, value in required_paths.items() if not value)
    if missing:
        raise SteamWebApiConfigurationError(
            "Missing SteamWebAPI configuration: " + ", ".join(missing)
        )


def _value_at_path(payload: Mapping[str, Any], path: str | None, label: str) -> Any:
    """Read a dotted JSON path from an object."""
    if not path:
        raise SteamWebApiParseError(f"Missing configured JSON path for {label}")
    value: Any = payload
    for part in path.split("."):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
            continue
        raise SteamWebApiParseError(f"SteamWebAPI response missing field: {path}")
    return value


def _decimal_at_path(payload: Mapping[str, Any], path: str | None, label: str) -> Decimal:
    """Read a Decimal from a configured JSON path."""
    value = _value_at_path(payload, path, label)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SteamWebApiParseError(f"SteamWebAPI field is not a decimal: {path}") from exc


def _int_at_path(payload: Mapping[str, Any], path: str | None, label: str) -> int:
    """Read an integer from a configured JSON path."""
    value = _value_at_path(payload, path, label)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SteamWebApiParseError(f"SteamWebAPI field is not an integer: {path}") from exc


def _optional_str_at_path(payload: Mapping[str, Any], path: str | None) -> str | None:
    """Read an optional string from a configured JSON path."""
    if not path:
        return None
    value = _value_at_path(payload, path, path)
    return str(value) if value is not None else None


def _optional_datetime_at_path(payload: Mapping[str, Any], path: str | None) -> datetime | None:
    """Read an optional ISO datetime from a configured JSON path."""
    if not path:
        return None
    value = _value_at_path(payload, path, path)
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SteamWebApiParseError(f"SteamWebAPI field is not an ISO datetime: {path}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
