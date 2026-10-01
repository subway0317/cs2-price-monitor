from __future__ import annotations

from dataclasses import is_dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import select

from cs2_price_monitor.catalog import ItemInfo, search_items
from cs2_price_monitor.config import ProviderMode, load_settings
from cs2_price_monitor.db import build_engine, build_session_factory, init_db
from cs2_price_monitor.models import Alert, DailyPriceSummary, Item, ManualHolding, PriceSnapshot, Transaction, TransactionType
from cs2_price_monitor.providers.manual import ManualPriceProvider
from cs2_price_monitor.providers.errors import PriceProviderError
from cs2_price_monitor.providers.factory import validate_api_provider_settings
from cs2_price_monitor.services.analytics import ItemAnalysis, calculate_item_analysis, calculate_portfolio, portfolio_dataframe
from cs2_price_monitor.services.automatic_sync import read_automatic_sync_status
from cs2_price_monitor.services.import_export import export_price_history, import_price_csv, import_transaction_csv
from cs2_price_monitor.services.items import (
    add_item,
    delete_item_with_related_data,
    list_enabled_items,
    list_items,
    set_item_active,
    update_item_metadata,
)
from cs2_price_monitor.services.manual_holdings import (
    convert_manual_holding_to_buy_transaction,
    create_manual_holding_from_catalog,
    list_manual_holdings,
    set_manual_holding_active,
    update_manual_holding,
)
from cs2_price_monitor.services.migrations import migrate_sqlite_schema
from cs2_price_monitor.services.prices import record_price_quote, regenerate_daily_summaries
from cs2_price_monitor.services.sync import SyncSummary, sync_all_enabled_items
from cs2_price_monitor.services.transactions import (
    delete_transaction,
    get_transactions,
    is_valid_transaction_quantity,
    parse_transaction_quantity,
    record_transaction,
    update_transaction,
)


TRANSACTION_DISPLAY_LIMIT = 200
ITEM_ANALYSIS_SNAPSHOT_LIMIT = 1000
ALERT_DISPLAY_LIMIT = 200
SUSPICIOUS_SNAPSHOT_DISPLAY_LIMIT = 200

logger = logging.getLogger(__name__)


def _configure_pandas_for_python_strings() -> None:
    """Prefer Python-backed strings to avoid PyArrow string arrays."""
    try:
        pd.options.mode.string_storage = "python"
    except (AttributeError, KeyError, ValueError, OptionError):
        pass
    try:
        pd.set_option("future.infer_string", False)
    except (AttributeError, KeyError, ValueError, OptionError):
        pass


try:
    from pandas._config.config import OptionError
except ImportError:  # pragma: no cover - compatibility with older pandas
    OptionError = KeyError


_configure_pandas_for_python_strings()


@st.cache_resource
def _session_factory():
    settings = load_settings()
    engine = build_engine(settings.database.url)
    init_db(engine)
    migrate_sqlite_schema(engine)
    return build_session_factory(engine)


def _to_decimal(value: object) -> Decimal:
    """Convert UI value to Decimal."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid decimal value: {value}") from exc


def _optional_decimal(value: object) -> Decimal | None:
    """Convert an optional UI value to Decimal."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _to_decimal(text)


def normalize_dataframe_for_display(df: pd.DataFrame) -> pd.DataFrame:
    """Return a display-only DataFrame without Arrow-backed string columns."""
    _configure_pandas_for_python_strings()
    normalized = pd.DataFrame(index=df.index.copy())
    for column in df.columns:
        values = list(df[column])
        normalized[column] = _normalize_display_column(values)
    return normalized.reset_index(drop=True)


def _normalize_display_column(values: list[object]) -> pd.Series:
    """Normalize one column to a stable display dtype."""
    non_missing = [value for value in values if not _is_missing_value(value)]
    if not non_missing:
        return pd.Series(["" for _ in values], dtype=object)
    if all(isinstance(value, bool) for value in non_missing):
        return pd.Series([None if _is_missing_value(value) else bool(value) for value in values], dtype="boolean")
    if all(isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) for value in non_missing):
        return pd.Series([float(value) if not _is_missing_value(value) else pd.NA for value in values], dtype="Float64")
    if all(isinstance(value, (date, datetime, pd.Timestamp)) for value in non_missing):
        return pd.to_datetime(pd.Series([None if _is_missing_value(value) else value for value in values]), errors="coerce")
    return pd.Series(["" if _is_missing_value(value) else str(value) for value in values], dtype=object)


def _is_missing_value(value: object) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _render_dataframe_html(df: pd.DataFrame, *, max_rows: int, empty_message: str) -> None:
    """Render a DataFrame as static HTML so Streamlit does not serialize it via Arrow."""
    display = normalize_dataframe_for_display(df)
    if display.empty:
        st.info(empty_message)
        return
    clipped = display.head(max_rows)
    if len(display) > max_rows:
        st.caption(f"仅显示最近 {max_rows} 条，共 {len(display)} 条。")
    html = clipped.to_html(index=False, escape=True, na_rep="", classes="safe-display-table")
    st.markdown(
        """
<style>
table.safe-display-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9rem;
}
table.safe-display-table th,
table.safe-display-table td {
  border-bottom: 1px solid rgba(49, 51, 63, 0.18);
  padding: 0.4rem 0.5rem;
  text-align: left;
  vertical-align: top;
}
table.safe-display-table th {
  font-weight: 600;
}
</style>
"""
        + html,
        unsafe_allow_html=True,
    )


def main() -> None:
    settings = load_settings()
    st.set_page_config(page_title=settings.app.page_title, layout="wide")
    st.title(settings.app.page_title)

    page = st.sidebar.radio(
        "页面",
        ["投资组合总览", "手动库存管理", "饰品管理", "每日价格录入", "交易记录管理", "单品价格分析", "告警和数据质量"],
    )
    factory = _session_factory()
    with factory() as session:
        if page == "投资组合总览":
            render_portfolio_overview(session)
        elif page == "手动库存管理":
            render_manual_inventory(session)
        elif page == "饰品管理":
            render_item_management(session)
        elif page == "每日价格录入":
            render_daily_price_entry(session)
        elif page == "交易记录管理":
            render_transactions(session)
        elif page == "单品价格分析":
            render_item_analysis(session)
        else:
            render_alerts_quality(session)


