from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd

from cs2_price_monitor.config import ApiProviderConfig, ProviderMode
from cs2_price_monitor.models import Alert, AlertSeverity, DailyPriceSummary, PriceSnapshot, Transaction, TransactionType
from cs2_price_monitor.providers.manual import ManualPriceProvider
from cs2_price_monitor.services.analytics import calculate_item_analysis, calculate_portfolio
from cs2_price_monitor.services.items import add_item
from cs2_price_monitor.services.prices import record_price_quote
from cs2_price_monitor.services.sync import SyncItemResult, SyncItemStatus, SyncSummary
from cs2_price_monitor.services.transactions import record_transaction
from cs2_price_monitor.ui import streamlit_app
from cs2_price_monitor.ui.streamlit_app import _latest_api_snapshot, normalize_dataframe_for_display


def test_latest_api_snapshot_ignores_manual_default_entry_values(session):
    item = add_item(session, "AK-47 | Redline (Field-Tested)")
    manual = ManualPriceProvider(
        Decimal("1"),
        Decimal("1"),
        1,
        captured_at=datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc),
    )
    record_price_quote(session, manual.get_quote(item.id))
    api_snapshot = PriceSnapshot(
        item_id=item.id,
        provider="csinventoryapi:buff163",
        lowest_listing_price=Decimal("204.02"),
        highest_buy_order_price=Decimal("199.01"),
        listing_count=9295,
        currency="CNY",
        captured_at=datetime(2026, 7, 12, 11, 0, tzinfo=timezone.utc),
        is_suspicious=False,
    )
    session.add(api_snapshot)
    session.flush()

    latest = _latest_api_snapshot(session, item.id)

    assert latest is not None
    assert latest.provider == "csinventoryapi:buff163"
    assert latest.lowest_listing_price == Decimal("204.020000")
    assert latest.listing_count == 9295


def test_item_analysis_reads_price_snapshots_for_current_price(session):
    item = add_item(session, "AK-47 | Redline (Field-Tested)")
    snapshot = PriceSnapshot(
        item_id=item.id,
        provider="csinventoryapi:buff163",
        lowest_listing_price=Decimal("204.02"),
        highest_buy_order_price=Decimal("199.01"),
        listing_count=9295,
        currency="CNY",
        captured_at=datetime(2026, 7, 12, 11, 0, tzinfo=timezone.utc),
        is_suspicious=False,
    )
    session.add(snapshot)
    session.flush()

    analysis = calculate_item_analysis(session, item.id)

    assert analysis.current_lowest_listing_price == Decimal("204.020000")
    assert analysis.current_highest_buy_order_price == Decimal("199.010000")
    assert len(analysis.price_points) == 1


def test_normalize_dataframe_for_display_removes_arrow_strings_and_mixed_objects():
    source = pd.DataFrame(
        {
            "name": pd.Series(["AK-47", None], dtype="string[pyarrow]"),
            "amount": [Decimal("12.50"), None],
            "captured_at": [datetime(2026, 7, 12, 11, 0, tzinfo=timezone.utc), pd.NaT],
            "mixed": ["x", 10],
        }
    )

    normalized = normalize_dataframe_for_display(source)

    assert str(normalized["name"].dtype) == "object"
    assert not _has_arrow_string_dtype(normalized)
    assert normalized["amount"].dtype == "Float64"
    assert normalized["amount"].iloc[0] == 12.5
    assert "datetime64" in str(normalized["captured_at"].dtype)
    assert str(normalized["mixed"].dtype) == "object"
    assert all(isinstance(value, str) for value in normalized["mixed"])


def test_pandas_string_options_prefer_python_storage():
    assert pd.get_option("mode.string_storage") == "python"
    try:
        assert pd.get_option("future.infer_string") is False
    except (KeyError, ValueError):
        pass


def test_target_pages_do_not_use_arrow_table_components(session, monkeypatch):
    fake_st = FakeStreamlit()
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    _seed_render_data(session)

    streamlit_app.render_transactions(session)
    streamlit_app.render_item_analysis(session)
    streamlit_app.render_alerts_quality(session)
    streamlit_app.render_manual_inventory(session)

    assert fake_st.forbidden_calls == []
    assert "markdown" in fake_st.calls
    assert "pyplot" in fake_st.calls


