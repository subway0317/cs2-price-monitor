from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class DatabaseConfig:
    url: str


@dataclass(frozen=True)
class PricingConfig:
    suspicious_high_multiplier: Decimal
    suspicious_low_multiplier: Decimal


@dataclass(frozen=True)
class AppConfig:
    timezone: str
    page_title: str


class ProviderMode(StrEnum):
    """Supported price source modes."""

    MANUAL = "manual"
    API = "api"
    HYBRID = "hybrid"


class ApiProviderName(StrEnum):
    """Supported external API providers."""

    CSINVENTORYAPI = "csinventoryapi"
    STEAMWEBAPI = "steamwebapi"


@dataclass(frozen=True)
class ApiProviderConfig:
    """Generic external API provider selection."""

    mode: ProviderMode
    provider: ApiProviderName


@dataclass(frozen=True)
class SteamWebApiFieldPaths:
    """JSON field paths for response adaptation."""

    lowest_listing_price: str | None
    highest_buy_order_price: str | None
    listing_count: str | None
    currency: str | None
    captured_at: str | None


@dataclass(frozen=True)
class SteamWebApiConfig:
    """SteamWebAPI configuration."""

    api_key: str | None
    base_url: str
    timeout_seconds: float
    provider_mode: ProviderMode
    market: str
    price_endpoint_template: str | None
    auth_header_name: str | None
    auth_query_param_name: str | None
    field_paths: SteamWebApiFieldPaths


@dataclass(frozen=True)
class CSInventoryApiConfig:
    """CSInventoryAPI configuration."""

    api_key: str | None
    base_url: str
    timeout_seconds: float
    market: str
    price_endpoint_template: str | None
    auth_header_name: str | None
    auth_query_param_name: str | None


@dataclass(frozen=True)
class Settings:
    database: DatabaseConfig
    pricing: PricingConfig
    app: AppConfig
    api: ApiProviderConfig
    csinventoryapi: CSInventoryApiConfig
    steamwebapi: SteamWebApiConfig


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    return loaded


def load_settings() -> Settings:
    """Load YAML and .env config."""
    load_dotenv()
    config_path = Path(os.getenv("CS2_MONITOR_CONFIG", "config/app.yaml"))
    raw = _read_yaml(config_path)

    database_url = os.getenv(
        "CS2_MONITOR_DATABASE_URL",
        raw.get("database", {}).get("url", "sqlite:///data/cs2_monitor.sqlite3"),
    )
    pricing = raw.get("pricing", {})
    app = raw.get("app", {})
    api = raw.get("api", {})
    csinventoryapi = raw.get("csinventoryapi", {})
    steamwebapi = raw.get("steamwebapi", {})
    provider_mode = ProviderMode(
        os.getenv(
            "CS2_MONITOR_PRICE_MODE",
            os.getenv(
                "CS2_PRICE_PROVIDER_MODE",
                str(api.get("mode", ProviderMode.MANUAL.value)),
            ),
        )
    )
    api_provider = ApiProviderName(
        os.getenv(
            "CS2_MONITOR_API_PROVIDER",
            str(api.get("provider", ApiProviderName.CSINVENTORYAPI.value)),
        )
    )

    return Settings(
        database=DatabaseConfig(url=database_url),
        pricing=PricingConfig(
            suspicious_high_multiplier=Decimal(
                str(pricing.get("suspicious_high_multiplier", "3"))
            ),
            suspicious_low_multiplier=Decimal(
                str(pricing.get("suspicious_low_multiplier", "0.333333333333333333"))
            ),
        ),
        app=AppConfig(
            timezone=str(app.get("timezone", "UTC")),
            page_title=str(app.get("page_title", "CS2 Portfolio Monitor")),
        ),
        api=ApiProviderConfig(mode=provider_mode, provider=api_provider),
        csinventoryapi=CSInventoryApiConfig(
            api_key=os.getenv("CS2_MONITOR_CSINVENTORY_API_KEY") or None,
            base_url=os.getenv(
                "CS2_MONITOR_CSINVENTORY_BASE_URL",
                str(csinventoryapi.get("base_url", "")),
            ).rstrip("/"),
            timeout_seconds=float(
                os.getenv(
                    "CS2_MONITOR_CSINVENTORY_TIMEOUT_SECONDS",
                    str(csinventoryapi.get("timeout_seconds", "10")),
                )
            ),
            market=os.getenv(
                "CS2_MONITOR_CSINVENTORY_MARKET",
                str(csinventoryapi.get("market", "buff163")),
            ),
            price_endpoint_template=os.getenv("CS2_MONITOR_CSINVENTORY_PRICE_ENDPOINT_TEMPLATE")
            or csinventoryapi.get("price_endpoint_template")
            or None,
            auth_header_name=os.getenv("CS2_MONITOR_CSINVENTORY_AUTH_HEADER_NAME")
            or csinventoryapi.get("auth_header_name")
            or None,
            auth_query_param_name=os.getenv("CS2_MONITOR_CSINVENTORY_AUTH_QUERY_PARAM_NAME")
            or csinventoryapi.get("auth_query_param_name")
            or None,
        ),
        steamwebapi=SteamWebApiConfig(
            api_key=os.getenv("STEAMWEBAPI_API_KEY") or None,
            base_url=os.getenv(
                "STEAMWEBAPI_BASE_URL",
                str(steamwebapi.get("base_url", "")),
            ).rstrip("/"),
            timeout_seconds=float(
                os.getenv(
                    "STEAMWEBAPI_TIMEOUT_SECONDS",
                    str(steamwebapi.get("timeout_seconds", "10")),
                )
            ),
            provider_mode=provider_mode,
            market=os.getenv(
                "STEAMWEBAPI_MARKET",
                str(steamwebapi.get("market", "buff")),
            ),
            price_endpoint_template=os.getenv("STEAMWEBAPI_PRICE_ENDPOINT_TEMPLATE")
            or steamwebapi.get("price_endpoint_template")
            or None,
            auth_header_name=os.getenv("STEAMWEBAPI_AUTH_HEADER_NAME")
            or steamwebapi.get("auth_header_name")
            or None,
            auth_query_param_name=os.getenv("STEAMWEBAPI_AUTH_QUERY_PARAM_NAME")
            or steamwebapi.get("auth_query_param_name")
            or None,
            field_paths=SteamWebApiFieldPaths(
                lowest_listing_price=os.getenv("STEAMWEBAPI_FIELD_LOWEST_LISTING_PRICE")
                or None,
                highest_buy_order_price=os.getenv("STEAMWEBAPI_FIELD_HIGHEST_BUY_ORDER_PRICE")
                or None,
                listing_count=os.getenv("STEAMWEBAPI_FIELD_LISTING_COUNT") or None,
                currency=os.getenv("STEAMWEBAPI_FIELD_CURRENCY") or None,
                captured_at=os.getenv("STEAMWEBAPI_FIELD_CAPTURED_AT") or None,
            ),
        ),
    )