def render_portfolio_overview(session) -> None:
    st.subheader("投资组合总览")
    items = session.scalars(select(Item).order_by(Item.market_name)).all()
    if "show_quick_add_item" not in st.session_state:
        st.session_state.show_quick_add_item = False

    top_cols = st.columns([1, 4])
    if top_cols[0].button("添加新饰品", type="primary", use_container_width=True):
        st.session_state.show_quick_add_item = not st.session_state.show_quick_add_item

    if not items:
        st.warning("尚未添加饰品，请先添加第一个饰品")
        if st.button("添加饰品", type="primary"):
            st.session_state.show_quick_add_item = True

    if st.session_state.show_quick_add_item:
        _render_add_item_section(session)

    regenerate_daily_summaries(session)
    session.commit()
    rows = calculate_portfolio(session)
    frame = portfolio_dataframe(rows)

    if frame.empty:
        if items:
            st.info("暂无持仓或价格记录。")
        return

    total_listing = sum(Decimal(str(value)) for value in frame["listing_value"])
    total_quick = sum(Decimal(str(value)) for value in frame["quick_sell_value"])
    total_profit = sum(
        Decimal(str(value))
        for value in frame["unrealized_profit"]
        if value is not None and not pd.isna(value)
    )
    col1, col2, col3 = st.columns(3)
    col1.metric("挂单估值", f"{total_listing:.2f}")
    col2.metric("快速变现估值", f"{total_quick:.2f}")
    col3.metric("未实现收益", f"{total_profit:.2f}")

    display = frame.copy()
    display["position"] = display.apply(
        lambda row: row["market_name"] if not row.get("version") else f"{row['market_name']} | {row['version']}",
        axis=1,
    )
    display["avg_cost"] = display["avg_cost"].apply(lambda value: "未知" if value is None else f"{Decimal(str(value)):.2f}")
    display["known_cost"] = display["known_cost"].apply(lambda value: "未知" if value is None else f"{Decimal(str(value)):.2f}")
    display["unrealized_profit"] = display["unrealized_profit"].apply(
        lambda value: "未知" if value is None else f"{Decimal(str(value)):.2f}"
    )
    display["non_calculable_value"] = display["non_calculable_value"].apply(
        lambda value: f"{Decimal(str(value)):.2f}"
    )
    for column in ["change_24h", "change_7d", "change_30d"]:
        display[column] = display[column].apply(lambda x: None if x is None else f"{Decimal(str(x)) * 100:.2f}%")
    ordered_columns = [
        "position",
        "transaction_quantity",
        "manual_quantity",
        "quantity",
        "listing_price",
        "listing_value",
        "known_cost",
        "unknown_cost_quantity",
        "unrealized_profit",
        "non_calculable_value",
        "currency",
        "change_24h",
        "change_7d",
        "change_30d",
    ]
    display = display[[column for column in ordered_columns if column in display.columns]]
    display = display.rename(
        columns={
            "position": "饰品",
            "transaction_quantity": "交易持仓数量",
            "manual_quantity": "手动期初持仓数量",
            "quantity": "总持仓数量",
            "listing_price": "当前价格",
            "listing_value": "当前市值",
            "known_cost": "已知成本",
            "unknown_cost_quantity": "成本未知数量",
            "unrealized_profit": "可计算收益",
            "non_calculable_value": "不可计算收益部分市值",
            "currency": "币种",
            "change_24h": "24小时涨跌幅",
            "change_7d": "7日涨跌幅",
            "change_30d": "30日涨跌幅",
        }
    )
    _render_dataframe_html(display, max_rows=len(display), empty_message="暂无持仓或价格记录。")


def render_manual_inventory(session) -> None:
    st.subheader("手动库存管理")
    _render_add_manual_holding_section(session)

    st.divider()
    st.subheader("现有手动期初持仓")
    holdings = list_manual_holdings(session, include_inactive=True)
    if not holdings:
        st.info("暂无手动期初持仓。")
        return

    items = {item.id: item for item in list_items(session)}
    _render_manual_holding_header()
    for holding in holdings:
        item = items.get(holding.item_id)
        _render_manual_holding_row(session, holding, item)


def _render_add_manual_holding_section(session) -> None:
    """Render controls for adding an opening inventory row."""
    st.subheader("添加现有持仓")
    keyword = st.text_input("Catalog 饰品搜索", key="manual-catalog-search-keyword")
    if len(keyword.strip()) < 2:
        st.info("请输入至少 2 个字符，可搜索 market_hash_name、中文名、aliases 或 version。")
        return

    matches = search_items(keyword, limit=20)
    if not matches:
        st.info("未找到匹配的 Catalog 物品。")
        return

    selected_item = _select_catalog_item(matches, _monitored_market_names(session))
    if selected_item is None:
        return
    _render_catalog_item_details(selected_item, selected_item.market_hash_name in _monitored_market_names(session))

    with st.form("manual-holding-add"):
        cols = st.columns([1, 1, 1, 1])
        quantity = cols[0].number_input("数量", min_value=0.000001, step=1.0, format="%.6f")
        unit_cost_text = cols[1].text_input("单件成本，可选", value="")
        has_acquired_date = cols[2].checkbox("填写获得日期", value=False)
        add_to_monitoring = cols[3].checkbox("加入价格监控", value=True)
        acquired_date = st.date_input("获得日期", value=date.today()) if has_acquired_date else None
        source = st.text_input("来源", value="steam_manual")
        note = st.text_area("备注")
        submitted = st.form_submit_button("保存手动持仓")
        if submitted:
            try:
                create_manual_holding_from_catalog(
                    session,
                    selected_item,
                    quantity=_to_decimal(quantity),
                    unit_cost=_optional_decimal(unit_cost_text),
                    acquired_at=(
                        datetime.combine(acquired_date, time(12, 0), tzinfo=timezone.utc)
                        if acquired_date is not None
                        else None
                    ),
                    source=source,
                    note=note or None,
                    add_to_monitoring=add_to_monitoring,
                )
                session.commit()
                st.success("手动期初持仓已保存，未创建交易记录。")
                st.rerun()
            except ValueError as exc:
                session.rollback()
                st.error(str(exc))


def _render_manual_holding_header() -> None:
    """Render the manual holdings list header."""
    header = st.columns([3, 1, 1, 1, 1, 1, 1])
    header[0].markdown("**饰品**")
    header[1].markdown("**Version**")
    header[2].markdown("**数量**")
    header[3].markdown("**单件成本**")
    header[4].markdown("**获得日期**")
    header[5].markdown("**来源**")
    header[6].markdown("**状态**")