def test_target_pages_render_empty_data_without_arrow_components(session, monkeypatch):
    fake_st = FakeStreamlit()
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    add_item(session, "AK-47 | Redline (Field-Tested)")

    streamlit_app.render_transactions(session)
    streamlit_app.render_item_analysis(session)
    streamlit_app.render_alerts_quality(session)
    streamlit_app.render_manual_inventory(session)

    assert fake_st.forbidden_calls == []
    assert "info" in fake_st.calls


def test_target_pages_can_render_repeatedly_without_external_price_api(session, monkeypatch):
    fake_st = FakeStreamlit()
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    monkeypatch.setattr(
        streamlit_app,
        "sync_all_enabled_items",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("external price API called")),
    )
    _seed_render_data(session)

    for _ in range(20):
        streamlit_app.render_transactions(session)
        streamlit_app.render_item_analysis(session)
        streamlit_app.render_alerts_quality(session)
        streamlit_app.render_manual_inventory(session)

    assert fake_st.forbidden_calls == []


def test_render_sync_summary_does_not_call_dataframe(monkeypatch):
    fake_st = FakeStreamlit()
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    summary = SyncSummary(
        mode=ProviderMode.API,
        success_count=1,
        failure_count=0,
        suspicious_count=0,
        skipped_count=0,
        results=[
            SyncItemResult(
                item_id=1,
                market_name="AK-47 | Redline (Field-Tested)",
                status=SyncItemStatus.SUCCESS,
                message="Saved",
                snapshot_id=10,
                provider="csinventoryapi:buff163",
            )
        ],
    )

    streamlit_app._render_sync_summary(summary)

    assert fake_st.forbidden_calls == []
    assert "columns" in fake_st.calls
    assert "caption" in fake_st.calls


def test_render_sync_summary_safely_displays_complex_result_fields(monkeypatch):
    fake_st = FakeStreamlit()
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    summary = SyncSummary(
        mode=ProviderMode.API,
        success_count=0,
        failure_count=1,
        suspicious_count=0,
        skipped_count=0,
        results=[
            SimpleNamespace(
                item_id=1,
                market_name="AK-47 | Redline (Field-Tested)",
                status=SyncItemStatus.FAILED,
                message=RuntimeError("provider failed"),
                price=Decimal("204.02"),
                currency="CNY",
                captured_at=datetime(2026, 7, 13, 12, 30, tzinfo=timezone.utc),
            )
        ],
    )

    streamlit_app._render_sync_summary(summary)

    assert fake_st.forbidden_calls == []
    assert "RuntimeError" not in "".join(fake_st.rendered_text)
    assert any("provider failed" in value for value in fake_st.rendered_text)
    assert any("204.02" in value for value in fake_st.rendered_text)
    assert any("2026-07-13T12:30:00+00:00" in value for value in fake_st.rendered_text)


def test_render_sync_summary_empty_results(monkeypatch):
    fake_st = FakeStreamlit()
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    summary = SyncSummary(
        mode=ProviderMode.API,
        success_count=0,
        failure_count=0,
        suspicious_count=0,
        skipped_count=0,
        results=[],
    )

    streamlit_app._render_sync_summary(summary)

    assert fake_st.forbidden_calls == []
    assert "info" in fake_st.calls


