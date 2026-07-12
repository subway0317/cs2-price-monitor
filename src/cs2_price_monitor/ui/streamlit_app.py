from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import select

from cs2_price_monitor.config import ProviderMode, load_settings
from cs2_price_monitor.db import build_engine, build_session_factory, init_db
from cs2_price_monitor.models import Alert, DailyPriceSummary, Item, PriceSnapshot, Transaction, TransactionType
from cs2_price_monitor.providers.manual import ManualPriceProvider
from cs2_price_monitor.providers.errors import PriceProviderError
from cs2_price_monitor.providers.factory import validate_api_provider_settings
from cs2_price_monitor.services.analytics import ItemAnalysis, calculate_item_analysis, calculate_portfolio, portfolio_dataframe
from cs2_price_monitor.services.import_export import export_price_history, import_price_csv, import_transaction_csv
from cs2_price_monitor.services.items import add_item, list_enabled_items, list_items, set_item_active, update_item_metadata
from cs2_price_monitor.services.prices import record_price_quote, regenerate_daily_summaries
from cs2_price_monitor.services.sync import SyncSummary, sync_all_enabled_items
from cs2_price_monitor.services.transactions import record_transaction


@st.cache_resource
def _session_factory():
    settings = load_settings()
    engine = build_engine(settings.database.url)
    init_db(engine)
    return build_session_factory(engine)