def _render_manual_holding_row(session, holding: ManualHolding, item: Item | None) -> None:
    """Render one editable manual holding row without Arrow-backed components."""
    with st.container(border=True):
        cols = st.columns([3, 1, 1, 1, 1, 1, 1])
        cols[0].markdown(_safe_display_text(item.market_name if item else f"item_id={holding.item_id}"))
        cols[0].caption(_safe_display_text(holding.catalog_id))
        cols[1].caption(_safe_display_text(holding.version) or "无")
        cols[2].caption(f"{Decimal(holding.quantity):.6f}")
        cols[3].caption("未知" if holding.unit_cost is None else f"{Decimal(holding.unit_cost):.2f}")
        cols[4].caption(_safe_display_text(holding.acquired_at) or "未知")
        cols[5].caption(_safe_display_text(holding.source))
        cols[6].caption("有效" if holding.is_active else "已停用")

        with st.form(f"manual-holding-edit-{holding.id}"):
            edit_cols = st.columns([1, 1, 1, 1, 2])
            quantity = edit_cols[0].number_input(
                "数量",
                min_value=0.000001,
                step=1.0,
                format="%.6f",
                value=float(holding.quantity),
                key=f"manual-qty-{holding.id}",
            )
            unit_cost = edit_cols[1].text_input(
                "单件成本，可选",
                value="" if holding.unit_cost is None else f"{Decimal(holding.unit_cost):.2f}",
                key=f"manual-cost-{holding.id}",
            )
            has_acquired_date = edit_cols[2].checkbox(
                "填写获得日期",
                value=holding.acquired_at is not None,
                key=f"manual-has-date-{holding.id}",
            )
            acquired_value = holding.acquired_at.date() if holding.acquired_at else date.today()
            acquired_date = (
                edit_cols[3].date_input("获得日期", value=acquired_value, key=f"manual-date-{holding.id}")
                if has_acquired_date
                else None
            )
            source = edit_cols[4].text_input("来源", value=holding.source, key=f"manual-source-{holding.id}")
            note = st.text_area("备注", value=holding.note or "", key=f"manual-note-{holding.id}")
            save_cols = st.columns([1, 1, 2, 2])
            saved = save_cols[0].form_submit_button("保存修改")
            toggled = save_cols[1].form_submit_button("停用" if holding.is_active else "恢复")
            convert = save_cols[2].form_submit_button("转换为历史买入交易")
            if saved:
                try:
                    update_manual_holding(
                        session,
                        holding.id,
                        quantity=_to_decimal(quantity),
                        unit_cost=_optional_decimal(unit_cost),
                        acquired_at=(
                            datetime.combine(acquired_date, time(12, 0), tzinfo=timezone.utc)
                            if acquired_date is not None
                            else None
                        ),
                        source=source,
                        note=note or None,
                    )
                    session.commit()
                    st.success("手动持仓已更新，交易记录未改变。")
                    st.rerun()
                except ValueError as exc:
                    session.rollback()
                    st.error(str(exc))
            if toggled:
                set_manual_holding_active(session, holding.id, not holding.is_active)
                session.commit()
                st.success("手动持仓状态已更新。")
                st.rerun()
            if convert:
                try:
                    parsed_cost = _optional_decimal(unit_cost)
                    parsed_date = (
                        datetime.combine(acquired_date, time(12, 0), tzinfo=timezone.utc)
                        if acquired_date is not None
                        else None
                    )
                    if parsed_cost is None or parsed_date is None:
                        raise ValueError("转换为历史买入交易需要填写日期和价格")
                    convert_manual_holding_to_buy_transaction(
                        session,
                        holding.id,
                        quantity=_to_decimal(quantity),
                        unit_price=parsed_cost,
                        occurred_at=parsed_date,
                        notes=note or None,
                    )
                    session.commit()
                    st.success("已转换为历史买入交易，并减少或停用对应手动持仓。")
                    st.rerun()
                except ValueError as exc:
                    session.rollback()
                    st.error(str(exc))


def render_item_management(session) -> None:
    st.subheader("饰品管理")
    _render_add_item_section(session)

    st.divider()
    _render_delete_item_section(session)

    st.divider()
    st.subheader("Monitored Items")
    items = session.scalars(select(Item).order_by(Item.market_name)).all()
    if not items:
        st.info("尚未添加饰品。")
        return

    for item in items:
        with st.container(border=True):
            cols = st.columns([3, 2, 2, 1])
            cols[0].markdown(f"**{item.market_name}**")
            cols[1].write(item.buff_name or "未填写中文名")
            cols[2].write(item.category or "未分类")
            status = "启用中" if item.is_active else "已禁用"
            cols[3].write(status)

            with st.form(f"edit-item-{item.id}"):
                edit_cols = st.columns([2, 2, 1, 1])
                buff_name = edit_cols[0].text_input("中文名 / BUFF 名称", value=item.buff_name or "", key=f"buff-{item.id}")
                category = edit_cols[1].text_input("分类", value=item.category or "", key=f"category-{item.id}")
                saved = edit_cols[2].form_submit_button("保存修改")
                toggled = edit_cols[3].form_submit_button("禁用" if item.is_active else "启用")
                if saved:
                    update_item_metadata(session, item.id, buff_name, category)
                    session.commit()
                    st.success("饰品信息已更新。")
                    st.rerun()
                if toggled:
                    set_item_active(session, item.id, not item.is_active)
                    session.commit()
                    st.success("监控状态已更新，历史交易和价格数据已保留。")
                    st.rerun()


def _render_delete_item_section(session) -> None:
    """Render safe item deletion controls."""
    st.subheader("安全删除饰品")
    items = list_items(session)
    if not items:
        st.info("暂无可删除饰品。")
        return

    labels = {f"{item.market_name} #{item.id}": item for item in items}
    selected_label = st.selectbox("选择要删除的饰品", list(labels.keys()), key="delete-item-select")
    item = labels[selected_label]
    st.warning(f"将删除：{item.market_name} | item_id={item.id}")
    confirmation = st.text_input(
        "请输入完整 market_name 进行二次确认",
        key=f"delete-confirm-{item.id}",
    )
    delete_disabled = confirmation != item.market_name
    if st.button("删除该饰品及关联数据", type="secondary", disabled=delete_disabled):
        try:
            result = delete_item_with_related_data(session, item.id)
            session.commit()
            st.success(
                "已删除 "
                f"{result.market_name} #{result.item_id}；"
                f"价格快照 {result.price_snapshots} 条，"
                f"每日汇总 {result.daily_price_summaries} 条，"
                f"交易 {result.transactions} 条，"
                f"持仓 {result.holdings} 条，"
                f"手动期初持仓 {result.manual_holdings} 条，"
                f"告警 {result.alerts} 条。"
            )
            st.rerun()
        except ValueError as exc:
            session.rollback()
            st.error(str(exc))


def _render_add_item_section(session) -> None:
    """Render global catalog search and add controls."""
    st.subheader("Add New Item")
    keyword = st.text_input("Search Item", key="catalog-search-keyword")
    if len(keyword.strip()) < 2:
        st.info("请输入至少 2 个字符")
        return
    matches = search_items(keyword, limit=20)
    monitored_names = _monitored_market_names(session)

    if keyword and not matches:
        st.info("未找到匹配的 Catalog 物品。")
        return

    selected_item = _select_catalog_item(matches, monitored_names)
    if selected_item is None:
        st.caption("输入中文、英文或别名，从完整 Item Catalog 中搜索。")
        return

    already_added = selected_item.market_hash_name in monitored_names
    _render_catalog_item_details(selected_item, already_added)
    if st.button("Add Item", type="primary", disabled=already_added):
        try:
            add_item(
                session,
                selected_item.market_hash_name,
                selected_item.display_name_cn or None,
                selected_item.item_type or None,
            )
            session.commit()
            st.success("饰品已添加。")
            st.session_state.show_quick_add_item = False
            st.rerun()
        except ValueError as exc:
            session.rollback()
            st.warning("Already added" if "already exists" in str(exc) else str(exc))