def test_transaction_edit_submit_without_confirmation_shows_error(session, monkeypatch):
    item = add_item(session, "AK-47 | Redline (Field-Tested)")
    transaction = record_transaction(session, item.id, TransactionType.BUY, Decimal("1"), Decimal("100"))
    fake_st = FakeStreamlit(
        checkbox_result=False,
        submit_results={"保存修改": True},
        input_values={f"edit-qty-{transaction.id}": 2.0},
    )
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    called = False

    def fail_update(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(streamlit_app, "update_transaction", fail_update)

    streamlit_app._render_transaction_edit_form(session, transaction, [item], {item.id: item})

    assert called is False
    assert any("请先勾选确认选项" in value for value in fake_st.rendered_text)


def test_transaction_edit_submit_with_confirmation_succeeds(session, monkeypatch):
    item = add_item(session, "M4A1-S | Printstream (Field-Tested)")
    transaction = record_transaction(session, item.id, TransactionType.BUY, Decimal("2"), Decimal("100"))
    record_price_quote(
        session,
        ManualPriceProvider(Decimal("150"), Decimal("140"), 3).get_quote(item.id),
    )
    fake_st = FakeStreamlit(
        checkbox_result=True,
        submit_results={"保存修改": True},
        input_values={f"edit-qty-{transaction.id}": 3.0},
    )
    fake_st.session_state.editing_transaction_id = transaction.id
    monkeypatch.setattr(streamlit_app, "st", fake_st)

    streamlit_app._render_transaction_edit_form(session, transaction, [item], {item.id: item})

    session.expire_all()
    assert transaction.quantity == Decimal("3.000000")
    assert item.holding.quantity == Decimal("3.000000")
    assert calculate_portfolio(session)[0].listing_value == Decimal("450.000000000000")
    assert "success" in fake_st.calls


def test_transaction_edit_save_button_is_not_disabled_by_form_checkbox(session, monkeypatch):
    item = add_item(session, "Glock-18 | Fade (Factory New)")
    transaction = record_transaction(session, item.id, TransactionType.BUY, Decimal("1"), Decimal("100"))
    fake_st = FakeStreamlit(
        checkbox_result=False,
        submit_results={"保存修改": True},
        input_values={f"edit-price-{transaction.id}": 110.0},
    )
    monkeypatch.setattr(streamlit_app, "st", fake_st)

    streamlit_app._render_transaction_edit_form(session, transaction, [item], {item.id: item})

    save_kwargs = [
        kwargs
        for label, kwargs in fake_st.submit_button_calls
        if label == "保存修改"
    ]
    assert save_kwargs
    assert all("disabled" not in kwargs for kwargs in save_kwargs)


def test_transaction_edit_ignores_equivalent_naive_and_utc_aware_datetime(session):
    item = add_item(session, "USP-S | Orion (Factory New)")
    transaction = record_transaction(
        session,
        item.id,
        TransactionType.BUY,
        Decimal("1"),
        Decimal("100"),
        occurred_at=datetime(2026, 7, 14, 5, 0),
    )

    changes = streamlit_app._transaction_edit_changes(
        transaction,
        item,
        item,
        TransactionType.BUY.value.upper(),
        Decimal("1"),
        Decimal("100"),
        datetime(2026, 7, 14, 5, 0, tzinfo=timezone.utc),
        None,
    )

    assert changes == []


def test_transaction_edit_detects_real_field_changes(session):
    item = add_item(session, "Desert Eagle | Blaze (Factory New)")
    transaction = record_transaction(
        session,
        item.id,
        TransactionType.BUY,
        Decimal("1"),
        Decimal("100"),
        occurred_at=datetime(2026, 7, 14, 5, 0),
    )

    changes = streamlit_app._transaction_edit_changes(
        transaction,
        item,
        item,
        TransactionType.SELL.value.upper(),
        Decimal("2"),
        Decimal("120"),
        datetime(2026, 7, 14, 6, 0, tzinfo=timezone.utc),
        "fixed",
    )
    labels = {label for label, _, _ in changes}

    assert {"Transaction type", "Quantity", "Unit price", "Transaction time", "Notes"} <= labels


def test_transaction_edit_no_actual_change_does_not_update_database(session, monkeypatch):
    item = add_item(session, "AWP | Asiimov (Field-Tested)")
    transaction = record_transaction(session, item.id, TransactionType.BUY, Decimal("1"), Decimal("100"))
    fake_st = FakeStreamlit(checkbox_result=True, submit_results={"保存修改": True})
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    called = False

    def fail_update(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(streamlit_app, "update_transaction", fail_update)

    streamlit_app._render_transaction_edit_form(session, transaction, [item], {item.id: item})

    assert called is False
    assert any("尚未修改任何字段" in value for value in fake_st.rendered_text)


def test_new_transaction_quantity_input_defaults_to_integer_one(session, monkeypatch):
    item = add_item(session, "AK-47 | Slate (Field-Tested)")
    fake_st = FakeStreamlit()
    monkeypatch.setattr(streamlit_app, "st", fake_st)

    streamlit_app.render_transactions(session)

    quantity_inputs = [
        kwargs for label, kwargs in fake_st.number_input_calls if label == "数量"
    ]
    assert quantity_inputs
    assert any(
        kwargs.get("min_value") == 1
        and kwargs.get("value") == 1
        and kwargs.get("step") == 1
        and kwargs.get("format") == "%d"
        for kwargs in quantity_inputs
    )


def test_transaction_edit_valid_quantity_uses_integer_input(session, monkeypatch):
    item = add_item(session, "M4A1-S | Guardian (Factory New)")
    transaction = record_transaction(session, item.id, TransactionType.BUY, 2, Decimal("100"))
    fake_st = FakeStreamlit()
    monkeypatch.setattr(streamlit_app, "st", fake_st)

    streamlit_app._render_transaction_edit_form(session, transaction, [item], {item.id: item})

    quantity_inputs = [
        kwargs for label, kwargs in fake_st.number_input_calls if label == "数量"
    ]
    assert quantity_inputs
    assert quantity_inputs[0]["min_value"] == 1
    assert quantity_inputs[0]["value"] == 2
    assert quantity_inputs[0]["step"] == 1
    assert quantity_inputs[0]["format"] == "%d"


def test_transaction_edit_invalid_quantity_warns_and_requires_manual_fix(session, monkeypatch):
    item = add_item(session, "USP-S | Cortex (Field-Tested)")
    transaction = Transaction(
        item_id=item.id,
        transaction_type=TransactionType.BUY.value,
        quantity=Decimal("0.000001"),
        unit_price=Decimal("100"),
        fee=Decimal("0"),
        occurred_at=datetime(2026, 7, 14, 5, 0),
    )
    session.add(transaction)
    session.flush()
    fake_st = FakeStreamlit(
        checkbox_result=True,
        submit_results={"保存修改": True},
        input_values={f"edit-qty-{transaction.id}": ""},
    )
    monkeypatch.setattr(streamlit_app, "st", fake_st)

    streamlit_app._render_transaction_edit_form(session, transaction, [item], {item.id: item})

    assert any("历史数量 0.000001" in value for value in fake_st.rendered_text)
    assert any("交易数量必须是大于等于 1 的整数" in value for value in fake_st.rendered_text)


def test_transaction_edit_repairs_invalid_quantity_and_recalculates(session, monkeypatch):
    item = add_item(session, "Desert Eagle | Code Red (Field-Tested)")
    transaction = Transaction(
        item_id=item.id,
        transaction_type=TransactionType.BUY.value,
        quantity=Decimal("0.000001"),
        unit_price=Decimal("100"),
        fee=Decimal("0"),
        occurred_at=datetime(2026, 7, 14, 5, 0),
    )
    session.add(transaction)
    session.flush()
    record_price_quote(
        session,
        ManualPriceProvider(Decimal("150"), Decimal("140"), 3).get_quote(item.id),
    )
    fake_st = FakeStreamlit(
        checkbox_result=True,
        submit_results={"保存修改": True},
        input_values={f"edit-qty-{transaction.id}": "2"},
    )
    fake_st.session_state.editing_transaction_id = transaction.id
    monkeypatch.setattr(streamlit_app, "st", fake_st)

    streamlit_app._render_transaction_edit_form(session, transaction, [item], {item.id: item})

    session.expire_all()
    assert transaction.quantity == Decimal("2.000000")
    assert item.holding.quantity == Decimal("2.000000")
    assert calculate_portfolio(session)[0].listing_value == Decimal("300.000000000000")


def test_render_sync_summary_single_item_failure_does_not_abort_page(monkeypatch):
    fake_st = FakeStreamlit()
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    summary = SyncSummary(
        mode=ProviderMode.API,
        success_count=0,
        failure_count=1,
        suspicious_count=0,
        skipped_count=0,
        results=[
            SyncItemResult(
                item_id=1,
                market_name="AK-47 | Redline (Field-Tested)",
                status=SyncItemStatus.FAILED,
                message="not found",
            )
        ],
    )

    streamlit_app._render_sync_summary(summary)

    assert fake_st.forbidden_calls == []
    assert "error" not in fake_st.calls
    assert any("not found" in value for value in fake_st.rendered_text)


def test_sync_button_renders_summary_without_process_exit(session, monkeypatch):
    fake_st = FakeStreamlit(button_result=True)
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    settings = replace(
        streamlit_app.load_settings(),
        api=ApiProviderConfig(
            mode=ProviderMode.API,
            provider=streamlit_app.load_settings().api.provider,
        ),
    )
    summary = SyncSummary(
        mode=ProviderMode.API,
        success_count=1,
        failure_count=0,
        suspicious_count=0,
        skipped_count=0,
        results=[
            SyncItemResult(
                item_id=1,
                market_name="AK-47 | Redline (Field-Tested)",
                status=SyncItemStatus.SUCCESS,
                message="Saved",
            )
        ],
    )
    calls = {"sync": 0}

    def fake_sync(*args, **kwargs):
        calls["sync"] += 1
        return summary

    monkeypatch.setattr(streamlit_app, "load_settings", lambda: settings)
    monkeypatch.setattr(streamlit_app, "validate_api_provider_settings", lambda settings: None)
    monkeypatch.setattr(streamlit_app, "sync_all_enabled_items", fake_sync)

    streamlit_app._render_api_sync_section(session)

    assert calls["sync"] == 1
    assert fake_st.forbidden_calls == []
    assert "rerun" in fake_st.calls


def test_render_sync_summary_repeatedly_without_arrow_components(monkeypatch):
    fake_st = FakeStreamlit()
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    summary = SyncSummary(
        mode=ProviderMode.API,
        success_count=1,
        failure_count=1,
        suspicious_count=0,
        skipped_count=0,
        results=[
            SyncItemResult(1, "AK-47 | Redline (Field-Tested)", SyncItemStatus.SUCCESS, "Saved"),
            SyncItemResult(2, "M4A1-S | Printstream (Field-Tested)", SyncItemStatus.FAILED, "not found"),
        ],
    )

    for _ in range(20):
        streamlit_app._render_sync_summary(summary)

    assert fake_st.forbidden_calls == []


def test_systemd_segv_regression_path_avoids_arrow_components(monkeypatch):
    fake_st = FakeStreamlit()
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    summary = SyncSummary(
        mode=ProviderMode.API,
        success_count=1,
        failure_count=0,
        suspicious_count=0,
        skipped_count=0,
        results=[SyncItemResult(1, "AK-47 | Redline (Field-Tested)", SyncItemStatus.SUCCESS, "Saved")],
    )

    streamlit_app._render_sync_summary(summary)

    assert fake_st.forbidden_calls == []


def _has_arrow_string_dtype(frame: pd.DataFrame) -> bool:
    for dtype in frame.dtypes:
        if isinstance(dtype, pd.StringDtype) and dtype.storage == "pyarrow":
            return True
    return False


def _seed_render_data(session):
    item = add_item(session, "AK-47 | Redline (Field-Tested)")
    record_transaction(
        session,
        item.id,
        TransactionType.BUY,
        Decimal("1"),
        Decimal("100"),
        occurred_at=datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc),
        notes="seed",
    )
    for offset in range(3):
        session.add(
            PriceSnapshot(
                item_id=item.id,
                provider="manual",
                lowest_listing_price=Decimal("100") + Decimal(offset),
                highest_buy_order_price=Decimal("95") + Decimal(offset),
                listing_count=10 + offset,
                currency="CNY",
                captured_at=datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc) + timedelta(hours=offset),
                is_suspicious=False,
            )
        )
    session.add(
        PriceSnapshot(
            item_id=item.id,
            provider="manual",
            lowest_listing_price=Decimal("300"),
            highest_buy_order_price=Decimal("90"),
            listing_count=1,
            currency="CNY",
            captured_at=datetime(2026, 7, 12, 13, 0, tzinfo=timezone.utc),
            is_suspicious=True,
            suspicion_reason="synthetic test row",
        )
    )
    session.add(
        DailyPriceSummary(
            item_id=item.id,
            summary_date=date(2026, 7, 12),
            currency="CNY",
            open_price=Decimal("100"),
            high_price=Decimal("102"),
            low_price=Decimal("99"),
            close_price=Decimal("101"),
            median_price=Decimal("100.5"),
            snapshot_count=3,
        )
    )
    session.add(
        Alert(
            item_id=item.id,
            severity=AlertSeverity.WARNING.value,
            message="synthetic alert",
            created_at=datetime(2026, 7, 12, 14, 0, tzinfo=timezone.utc),
        )
    )
    session.commit()


