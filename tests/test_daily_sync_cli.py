from __future__ import annotations

import fcntl
import json
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from cs2_price_monitor.cli import main
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
from cs2_price_monitor.models import PriceSnapshot
from cs2_price_monitor.providers.base import PriceProvider, PriceQuote
from cs2_price_monitor.providers.errors import ApiHttpError
from cs2_price_monitor.services.items import add_item, set_item_active
from cs2_price_monitor.services.sync import sync_all_enabled_items


class FakeProvider(PriceProvider):
    """Configurable provider for one-shot sync tests."""

    name = "fake"

    def __init__(self, failures: set[int] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[int] = []

    def get_quote(self, item_id: int) -> PriceQuote:
        self.calls.append(item_id)
        if item_id in self.failures:
            raise ApiHttpError("CSInventoryAPI returned HTTP 500")
        return PriceQuote(
            item_id=item_id,
            provider="fake:test",
            lowest_listing_price=Decimal("12.34"),
            highest_buy_order_price=Decimal("11.11"),
            listing_count=7,
            currency="CNY",
            captured_at=datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
            raw_payload={"source": "test"},
        )


class FlakyProvider(FakeProvider):
    """Provider that succeeds after transient errors."""

    def __init__(self, fail_times: int) -> None:
        super().__init__()
        self.fail_times = fail_times

    def get_quote(self, item_id: int) -> PriceQuote:
        if len(self.calls) < self.fail_times:
            self.calls.append(item_id)
            raise ApiHttpError("CSInventoryAPI returned HTTP 429")
        return super().get_quote(item_id)


def test_cli_sync_all_empty_watchlist_exits_zero_from_outside_root(tmp_path, monkeypatch):
    db_path = tmp_path / "empty.sqlite3"
    status_path = tmp_path / "status.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CS2_MONITOR_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("CS2_MONITOR_PRICE_MODE", "manual")
    monkeypatch.setenv("CS2_MONITOR_CSINVENTORY_API_KEY", "SECRET-KEY")

    exit_code = main(
        [
            "sync-all",
            "--status-file",
            str(status_path),
            "--retry-base-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["total_items"] == 0
    assert payload["successful_items"] == 0
    assert "SECRET-KEY" not in status_path.read_text(encoding="utf-8")


def test_sync_only_enabled_monitored_items_and_not_catalog(session):
    enabled = add_item(session, "Enabled Item")
    disabled = add_item(session, "Disabled Item")
    set_item_active(session, disabled.id, False)
    provider = FakeProvider()

    summary = sync_all_enabled_items(session, _settings(), provider=provider)

    assert provider.calls == [enabled.id]
    assert summary.success_count == 1


def test_single_failure_does_not_write_zero_or_stop_success(session):
    failed = add_item(session, "Failed Item")
    good = add_item(session, "Good Item")
    provider = FakeProvider(failures={failed.id})

    summary = sync_all_enabled_items(session, _settings(), provider=provider)
    snapshots = session.scalars(select(PriceSnapshot).order_by(PriceSnapshot.item_id)).all()

    assert summary.success_count == 1
    assert summary.failure_count == 1
    assert len(snapshots) == 1
    assert snapshots[0].item_id == good.id
    assert snapshots[0].lowest_listing_price == Decimal("12.340000")
    assert snapshots[0].lowest_listing_price != Decimal("0")


def test_transient_errors_are_retried_before_success(session):
    item = add_item(session, "Retry Item")
    provider = FlakyProvider(fail_times=2)

    summary = sync_all_enabled_items(
        session,
        _settings(),
        provider=provider,
        max_retries=3,
        retry_base_seconds=0,
    )

    assert provider.calls == [item.id, item.id, item.id]
    assert summary.success_count == 1


def test_non_transient_error_is_not_retried(session):
    item = add_item(session, "Bad Schema Item")

    class BadSchemaProvider(FakeProvider):
        def get_quote(self, item_id: int) -> PriceQuote:
            self.calls.append(item_id)
            from cs2_price_monitor.providers.errors import ApiDecodeError

            raise ApiDecodeError("invalid JSON")

    provider = BadSchemaProvider()
    summary = sync_all_enabled_items(session, _settings(), provider=provider, max_retries=3)

    assert provider.calls == [item.id]
    assert summary.failure_count == 1


def test_daily_sync_script_uses_lock_to_prevent_parallel_runs():
    project_dir = Path("/home/zbw21/projects/cs2-price-monitor")
    lock_path = project_dir / "logs" / "daily_sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = subprocess.run(
                [str(project_dir / "scripts" / "daily_sync.sh")],
                cwd="/tmp",
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

    assert result.returncode == 75


def test_daily_sync_script_does_not_log_api_key_literal():
    script = Path("/home/zbw21/projects/cs2-price-monitor/scripts/daily_sync.sh")
    text = script.read_text(encoding="utf-8")

    assert "CS2_MONITOR_CSINVENTORY_API_KEY" not in text
    assert "api_key" not in text


def test_streamlit_not_running_is_not_required_for_service_sync(session):
    item = add_item(session, "No Streamlit Item")

    summary = sync_all_enabled_items(session, _settings(), provider=FakeProvider())
    snapshot = session.scalar(select(PriceSnapshot).where(PriceSnapshot.item_id == item.id))

    assert summary.success_count == 1
    assert snapshot is not None


def _settings(mode: ProviderMode = ProviderMode.API) -> Settings:
    """Build test settings without reading .env."""
    return Settings(
        database=DatabaseConfig(url="sqlite:///:memory:"),
        pricing=PricingConfig(
            suspicious_high_multiplier=Decimal("3"),
            suspicious_low_multiplier=Decimal("0.333333333333333333"),
        ),
        app=AppConfig(timezone="UTC", page_title="Test"),
        api=ApiProviderConfig(mode=mode, provider=ApiProviderName.CSINVENTORYAPI),
        csinventoryapi=CSInventoryApiConfig(
            api_key="test-key",
            base_url="https://example.invalid",
            timeout_seconds=1,
            market="buff163",
            app_id="730",
            currency="cny",
            price_endpoint_template="/api/v2/prices/{market_name}",
            auth_header_name=None,
            auth_query_param_name="api_key",
        ),
        steamwebapi=SteamWebApiConfig(
            api_key="test-key",
            base_url="https://example.invalid",
            timeout_seconds=1,
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