def _select_catalog_item(
    matches: list[ItemInfo], monitored_names: set[str]
) -> ItemInfo | None:
    """Render catalog result choices and return the selected item."""
    if not matches:
        return None
    options = {
        _catalog_option_label(item, item.market_hash_name in monitored_names): item
        for item in matches
    }
    selected_label = st.selectbox("Catalog Results", list(options.keys()))
    return options[selected_label]


def _catalog_option_label(item: ItemInfo, already_added: bool) -> str:
    """Build a catalog result label."""
    status = "Added" if already_added else "Not Added"
    display_name = _catalog_display_name(item)
    return (
        f"{item.display_name_cn} | {display_name} | "
        f"{item.market_hash_name} | Type: {item.item_type} | Status: {status}"
    )


def _render_catalog_item_details(item: ItemInfo, already_added: bool) -> None:
    """Render selected catalog item details."""
    status = "Already added" if already_added else "Not Added"
    st.write(item.display_name_cn)
    st.write(_catalog_display_name(item))
    st.caption(f"market_hash_name: {item.market_hash_name}")
    if item.version:
        st.caption(f"Version: {item.version}")
        st.warning("当前价格 Provider 按 market_hash_name 查询，不支持 version 级定价。")
    st.caption(f"Type: {item.item_type}")
    st.caption(f"Status: {status}")
    st.caption(f"Price Supported: {'Yes' if item.price_supported else 'No'}")


def _catalog_display_name(item: ItemInfo) -> str:
    """Return UI display name with version suffix when present."""
    return f"{item.display_name_en} — {item.version}" if item.version else item.display_name_en


def _monitored_market_names(session) -> set[str]:
    """Return market names already present in the local watchlist."""
    return set(session.scalars(select(Item.market_name)).all())


def render_daily_price_entry(session) -> None:
    st.subheader("每日价格录入")
    active_items = list_enabled_items(session)
    if not active_items:
        st.info("没有已启用饰品。请先到“饰品管理”页面添加或启用饰品。")
        return

    labels = {f"{item.market_name} #{item.id}": item for item in active_items}
    selected_label = st.selectbox("饰品", list(labels.keys()), key="daily-price-selected-item")
    selected_item = labels[selected_label]

    _render_automatic_sync_status()
    _render_api_sync_section(session)
    _render_latest_api_price(session, selected_item.id)

    st.divider()
    st.subheader("手动价格录入")
    with st.form("manual_price"):
        lowest = st.number_input("最低挂单价", min_value=0.0, step=1.0, format="%.2f")
        highest_buy = st.number_input("最高求购价", min_value=0.0, step=1.0, format="%.2f")
        listing_count = st.number_input("挂单量", min_value=0, step=1)
        currency = st.text_input("币种", value="CNY")
        captured_date = st.date_input("记录日期")
        captured_time = st.time_input("记录时间", value=datetime.now().time().replace(microsecond=0))
        submitted = st.form_submit_button("保存价格快照")
        if submitted:
            item = selected_item
            captured_at = datetime.combine(captured_date, captured_time, tzinfo=timezone.utc)
            provider = ManualPriceProvider(
                lowest_listing_price=_to_decimal(lowest),
                highest_buy_order_price=_to_decimal(highest_buy),
                listing_count=int(listing_count),
                currency=currency,
                captured_at=captured_at,
            )
            snapshot = record_price_quote(session, provider.get_quote(item.id))
            regenerate_daily_summaries(session)
            session.commit()
            if snapshot.is_suspicious:
                st.warning(f"已保存，但价格被标记为可疑：{snapshot.suspicion_reason}")
            else:
                st.success("价格快照已保存。")

    st.divider()
    st.subheader("CSV 批量导入价格")
    uploaded = st.file_uploader("CSV columns: market_name, lowest_listing_price, highest_buy_order_price, listing_count, currency, captured_at", type="csv")
    if uploaded and st.button("导入价格 CSV"):
        temp_path = Path("data/import_prices.csv")
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(uploaded.getvalue())
        count = import_price_csv(session, temp_path)
        regenerate_daily_summaries(session)
        session.commit()
        st.success(f"已导入 {count} 条价格记录。")


def _render_api_sync_section(session) -> None:
    """Render external API sync controls."""
    settings = load_settings()
    st.markdown("#### 同步所有启用饰品")
    mode = settings.api.mode
    st.caption(f"当前 Provider: {settings.api.provider.value} | 当前运行模式: {mode.value}")
    if mode == ProviderMode.MANUAL:
        st.info("当前为 manual 模式：外部 API 同步已禁用，仍可使用下方手动录入。")
        st.divider()
        return

    config_error: str | None = None
    try:
        validate_api_provider_settings(settings)
    except (PriceProviderError, ValueError) as exc:
        config_error = str(exc)

    if config_error:
        st.warning(f"外部 API 尚未配置完成：{config_error}")
        st.button("同步所有启用饰品", disabled=True)
        if mode == ProviderMode.HYBRID:
            st.caption("hybrid 模式下 API 不可用时，仍可使用下方手动录入。")
        st.divider()
        return

    if st.button("同步所有启用饰品", type="primary"):
        try:
            summary = sync_all_enabled_items(session, settings)
            session.commit()
            _render_sync_summary(summary)
            st.rerun()
        except (PriceProviderError, ValueError) as exc:
            session.rollback()
            st.error(f"同步启动失败：{exc}")
            if mode == ProviderMode.HYBRID:
                st.info("hybrid 模式已保留手动录入能力，请使用下方表单。")

    st.divider()


def _render_automatic_sync_status() -> None:
    """Render the last one-shot automatic sync status without starting jobs."""
    status = read_automatic_sync_status()
    st.markdown("#### Automatic Sync Status")
    if status is None:
        st.info("Last automatic sync time: never")
        return
    cols = st.columns(4)
    cols[0].metric("Last automatic sync time", status.ended_at or "unknown")
    cols[1].metric("Last sync result", status.result)
    cols[2].metric("Successful items", status.successful_items)
    cols[3].metric("Failed items", status.failed_items)
    if status.failures:
        st.caption("Failed items")
        _render_sync_failure_rows(status.failures)


def _latest_api_snapshot(session, item_id: int) -> PriceSnapshot | None:
    """Return the latest non-manual snapshot for one item."""
    return session.scalar(
        select(PriceSnapshot)
        .where(PriceSnapshot.item_id == item_id)
        .where(PriceSnapshot.provider != "manual")
        .order_by(PriceSnapshot.captured_at.desc(), PriceSnapshot.id.desc())
        .limit(1)
    )


def _render_latest_api_price(session, item_id: int) -> None:
    """Render the latest API price snapshot for the selected item."""
    st.markdown("#### 最新 API 价格")
    snapshot = _latest_api_snapshot(session, item_id)
    if snapshot is None:
        st.info("当前饰品暂无 API 价格快照。")
        return

    cols = st.columns(6)
    cols[0].metric("最低挂单价", _format_money_or_insufficient(_decimal_or_none(snapshot.lowest_listing_price)))
    cols[1].metric("最高求购价", _format_money_or_insufficient(_decimal_or_none(snapshot.highest_buy_order_price)))
    cols[2].metric("挂单量", "数据不足" if snapshot.listing_count is None else str(snapshot.listing_count))
    cols[3].metric("币种", snapshot.currency)
    cols[4].metric("来源", snapshot.provider)
    cols[5].metric("记录时间", str(snapshot.captured_at))