def _to_decimal(value: object) -> Decimal:
    """Convert UI value to Decimal."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid decimal value: {value}") from exc


def main() -> None:
    settings = load_settings()
    st.set_page_config(page_title=settings.app.page_title, layout="wide")
    st.title(settings.app.page_title)

    page = st.sidebar.radio(
        "页面",
        ["投资组合总览", "饰品管理", "每日价格录入", "交易记录管理", "单品价格分析", "告警和数据质量"],
    )
    factory = _session_factory()
    with factory() as session:
        if page == "投资组合总览":
            render_portfolio_overview(session)
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
        _render_add_item_form(session, form_key="overview_quick_add", expanded=True)

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
    for column in ["change_24h", "change_7d", "change_30d"]:
        display[column] = display[column].apply(lambda x: None if x is None else f"{Decimal(str(x)) * 100:.2f}%")
    st.dataframe(display, use_container_width=True)


def render_item_management(session) -> None:
    st.subheader("饰品管理")
    _render_add_item_form(session, form_key="item_management_add", expanded=True)

    st.divider()
    st.subheader("全部饰品列表")
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


def _render_add_item_form(session, form_key: str, expanded: bool = True) -> None:
    """Render add item form with duplicate protection."""
    with st.expander("新增饰品", expanded=expanded):
        with st.form(form_key):
            market_name = st.text_input("market_hash_name / 饰品市场名")
            buff_name = st.text_input("中文名 / BUFF 名称")
            category = st.text_input("分类")
            submitted = st.form_submit_button("添加饰品", type="primary")
            if submitted:
                try:
                    add_item(session, market_name, buff_name or None, category or None)
                    session.commit()
                    st.success("饰品已添加。")
                    st.session_state.show_quick_add_item = False
                    st.rerun()
                except ValueError as exc:
                    session.rollback()
                    st.error(str(exc))


def render_daily_price_entry(session) -> None:
    st.subheader("每日价格录入")
    active_items = list_enabled_items(session)
    if not active_items:
        st.info("没有已启用饰品。请先到“饰品管理”页面添加或启用饰品。")
        return

    _render_api_sync_section(session)

    labels = {f"{item.market_name} #{item.id}": item for item in active_items}
    with st.form("manual_price"):
        selected_label = st.selectbox("饰品", list(labels.keys()))
        lowest = st.number_input("最低挂单价", min_value=0.0, step=1.0, format="%.2f")
        highest_buy = st.number_input("最高求购价", min_value=0.0, step=1.0, format="%.2f")
        listing_count = st.number_input("挂单量", min_value=0, step=1)
        currency = st.text_input("币种", value="CNY")
        captured_date = st.date_input("记录日期")
        captured_time = st.time_input("记录时间", value=datetime.now().time().replace(microsecond=0))
        submitted = st.form_submit_button("保存价格快照")
        if submitted:
            item = labels[selected_label]
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
        except (PriceProviderError, ValueError) as exc:
            session.rollback()
            st.error(f"同步启动失败：{exc}")
            if mode == ProviderMode.HYBRID:
                st.info("hybrid 模式已保留手动录入能力，请使用下方表单。")

    st.divider()


def _render_sync_summary(summary: SyncSummary) -> None:
    """Render API sync summary and per-item details."""
    cols = st.columns(3)
    cols[0].metric("成功", summary.success_count)
    cols[1].metric("失败", summary.failure_count)
    cols[2].metric("可疑", summary.suspicious_count)
    rows = [
        {
            "item_id": result.item_id,
            "market_name": result.market_name,
            "status": result.status.value,
            "message": result.message,
            "provider": result.provider,
            "snapshot_id": result.snapshot_id,
        }
        for result in summary.results
    ]
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True)


def render_transactions(session) -> None:
    st.subheader("交易记录管理")
    items = list_enabled_items(session)
    if not items:
        st.info("没有已启用饰品。请先到“饰品管理”页面添加或启用饰品。")
        return

    labels = {f"{item.market_name} #{item.id}": item for item in items}
    with st.form("transaction"):
        selected_label = st.selectbox("饰品", list(labels.keys()))
        tx_type = st.selectbox("类型", [TransactionType.BUY.value, TransactionType.SELL.value])
        quantity = st.number_input("数量", min_value=0.000001, step=1.0, format="%.6f")
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
                    transaction_type=TransactionType(tx_type),
                    quantity=_to_decimal(quantity),
                    unit_price=_to_decimal(unit_price),
                    fee=_to_decimal(fee),
                    occurred_at=datetime.combine(tx_date, tx_time, tzinfo=timezone.utc),
                    notes=notes or None,
                )
                session.commit()
                st.success("交易已保存，持仓已更新。")
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

    tx_rows = session.scalars(select(Transaction).order_by(Transaction.occurred_at.desc())).all()
    st.dataframe(pd.DataFrame([_transaction_row(row) for row in tx_rows]), use_container_width=True)


def render_item_analysis(session) -> None:
    st.subheader("单品价格分析")
    items = list_items(session)
    if not items:
        st.info("暂无饰品。")
        return
    labels = {f"{item.market_name} #{item.id}": item for item in items}
    item = labels[st.selectbox("饰品", list(labels.keys()))]
    analysis = calculate_item_analysis(session, item.id)

    if not analysis.price_points and not analysis.suspicious_points:
        st.info("暂无价格快照。")
        return

    _render_item_analysis_metrics(analysis)
    _render_holding_metrics(analysis)
    fig = _build_item_analysis_chart(analysis)
    st.plotly_chart(fig, use_container_width=True)

    if analysis.daily_summaries:
        summary_frame = pd.DataFrame(
            [
                {
                    "date": row.summary_date,
                    "currency": row.currency,
                    "open": _format_money(row.open_price),
                    "high": _format_money(row.high_price),
                    "low": _format_money(row.low_price),
                    "close": _format_money(row.close_price),
                    "median": _format_money(row.median_price),
                    "snapshot_count": row.snapshot_count,
                }
                for row in analysis.daily_summaries
            ]
        )
        st.dataframe(summary_frame, use_container_width=True)


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


def _build_item_analysis_chart(analysis: ItemAnalysis) -> go.Figure:
    normal_frame = pd.DataFrame([point.__dict__ for point in analysis.price_points])
    suspicious_frame = pd.DataFrame([point.__dict__ for point in analysis.suspicious_points])
    fig = go.Figure()

    if not normal_frame.empty:
        normal_frame = normal_frame.sort_values("captured_at")
        fig.add_trace(
            go.Scatter(
                x=normal_frame["captured_at"],
                y=normal_frame["lowest_listing_price"],
                mode="lines+markers",
                name="最低挂单价",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=normal_frame["captured_at"],
                y=normal_frame["highest_buy_order_price"],
                mode="lines+markers",
                name="最高求购价",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=normal_frame["captured_at"],
                y=normal_frame["ma_7d"],
                mode="lines",
                name="7日移动平均线",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=normal_frame["captured_at"],
                y=normal_frame["ma_30d"],
                mode="lines",
                name="30日移动平均线",
            )
        )

    if analysis.holding is not None and not normal_frame.empty:
        fig.add_hline(
            y=float(analysis.holding.avg_cost),
            line_dash="dash",
            line_color="#666666",
            annotation_text="平均持仓成本",
        )

    if not suspicious_frame.empty:
        suspicious_frame = suspicious_frame.sort_values("captured_at")
        fig.add_trace(
            go.Scatter(
                x=suspicious_frame["captured_at"],
                y=suspicious_frame["lowest_listing_price"],
                mode="markers",
                name="可疑价格点",
                marker={"size": 11, "symbol": "x", "color": "#d62728"},
            )
        )

    fig.update_layout(
        xaxis_title="captured_at",
        yaxis_title="价格",
        legend_title_text="指标",
        hovermode="x unified",
    )
    return fig


def _format_money(value: Decimal) -> str:
    return f"{value:.2f}"


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

    alerts = session.scalars(select(Alert).order_by(Alert.created_at.desc())).all()
    alert_frame = pd.DataFrame(
        [
            {
                "id": alert.id,
                "item": item_map.get(alert.item_id, ""),
                "severity": alert.severity,
                "message": alert.message,
                "is_resolved": alert.is_resolved,
                "created_at": alert.created_at,
            }
            for alert in alerts
        ]
    )
    st.dataframe(alert_frame, use_container_width=True)

    suspicious = session.scalars(
        select(PriceSnapshot).where(PriceSnapshot.is_suspicious.is_(True)).order_by(PriceSnapshot.captured_at.desc())
    ).all()
    st.subheader("可疑价格快照")
    st.dataframe(pd.DataFrame([_snapshot_row(row) for row in suspicious]), use_container_width=True)

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
