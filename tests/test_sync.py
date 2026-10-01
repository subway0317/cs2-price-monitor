from __future__ import annotations

from decimal import Decimal

import httpx
from sqlalchemy import select

from cs2_price_monitor.config import (
    ApiProviderConfig,
    ApiProviderName,
    AppConfig,
    CSInventoryApiConfig,
    DatabaseConfig,
    PricingConfig,
    ProviderMode,
    Settings,
    SteamWebApiConfig,
    SteamWebApiFieldPaths,
)
from cs2_price_monitor.models import DailyPriceSummary, PriceSnapshot
from cs2_price_monitor.providers.csinventoryapi import CSInventoryApiProvider
from cs2_price_monitor.providers.manual import ManualPriceProvider
from cs2_price_monitor.providers.steamwebapi import SteamWebApiProvider
from cs2_price_monitor.services.items import add_item
from cs2_price_monitor.services.prices import record_price_quote
from cs2_price_monitor.services.sync import SyncItemStatus, sync_all_enabled_items


def _settings(
    mode: ProviderMode = ProviderMode.API,
    api_key: str | None = "test-key",
    api_provider: ApiProviderName = ApiProviderName.STEAMWEBAPI,
) -> Settings:
    return Settings(
        database=DatabaseConfig(url="sqlite:///:memory:"),
        pricing=PricingConfig(
            suspicious_high_multiplier=Decimal("3"),
            suspicious_low_multiplier=Decimal("0.333333333333333333"),
        ),
        app=AppConfig(timezone="UTC", page_title="Test"),
        api=ApiProviderConfig(mode=mode, provider=api_provider),
        csinventoryapi=CSInventoryApiConfig(
            api_key=api_key,
            base_url="https://csinventoryapi.test",
            timeout_seconds=2.0,
            market="buff163",
            app_id="730",
            currency="cny",
            price_endpoint_template="/api/v2/prices/{market_name}",
            auth_header_name=None,
            auth_query_param_name="api_key",
        ),
        steamwebapi=SteamWebApiConfig(
            api_key=api_key,
            base_url="https://steamwebapi.test",
            timeout_seconds=2.0,
            provider_mode=mode,
            market="buff",
            price_endpoint_template="/quote/{market_name}",
            auth_header_name="X-API-Key",
            auth_query_param_name=None,
            field_paths=SteamWebApiFieldPaths(
                lowest_listing_price="data.lowest",
                highest_buy_order_price="data.highest_buy",
                listing_count="data.listings",
                currency="data.currency",
                captured_at=None,
            ),
        ),
    )


def test_single_item_sync_failure_does_not_stop_batch(session):
    bad = add_item(session, "Bad Item")
    good = add_item(session, "Good Item")
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("Bad%20Item"):
            return httpx.Response(500, json={"error": "synthetic"})
        return httpx.Response(
            200,
            json={
                "data": {
                    "lowest": "10",
                    "highest_buy": "9",
                    "listings": 3,
                    "currency": "CNY",
                }
            },
        )

    provider = SteamWebApiProvider(
        config=settings.steamwebapi,
        items={
            bad.id: provider_item(bad.id, bad.market_name),
            good.id: provider_item(good.id, good.market_name),
        },
        transport=httpx.MockTransport(handler),
    )

    summary = sync_all_enabled_items(session, settings, provider=provider)
    snapshots = session.scalars(select(PriceSnapshot)).all()
    daily = session.scalars(select(DailyPriceSummary)).all()

    assert summary.success_count == 1
    assert summary.failure_count == 1
    assert {result.status for result in summary.results} == {
        SyncItemStatus.SUCCESS,
        SyncItemStatus.FAILED,
    }
    assert len(snapshots) == 1
    assert snapshots[0].item_id == good.id
    assert snapshots[0].provider == "steamwebapi:buff"
    assert len(daily) == 1


def test_csinventory_partial_sync_failure_does_not_stop_batch(session):
    bad = add_item(session, "Bad CSInventory Item")
    good = add_item(session, "Good CSInventory Item")
    settings = _settings(api_provider=ApiProviderName.CSINVENTORYAPI)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("Bad CSInventory Item"):
            return httpx.Response(500, json={"error": "synthetic"})
        return httpx.Response(
            200,
            json={
                "quantity": 3,
                "sell_price_cents": {"cny": 1000},
                "buy_price_cents": {"cny": 900},
            },
        )

    provider = CSInventoryApiProvider(
        config=settings.csinventoryapi,
        items={
            bad.id: csinventory_item(bad.id, bad.market_name),
            good.id: csinventory_item(good.id, good.market_name),
        },
        transport=httpx.MockTransport(handler),
    )

    summary = sync_all_enabled_items(session, settings, provider=provider)
    snapshots = session.scalars(select(PriceSnapshot)).all()

    assert summary.success_count == 1
    assert summary.failure_count == 1
    assert snapshots[0].provider == "csinventoryapi:buff163"
    assert snapshots[0].currency == "CNY"


def test_manual_mode_returns_clear_sync_result_without_api_client(session):
    add_item(session, "Manual Only")

    summary = sync_all_enabled_items(session, _settings(mode=ProviderMode.MANUAL))

    assert summary.success_count == 0
    assert summary.failure_count == 1
    assert summary.results[0].message == "API sync is disabled in manual mode"


def test_hybrid_mode_keeps_manual_entry_available_when_api_config_is_missing(session):
    item = add_item(session, "Hybrid Manual Item")
    settings = _settings(mode=ProviderMode.HYBRID, api_key=None)
    provider = ManualPriceProvider(Decimal("50"), Decimal("45"), 2)

    snapshot = record_price_quote(session, provider.get_quote(item.id))

    assert settings.api.mode == ProviderMode.HYBRID
    assert snapshot.provider == "manual"
    assert snapshot.lowest_listing_price == Decimal("50.000000")


def provider_item(item_id: int, market_name: str):
    from cs2_price_monitor.providers.steamwebapi import SteamWebApiItemRef

    return SteamWebApiItemRef(item_id=item_id, market_name=market_name)


def csinventory_item(item_id: int, market_name: str):
    from cs2_price_monitor.providers.csinventoryapi import CSInventoryApiItemRef

    return CSInventoryApiItemRef(item_id=item_id, market_name=market_name)