def _render_sync_summary(summary: SyncSummary) -> None:
    """Render API sync summary and per-item details."""
    cols = st.columns(3)
    cols[0].metric("成功", summary.success_count)
    cols[1].metric("失败", summary.failure_count)
    cols[2].metric("可疑", summary.suspicious_count)
    if not summary.results:
        st.info("本次同步没有返回物品结果。")
        return

    header = st.columns([3, 1.2, 1, 1, 3, 2])
    header[0].markdown("**Item**")
    header[1].markdown("**Status**")
    header[2].markdown("**Price**")
    header[3].markdown("**Currency**")
    header[4].markdown("**Error**")
    header[5].markdown("**Timestamp**")
    for result in summary.results:
        row = _sync_result_display_row(result)
        cols = st.columns([3, 1.2, 1, 1, 3, 2])
        cols[0].markdown(row["item_name"])
        cols[1].caption(row["status"])
        cols[2].caption(row["price"])
        cols[3].caption(row["currency"])
        cols[4].caption(row["error"])
        cols[5].caption(row["timestamp"])


def _render_sync_failure_rows(failures: list[object]) -> None:
    """Render automatic sync failures without Streamlit Arrow tables."""
    header = st.columns([1, 3, 4])
    header[0].markdown("**Item ID**")
    header[1].markdown("**Item**")
    header[2].markdown("**Reason**")
    for failure in failures:
        cols = st.columns([1, 3, 4])
        cols[0].caption(_safe_display_text(_get_field(failure, "item_id")))
        cols[1].caption(_safe_display_text(_get_field(failure, "market_name")))
        cols[2].caption(_safe_display_text(_get_field(failure, "reason")))


def _sync_result_display_row(result: object) -> dict[str, str]:
    """Convert one sync result to primitive display strings."""
    status = _get_field(result, "status")
    status_text = _safe_display_text(status.value if isinstance(status, Enum) else status)
    message = _safe_display_text(_get_field(result, "message"))
    error = _safe_display_text(_get_field(result, "error")) or (message if status_text == "failed" else "")
    price = _safe_price_text(
        _first_present_field(
            result,
            "price",
            "lowest_listing_price",
            "lowest_price",
            "listing_price",
        )
    )
    timestamp = _safe_display_text(
        _first_present_field(
            result,
            "captured_at",
            "timestamp",
            "updated_at",
            "created_at",
        )
    )
    return {
        "item_name": _safe_display_text(
            _first_present_field(result, "market_name", "item_name", "name")
        ),
        "status": status_text,
        "price": price,
        "currency": _safe_display_text(_get_field(result, "currency")),
        "error": error,
        "timestamp": timestamp,
    }


def _first_present_field(source: object, *names: str) -> object:
    for name in names:
        value = _get_field(source, name)
        if not _is_missing_value(value):
            return value
    return ""


def _get_field(source: object, name: str) -> object:
    if isinstance(source, dict):
        return source.get(name, "")
    return getattr(source, name, "")


def _safe_price_text(value: object) -> str:
    if _is_missing_value(value):
        return ""
    try:
        return str(float(value))
    except (TypeError, ValueError, InvalidOperation):
        return _safe_display_text(value)


def _safe_display_text(value: object) -> str:
    if _is_missing_value(value):
        return ""
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Exception):
        return str(value)
    if isinstance(value, Decimal):
        return str(float(value))
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, (dict, list, tuple, set)) or is_dataclass(value):
        return str(value)
    return str(value)


def render_transactions(session) -> None:
    st.subheader("交易记录管理")
    active_items = list_enabled_items(session)
    all_items = list_items(session)
    if not active_items:
        st.info("没有已启用饰品。请先到“饰品管理”页面添加或启用饰品。")
    else:
        labels = {f"{item.market_name} #{item.id}": item for item in active_items}
        with st.form("transaction"):
            selected_label = st.selectbox("饰品", list(labels.keys()))
            tx_type = st.selectbox("类型", [TransactionType.BUY.value.upper(), TransactionType.SELL.value.upper()])
            quantity = st.number_input("数量", min_value=1, value=1, step=1, format="%d")
            unit_price = st.number_input("单价", min_value=0.0, step=1.0, format="%.2f")
            fee = st.number_input("手续费", min_value=0.0, step=0.1, format="%.2f")
            tx_date = st.date_input("交易日期")
            tx_time = st.time_input("交易时间", value=time(12, 0))
            notes = st.text_area("备注")
            submitted = st.form_submit_button("保存交易")
            if submitted:
                item = labels[selected_label]
                try:
                    record_transaction(
                        session=session,
                        item_id=item.id,
                        transaction_type=TransactionType(tx_type.lower()),
                        quantity=parse_transaction_quantity(quantity),
                        unit_price=_to_decimal(unit_price),
                        fee=_to_decimal(fee),
                        occurred_at=datetime.combine(tx_date, tx_time, tzinfo=timezone.utc),
                        notes=notes or None,
                    )
                    session.commit()
                    st.success("交易已保存，持仓已更新。")
                    st.rerun()
                except ValueError as exc:
                    session.rollback()
                    st.error(str(exc))

    st.divider()
    st.subheader("CSV 批量导入交易")
    uploaded = st.file_uploader("CSV columns: market_name, transaction_type, quantity, unit_price, fee, occurred_at, notes", type="csv")
    if uploaded and st.button("导入交易 CSV"):
        temp_path = Path("data/import_transactions.csv")
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(uploaded.getvalue())
        try:
            count = import_transaction_csv(session, temp_path)
            session.commit()
            st.success(f"已导入 {count} 条交易记录。")
        except ValueError as exc:
            session.rollback()
            st.error(str(exc))

    st.divider()
    st.subheader("历史交易记录")
    transactions = get_transactions(session, limit=TRANSACTION_DISPLAY_LIMIT)
    if not transactions:
        st.info("暂无交易记录。")
        return
    item_map = {item.id: item for item in all_items}
    _render_transaction_history(session, transactions, item_map, all_items)