class FakeSessionState(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class FakeStreamlit:
    def __init__(
        self,
        button_result: bool = False,
        checkbox_result: bool = False,
        submit_results: dict[str, bool] | None = None,
        input_values: dict[str, object] | None = None,
    ):
        self.calls: list[str] = []
        self.forbidden_calls: list[str] = []
        self.rendered_text: list[str] = []
        self.button_result = button_result
        self.checkbox_result = checkbox_result
        self.submit_results = submit_results or {}
        self.input_values = input_values or {}
        self.submit_button_calls: list[tuple[str, dict[str, object]]] = []
        self.number_input_calls: list[tuple[str, dict[str, object]]] = []
        self.session_state = FakeSessionState()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def _record(self, name: str):
        self.calls.append(name)

    def _record_text(self, name: str, *args):
        self._record(name)
        self.rendered_text.extend(str(arg) for arg in args)

    def dataframe(self, *args, **kwargs):
        self.forbidden_calls.append("dataframe")
        raise AssertionError("st.dataframe should not be called")

    def data_editor(self, *args, **kwargs):
        self.forbidden_calls.append("data_editor")
        raise AssertionError("st.data_editor should not be called")

    def table(self, *args, **kwargs):
        self.forbidden_calls.append("table")
        raise AssertionError("st.table should not be called")

    def line_chart(self, *args, **kwargs):
        self.forbidden_calls.append("line_chart")
        raise AssertionError("st.line_chart should not be called")

    def area_chart(self, *args, **kwargs):
        self.forbidden_calls.append("area_chart")
        raise AssertionError("st.area_chart should not be called")

    def bar_chart(self, *args, **kwargs):
        self.forbidden_calls.append("bar_chart")
        raise AssertionError("st.bar_chart should not be called")

    def subheader(self, *args, **kwargs):
        self._record_text("subheader", *args)

    def info(self, *args, **kwargs):
        self._record_text("info", *args)

    def warning(self, *args, **kwargs):
        self._record_text("warning", *args)

    def error(self, *args, **kwargs):
        self._record_text("error", *args)

    def success(self, *args, **kwargs):
        self._record_text("success", *args)

    def caption(self, *args, **kwargs):
        self._record_text("caption", *args)

    def markdown(self, *args, **kwargs):
        self._record_text("markdown", *args)

    def write(self, *args, **kwargs):
        self._record_text("write", *args)

    def metric(self, *args, **kwargs):
        self._record_text("metric", *args)

    def text(self, *args, **kwargs):
        self._record_text("text", *args)

    def code(self, *args, **kwargs):
        self._record_text("code", *args)

    def divider(self, *args, **kwargs):
        self._record("divider")

    def pyplot(self, *args, **kwargs):
        self._record("pyplot")

    def form(self, *args, **kwargs):
        self._record("form")
        return self

    def columns(self, spec, *args, **kwargs):
        self._record("columns")
        count = spec if isinstance(spec, int) else len(spec)
        return [self for _ in range(count)]

    def selectbox(self, label, options, *args, **kwargs):
        self._record("selectbox")
        key = kwargs.get("key")
        values = list(options)
        if key in self.input_values:
            return self.input_values[key]
        index = kwargs.get("index", 0)
        return values[index]

    def number_input(self, *args, **kwargs):
        self._record("number_input")
        label = str(args[0]) if args else str(kwargs.get("label", ""))
        self.number_input_calls.append((label, dict(kwargs)))
        key = kwargs.get("key")
        if key in self.input_values:
            return self.input_values[key]
        return kwargs.get("value", kwargs.get("min_value", 0))

    def date_input(self, *args, **kwargs):
        self._record("date_input")
        key = kwargs.get("key")
        if key in self.input_values:
            return self.input_values[key]
        return kwargs.get("value", date(2026, 7, 12))

    def time_input(self, *args, **kwargs):
        self._record("time_input")
        key = kwargs.get("key")
        if key in self.input_values:
            return self.input_values[key]
        return kwargs.get("value", time(12, 0))

    def text_input(self, *args, **kwargs):
        self._record("text_input")
        key = kwargs.get("key")
        if key in self.input_values:
            return self.input_values[key]
        return kwargs.get("value", "")

    def text_area(self, *args, **kwargs):
        self._record("text_area")
        key = kwargs.get("key")
        if key in self.input_values:
            return self.input_values[key]
        return kwargs.get("value", "")

    def checkbox(self, *args, **kwargs):
        self._record("checkbox")
        key = kwargs.get("key")
        if key in self.input_values:
            return bool(self.input_values[key])
        return self.checkbox_result

    def form_submit_button(self, *args, **kwargs):
        self._record("form_submit_button")
        label = str(args[0]) if args else str(kwargs.get("label", ""))
        self.submit_button_calls.append((label, dict(kwargs)))
        return self.submit_results.get(label, False)

    def file_uploader(self, *args, **kwargs):
        self._record("file_uploader")
        return None

    def button(self, *args, **kwargs):
        self._record("button")
        return self.button_result

    def rerun(self, *args, **kwargs):
        self._record("rerun")