def _render_transaction_history(
    session,
    transactions: list[Transaction],
    item_map: dict[int, Item],
    items: list[Item],
) -> None:
    """Render editable transaction history rows."""
    if len(transactions) == TRANSACTION_DISPLAY_LIMIT:
        st.caption(f"仅显示最近 {TRANSACTION_DISPLAY_LIMIT} 条交易。")

    header = st.columns([0.8, 3, 0.9, 1, 1, 1, 1.8, 1.8, 2, 0.8, 0.8])
    header[0].markdown("**ID**")
    header[1].markdown("**饰品名称**")
    header[2].markdown("**类型**")
    header[3].markdown("**数量**")
    header[4].markdown("**单价**")
    header[5].markdown("**总金额**")
    header[6].markdown("**交易时间**")
    header[7].markdown("**创建时间**")
    header[8].markdown("**备注**")
    header[9].markdown("**编辑**")
    header[10].markdown("**删除**")

    for transaction in transactions:
        item = item_map.get(transaction.item_id)
        row = st.columns([0.8, 3, 0.9, 1, 1, 1, 1.8, 1.8, 2, 0.8, 0.8])
        row[0].caption(str(transaction.id))
        row[1].caption(_safe_display_text(item.market_name if item else f"item_id={transaction.item_id}"))
        row[2].caption(transaction.transaction_type.upper())
        row[3].caption(_format_transaction_quantity(transaction.quantity))
        row[4].caption(_format_money(Decimal(transaction.unit_price)))
        row[5].caption(_format_money(Decimal(transaction.quantity) * Decimal(transaction.unit_price)))
        row[6].caption(_safe_display_text(transaction.occurred_at))
        row[7].caption(_safe_display_text(transaction.created_at))
        row[8].caption(_safe_display_text(transaction.notes))
        if row[9].button("编辑", key=f"edit-tx-{transaction.id}"):
            st.session_state.editing_transaction_id = transaction.id
            st.session_state.deleting_transaction_id = None
        if row[10].button("删除", key=f"delete-tx-{transaction.id}"):
            st.session_state.deleting_transaction_id = transaction.id
            st.session_state.editing_transaction_id = None

    state = getattr(st, "session_state", {})
    editing_id = state.get("editing_transaction_id") if hasattr(state, "get") else None
    deleting_id = state.get("deleting_transaction_id") if hasattr(state, "get") else None
    if editing_id:
        transaction = next((row for row in transactions if row.id == editing_id), None)
        if transaction is None:
            transaction = session.get(Transaction, editing_id)
        if transaction is not None:
            st.divider()
            _render_transaction_edit_form(session, transaction, items, item_map)
    if deleting_id:
        transaction = next((row for row in transactions if row.id == deleting_id), None)
        if transaction is None:
            transaction = session.get(Transaction, deleting_id)
        if transaction is not None:
            st.divider()
            _render_transaction_delete_confirmation(session, transaction, item_map)


def _render_transaction_edit_form(
    session,
    transaction: Transaction,
    items: list[Item],
    item_map: dict[int, Item],
) -> None:
    """Render one edit form with before/after confirmation."""
    if not items:
        st.error("没有可选择的饰品，无法编辑交易。")
        return

    st.subheader(f"编辑交易 #{transaction.id}")
    item_labels = {f"{item.market_name} #{item.id}": item for item in items}
    current_label = next(
        (label for label, item in item_labels.items() if item.id == transaction.item_id),
        next(iter(item_labels)),
    )
    occurred_at = transaction.occurred_at
    with st.form(f"transaction-edit-{transaction.id}"):
        selected_label = st.selectbox(
            "饰品",
            list(item_labels.keys()),
            index=list(item_labels.keys()).index(current_label),
            key=f"edit-item-{transaction.id}",
        )
        tx_type = st.selectbox(
            "类型",
            [TransactionType.BUY.value.upper(), TransactionType.SELL.value.upper()],
            index=0 if transaction.transaction_type == TransactionType.BUY.value else 1,
            key=f"edit-type-{transaction.id}",
        )
        cols = st.columns([1, 1, 1, 1])
        current_quantity_valid = is_valid_transaction_quantity(transaction.quantity)
        if current_quantity_valid:
            quantity_value = cols[0].number_input(
                "数量",
                min_value=1,
                value=int(Decimal(transaction.quantity)),
                step=1,
                format="%d",
                key=f"edit-qty-{transaction.id}",
            )
        else:
            st.warning(
                f"该交易的历史数量 {transaction.quantity} 不符合当前整数数量规则，"
                "请输入正确的饰品数量后保存。"
            )
            quantity_value = cols[0].text_input(
                "数量",
                value="",
                placeholder=str(transaction.quantity),
                key=f"edit-qty-{transaction.id}",
            )
        unit_price = cols[1].number_input(
            "单价",
            min_value=0.0,
            step=1.0,
            format="%.2f",
            value=float(transaction.unit_price),
            key=f"edit-price-{transaction.id}",
        )
        tx_date = cols[2].date_input(
            "交易日期",
            value=occurred_at.date(),
            key=f"edit-date-{transaction.id}",
        )
        tx_time = cols[3].time_input(
            "交易时间",
            value=occurred_at.time().replace(microsecond=0),
            key=f"edit-time-{transaction.id}",
        )
        notes = st.text_area("备注", value=transaction.notes or "", key=f"edit-notes-{transaction.id}")
        selected_item = item_labels[selected_label]
        new_occurred_at = datetime.combine(tx_date, tx_time, tzinfo=timezone.utc)
        parsed_quantity, quantity_error = _parse_transaction_quantity_for_ui(quantity_value)
        changes = _transaction_edit_changes(
            transaction,
            item_map.get(transaction.item_id),
            selected_item,
            tx_type,
            parsed_quantity,
            _to_decimal(unit_price),
            new_occurred_at,
            notes or None,
        )
        if changes:
            st.markdown("**修改差异**")
            for label, before, after in changes:
                st.caption(f"{label}: {before} → {after}")
        else:
            st.caption("尚未修改任何字段。")
        confirmed = st.checkbox(
            "我确认保存以上修改，并重新计算库存、持仓成本、盈亏和资产组合估值。",
            key=f"edit-confirm-{transaction.id}",
        )
        validation_errors: list[str] = []
        if quantity_error is not None:
            validation_errors.append(quantity_error)
        has_changes = bool(changes)
        logger.info(
            "Edit form state: transaction_id=%s confirmed=%s has_changes=%s validation_errors=%s",
            transaction.id,
            confirmed,
            has_changes,
            validation_errors,
        )
        buttons = st.columns([1, 1, 4])
        saved = buttons[0].form_submit_button("保存修改")
        cancelled = buttons[1].form_submit_button("取消")
        if cancelled:
            st.session_state.editing_transaction_id = None
            st.rerun()
        if saved:
            if not confirmed:
                st.error("请先勾选确认选项。")
            elif not has_changes:
                st.error("尚未修改任何字段。")
            elif validation_errors:
                for error in validation_errors:
                    st.error(error)
            else:
                try:
                    update_transaction(
                        session,
                        transaction.id,
                        item_id=selected_item.id,
                        transaction_type=TransactionType(tx_type.lower()),
                        quantity=parse_transaction_quantity(quantity_value),
                        unit_price=_to_decimal(unit_price),
                        occurred_at=new_occurred_at,
                        notes=notes or None,
                    )
                    session.commit()
                    _clear_streamlit_caches()
                    st.session_state.editing_transaction_id = None
                    st.success("交易已更新，相关库存和估值已重新计算。")
                    st.rerun()
                except ValueError as exc:
                    session.rollback()
                    st.error(str(exc))


def _render_transaction_delete_confirmation(
    session,
    transaction: Transaction,
    item_map: dict[int, Item],
) -> None:
    """Render delete confirmation for one transaction."""
    item = item_map.get(transaction.item_id)
    st.subheader(f"删除交易 #{transaction.id}")
    st.warning(
        "将删除这条交易；删除后会重新计算库存；该操作可能改变持仓成本、"
        "已实现盈亏和总资产价值。"
    )
    st.write(
        "交易："
        f"{_safe_display_text(item.market_name if item else f'item_id={transaction.item_id}')} | "
        f"{transaction.transaction_type.upper()} | "
        f"数量 {_format_transaction_quantity(transaction.quantity)} | "
        f"单价 {_format_money(Decimal(transaction.unit_price))} | "
        f"时间 {_safe_display_text(transaction.occurred_at)}"
    )
    confirmed = st.checkbox(
        "我确认删除这条交易，并重新计算所有依赖数据。",
        key=f"delete-confirm-{transaction.id}",
    )
    cols = st.columns([1, 1, 4])
    if cols[0].button("确认删除", type="secondary", disabled=not confirmed, key=f"delete-confirm-button-{transaction.id}"):
        try:
            delete_transaction(session, transaction.id)
            session.commit()
            _clear_streamlit_caches()
            st.session_state.deleting_transaction_id = None
            st.success("交易已删除，相关库存和估值已重新计算。")
            st.rerun()
        except ValueError as exc:
            session.rollback()
            st.error(str(exc))
    if cols[1].button("取消", key=f"delete-cancel-{transaction.id}"):
        st.session_state.deleting_transaction_id = None
        st.rerun()


def _transaction_edit_changes(
    transaction: Transaction,
    old_item: Item | None,
    new_item: Item,
    tx_type: str,
    quantity: Decimal | None,
    unit_price: Decimal,
    occurred_at: datetime,
    notes: str | None,
) -> list[tuple[str, str, str]]:
    """Return displayable before/after transaction changes."""
    old_time = _normalize_ui_datetime(transaction.occurred_at)
    new_time = _normalize_ui_datetime(occurred_at)
    old_notes = transaction.notes or None
    new_notes = notes or None
    checks = [
        ("Item", _safe_display_text(old_item.market_name if old_item else f"item_id={transaction.item_id}"), new_item.market_name),
        ("Transaction type", transaction.transaction_type.upper(), tx_type.upper()),
        ("Quantity", Decimal(transaction.quantity), quantity),
        ("Unit price", Decimal(transaction.unit_price), unit_price),
        ("Transaction time", old_time, new_time),
        ("Notes", old_notes, new_notes),
    ]
    return [
        (label, _format_transaction_change_value(before), _format_transaction_change_value(after))
        for label, before, after in checks
        if before != after
    ]


def _normalize_ui_datetime(value: datetime) -> datetime:
    """Normalize UI datetimes to naive UTC for stable equality checks."""
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def _format_transaction_change_value(value: object) -> str:
    """Format a normalized value for transaction diff display."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, datetime):
        return value.isoformat()
    return _safe_display_text(value)


def _parse_transaction_quantity_for_ui(value: object) -> tuple[Decimal | None, str | None]:
    """Parse a transaction quantity for UI validation without raising during render."""
    try:
        return parse_transaction_quantity(value), None
    except ValueError as exc:
        return None, str(exc)


def _format_transaction_quantity(value: object) -> str:
    """Display valid transaction quantities as integers and invalid ones as stored."""
    if is_valid_transaction_quantity(value):
        return str(int(Decimal(str(value))))
    return str(value)


def _clear_streamlit_caches() -> None:
    """Clear Streamlit caches so pages re-read database state."""
    try:
        st.cache_data.clear()
    except AttributeError:
        pass


def render_item_analysis(session) -> None:
    st.subheader("单品价格分析")
    items = list_items(session)
    if not items:
        st.info("暂无饰品。")
        return
    labels = {f"{item.market_name} #{item.id}": item for item in items}
    item = labels[st.selectbox("饰品", list(labels.keys()))]
    analysis = calculate_item_analysis(session, item.id, max_snapshots=ITEM_ANALYSIS_SNAPSHOT_LIMIT)

    if not analysis.price_points and not analysis.suspicious_points:
        st.info("暂无价格快照。")
        return

    _render_item_analysis_metrics(analysis)
    _render_holding_metrics(analysis)
    fig = _build_item_analysis_chart(analysis)
    st.pyplot(fig, use_container_width=True)
    _close_matplotlib_figure(fig)

    if analysis.daily_summaries:
        summary_frame = pd.DataFrame(
            [
                {
                    "date": row.summary_date,
                    "currency": str(row.currency),
                    "open": float(row.open_price),
                    "high": float(row.high_price),
                    "low": float(row.low_price),
                    "close": float(row.close_price),
                    "median": float(row.median_price),
                    "snapshot_count": int(row.snapshot_count),
                }
                for row in analysis.daily_summaries
            ],
            columns=["date", "currency", "open", "high", "low", "close", "median", "snapshot_count"],
        )
        _render_dataframe_html(summary_frame, max_rows=ITEM_ANALYSIS_SNAPSHOT_LIMIT, empty_message="暂无每日汇总。")


def _render_item_analysis_metrics(analysis: ItemAnalysis) -> None:
    cols = st.columns(6)
    cols[0].metric("当前最低挂单价", _format_money_or_insufficient(analysis.current_lowest_listing_price))
    cols[1].metric("当前最高求购价", _format_money_or_insufficient(analysis.current_highest_buy_order_price))
    cols[2].metric("24小时涨跌幅", _format_percent_or_insufficient(analysis.change_24h))
    cols[3].metric("7日涨跌幅", _format_percent_or_insufficient(analysis.change_7d))
    cols[4].metric("30日涨跌幅", _format_percent_or_insufficient(analysis.change_30d))
    cols[5].metric("买卖价差率", _format_percent_or_insufficient(analysis.bid_ask_spread_rate))


def _render_holding_metrics(analysis: ItemAnalysis) -> None:
    if analysis.holding is None:
        return
    st.subheader("持仓估值")
    cols = st.columns(6)
    cols[0].metric("当前持仓数量", f"{analysis.holding.quantity:.6f}")
    cols[1].metric("移动加权平均成本", _format_money(analysis.holding.avg_cost))
    cols[2].metric("当前挂单估值", _format_money(analysis.holding.listing_value))
    cols[3].metric("快速变现估值", _format_money(analysis.holding.quick_sell_value))
    cols[4].metric("未实现收益", _format_money_or_insufficient(analysis.holding.unrealized_profit))
    cols[5].metric("未实现收益率", _format_percent_or_insufficient(analysis.holding.unrealized_profit_rate))


def _build_item_analysis_chart(analysis: ItemAnalysis) -> Any:
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/cs2-price-monitor-matplotlib")
    import matplotlib.pyplot as plt

    figure_background = "#0B1220"
    axes_background = "#111827"
    primary_text = "#E5E7EB"
    secondary_text = "#9CA3AF"
    grid_color = "#374151"
    spine_color = "#4B5563"

    fig, ax = plt.subplots(figsize=(10, 4.8), facecolor=figure_background)
    fig.patch.set_facecolor(figure_background)
    ax.set_facecolor(axes_background)
    normal_points = sorted(analysis.price_points, key=lambda point: point.captured_at)
    suspicious_points = sorted(analysis.suspicious_points, key=lambda point: point.captured_at)

    if normal_points:
        captured_at = [_to_python_datetime(point.captured_at) for point in normal_points]
        lowest = [_to_optional_float(point.lowest_listing_price) for point in normal_points]
        highest_buy = [_to_optional_float(point.highest_buy_order_price) for point in normal_points]
        ma_7d = [_to_optional_float(point.ma_7d) for point in normal_points]
        ma_30d = [_to_optional_float(point.ma_30d) for point in normal_points]
        ax.plot(captured_at, lowest, marker="o", markersize=4, linewidth=2.0, color="#60A5FA", label="Lowest listing")
        ax.plot(
            captured_at,
            highest_buy,
            marker="o",
            markersize=4,
            linewidth=1.9,
            color="#F59E0B",
            label="Highest buy order",
        )
        ax.plot(captured_at, ma_7d, linewidth=1.8, color="#34D399", label="7-day moving average")
        ax.plot(captured_at, ma_30d, linewidth=1.8, color="#F472B6", label="30-day moving average")

    if analysis.holding is not None and normal_points:
        ax.axhline(float(analysis.holding.avg_cost), linestyle="--", linewidth=1.5, color=secondary_text, label="Average cost")

    if suspicious_points:
        suspicious_captured_at = [_to_python_datetime(point.captured_at) for point in suspicious_points]
        suspicious_lowest = [_to_optional_float(point.lowest_listing_price) for point in suspicious_points]
        ax.scatter(suspicious_captured_at, suspicious_lowest, marker="x", s=52, color="#F87171", label="Suspicious point")

    ax.set_xlabel("captured_at", color=secondary_text)
    ax.set_ylabel("Price", color=secondary_text)
    ax.title.set_color(primary_text)
    ax.tick_params(axis="both", colors=secondary_text)
    for spine in ax.spines.values():
        spine.set_color(spine_color)
    ax.grid(True, color=grid_color, alpha=0.55, linewidth=0.8)
    legend = ax.legend(loc="best", facecolor=axes_background, edgecolor=spine_color, framealpha=0.92)
    for text in legend.get_texts():
        text.set_color(primary_text)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def _close_matplotlib_figure(fig: Any) -> None:
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/cs2-price-monitor-matplotlib")
    import matplotlib.pyplot as plt

    plt.close(fig)


def _format_money(value: Decimal) -> str:
    return f"{value:.2f}"


def _to_optional_float(value: Decimal | int | float | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _to_python_datetime(value: datetime | pd.Timestamp) -> datetime:
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


def _decimal_or_none(value: object) -> Decimal | None:
    """Convert nullable database numeric values to Decimal."""
    if value is None:
        return None
    return Decimal(str(value))


def _format_money_or_insufficient(value: Decimal | None) -> str:
    if value is None:
        return "数据不足"
    return _format_money(value)


def _format_percent_or_insufficient(value: Decimal | None) -> str:
    if value is None:
        return "数据不足"
    return f"{value * Decimal('100'):.2f}%"


def render_alerts_quality(session) -> None:
    st.subheader("告警和数据质量")
    items = session.scalars(select(Item).order_by(Item.market_name)).all()
    item_map = {item.id: item.market_name for item in items}
    for item in items:
        cols = st.columns([4, 1])
        cols[0].write(f"{item.market_name} | active={item.is_active}")
        if cols[1].button("禁用" if item.is_active else "启用", key=f"toggle-{item.id}"):
            set_item_active(session, item.id, not item.is_active)
            session.commit()
            st.rerun()

    alerts = session.scalars(
        select(Alert).order_by(Alert.created_at.desc(), Alert.id.desc()).limit(ALERT_DISPLAY_LIMIT)
    ).all()
    alert_frame = pd.DataFrame(
        [
            {
                "id": alert.id,
                "item": str(item_map.get(alert.item_id, "")),
                "severity": str(alert.severity),
                "message": str(alert.message),
                "is_resolved": alert.is_resolved,
                "created_at": alert.created_at,
            }
            for alert in alerts
        ],
        columns=["id", "item", "severity", "message", "is_resolved", "created_at"],
    )
    _render_dataframe_html(alert_frame, max_rows=ALERT_DISPLAY_LIMIT, empty_message="暂无告警。")

    suspicious = session.scalars(
        select(PriceSnapshot).where(PriceSnapshot.is_suspicious.is_(True)).order_by(PriceSnapshot.captured_at.desc())
        .limit(SUSPICIOUS_SNAPSHOT_DISPLAY_LIMIT)
    ).all()
    st.subheader("可疑价格快照")
    suspicious_frame = pd.DataFrame(
        [_snapshot_row(row) for row in suspicious],
        columns=[
            "id",
            "item_id",
            "provider",
            "lowest_listing_price",
            "highest_buy_order_price",
            "listing_count",
            "currency",
            "captured_at",
            "is_suspicious",
            "suspicion_reason",
        ],
    )
    _render_dataframe_html(suspicious_frame, max_rows=SUSPICIOUS_SNAPSHOT_DISPLAY_LIMIT, empty_message="暂无可疑价格快照。")

    if st.button("导出历史价格 CSV"):
        export_path = Path("exports/price_history.csv")
        export_price_history(session, export_path)
        st.success(f"已导出到 {export_path}")


def _transaction_row(row: Transaction) -> dict[str, object]:
    return {
        "id": row.id,
        "item_id": row.item_id,
        "transaction_type": row.transaction_type,
        "quantity": row.quantity,
        "unit_price": row.unit_price,
        "fee": row.fee,
        "occurred_at": row.occurred_at,
        "notes": row.notes,
    }


def _snapshot_row(row: PriceSnapshot) -> dict[str, object]:
    return {
        "id": row.id,
        "item_id": row.item_id,
        "provider": row.provider,
        "lowest_listing_price": row.lowest_listing_price,
        "highest_buy_order_price": row.highest_buy_order_price,
        "listing_count": row.listing_count,
        "currency": row.currency,
        "captured_at": row.captured_at,
        "is_suspicious": row.is_suspicious,
        "suspicion_reason": row.suspicion_reason,
    }


def _summary_row(row: DailyPriceSummary) -> dict[str, object]:
    return {
        "date": row.summary_date,
        "currency": row.currency,
        "open": row.open_price,
        "high": row.high_price,
        "low": row.low_price,
        "close": row.close_price,
        "median": row.median_price,
        "snapshot_count": row.snapshot_count,
    }

if __name__ == "__main__":
    main()
