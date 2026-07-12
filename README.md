# CS2 饰品投资组合监控系统

本项目是一个本地运行的 Python CS2 饰品投资组合监控系统。当前默认使用 Streamlit 手动录入价格；外部价格 API 的第一阶段工程骨架已接入到 CSInventoryAPI，但真实端点、认证方式和生产 JSON 响应仍待适配。不实现爬虫或自动交易。

## 技术栈

- Python 3.12
- SQLite
- SQLAlchemy 2.0
- pandas
- Streamlit
- Plotly
- APScheduler
- httpx
- pytest
- python-dotenv
- PyYAML

## 项目结构

```text
src/cs2_price_monitor/
  config.py                 # Config loading / 配置加载
  db.py                     # Database engine/session / 数据库引擎与会话
  models.py                 # SQLAlchemy models / 数据库模型
  providers/                # Price providers / 价格来源
    base.py                 # PriceProvider + PriceQuote / 抽象接口与统一报价
    manual.py               # ManualPriceProvider / 手动录入
    csinventoryapi.py       # Configurable CSInventoryAPI skeleton / 可配置 API 骨架
    steamwebapi.py          # Optional non-default SteamWebAPI skeleton / 非默认可选骨架
    factory.py              # Provider factory / Provider 工厂
    free_api.py             # Reserved free API provider / 预留免费 API
    paid_api.py             # Reserved paid API provider / 预留付费 API
  services/                 # Core business logic / 核心业务逻辑
    items.py
    transactions.py
    prices.py
    sync.py                 # External price sync service / 外部价格同步服务
    analytics.py
    import_export.py
  ui/streamlit_app.py       # Dashboard UI / 仪表盘界面
  scheduler.py              # Daily summary scheduler / 每日汇总调度
tests/                      # pytest tests / 测试
config/app.yaml             # YAML config / YAML 配置
```

## 安装

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

如果你的系统命令是 `python` 而不是 `python3.12`，可以按本机环境调整。

## 初始化数据库

```bash
PYTHONPATH=src python -m cs2_price_monitor.cli init-db
```

默认数据库路径来自 `.env.example`：

```text
CS2_MONITOR_DATABASE_URL=sqlite:///data/cs2_monitor.sqlite3
```

也可以在 `.env` 或 `config/app.yaml` 中修改。

## 启动 Dashboard

```bash
PYTHONPATH=src streamlit run src/cs2_price_monitor/app.py
```

浏览器打开 Streamlit 显示的本地地址即可使用。

## 录入数据

1. 打开“饰品管理”页面。
2. 添加监控饰品，填写 `market_hash_name`、中文名和分类。
3. 在“饰品管理”页面可以启用、禁用、修改中文名和分类；重复 `market_hash_name` 会被拒绝。
4. 打开“每日价格录入”页面，可以先使用顶部的外部 API 同步区域，也可以继续使用手动录入表单。
5. 每次录入都会写入一条独立 `price_snapshots` 历史记录。
6. 系统会生成 `daily_price_summaries`，包括 OHLC 和中位数价格。

禁用饰品只会停止它出现在价格录入和交易记录页面，不会删除历史交易或价格数据。

## 交易记录

在“交易记录管理”页面记录买入和卖出：

- 买入会按数量、单价和手续费更新移动加权平均成本。
- 卖出会减少持仓，并计算已实现收益。
- 不允许卖出超过当前持仓数量。

## CSV 导入与导出

价格 CSV 字段：

```text
market_name,lowest_listing_price,highest_buy_order_price,listing_count,currency,captured_at
```

交易 CSV 字段：

```text
market_name,transaction_type,quantity,unit_price,fee,occurred_at,notes
```

历史价格可在“告警和数据质量”页面导出到 `exports/price_history.csv`。

## 数据质量规则

当新录入的最低挂单价：

- 高于前一条记录的 3 倍；
- 或低于前一条记录的三分之一；

系统会将该价格快照标记为可疑，并创建一条告警。

可疑价格不会参与正常估值、涨跌幅计算或每日价格汇总。可疑检测会对比上一条正常价格，而不是上一条任意价格。

## 价格来源模式

通过 `.env` 中的 `CS2_MONITOR_PRICE_MODE` 设置运行模式，并通过 `CS2_MONITOR_API_PROVIDER` 选择外部 provider：

- `manual`：默认模式。不会初始化任何外部 API 客户端，只使用 Streamlit 手动录入。
- `api`：启用外部 API 同步。缺少 CSInventoryAPI 配置时，页面会显示明确错误并禁用同步按钮。
- `hybrid`：同时保留 API 同步和手动录入。API 配置缺失或同步失败时，不会伪造价格，手动表单仍可使用。

外部 API 同步只调用服务层 `sync_all_enabled_items()`，保存价格仍复用现有 `record_price_quote()`，UI 不直接写入 `price_snapshots`。

第一阶段不会自动启用 APScheduler 定时 API 同步。

## CSInventoryAPI Starter 配置

当前项目只完成 CSInventoryAPI 接入骨架，没有猜测真实端点、认证方式或 JSON 字段。HTTP 请求构造、响应解码和 `PriceQuote` 映射已拆分；在没有真实响应 schema 前，真实 provider 的映射阶段会抛出清晰的 `ApiSchemaNotConfiguredError`。

`.env.example` 中提供占位配置：

```text
CS2_MONITOR_PRICE_MODE=manual
CS2_MONITOR_API_PROVIDER=csinventoryapi
CS2_MONITOR_CSINVENTORY_API_KEY=
CS2_MONITOR_CSINVENTORY_BASE_URL=
CS2_MONITOR_CSINVENTORY_TIMEOUT_SECONDS=10
CS2_MONITOR_CSINVENTORY_MARKET=buff163
CS2_MONITOR_CSINVENTORY_PRICE_ENDPOINT_TEMPLATE=
CS2_MONITOR_CSINVENTORY_AUTH_HEADER_NAME=
CS2_MONITOR_CSINVENTORY_AUTH_QUERY_PARAM_NAME=
```

API Key 只从 `.env` 读取，不应写入代码、日志或 README。`CS2_MONITOR_CSINVENTORY_PRICE_ENDPOINT_TEMPLATE` 支持 `{item_id}`、`{market_name}`、`{buff_name}` 占位符，但真实模板必须来自官方文档。认证可以配置为 header 或 query 参数，具体名称也必须来自官方文档。

SteamWebAPI provider 仍保留为可选非默认骨架：

```text
STEAMWEBAPI_API_KEY=
STEAMWEBAPI_BASE_URL=
STEAMWEBAPI_TIMEOUT_SECONDS=10
STEAMWEBAPI_MARKET=buff
STEAMWEBAPI_PRICE_ENDPOINT_TEMPLATE=
STEAMWEBAPI_AUTH_HEADER_NAME=
STEAMWEBAPI_AUTH_QUERY_PARAM_NAME=
STEAMWEBAPI_FIELD_LOWEST_LISTING_PRICE=
STEAMWEBAPI_FIELD_HIGHEST_BUY_ORDER_PRICE=
STEAMWEBAPI_FIELD_LISTING_COUNT=
STEAMWEBAPI_FIELD_CURRENCY=
STEAMWEBAPI_FIELD_CAPTURED_AT=
```

测试中的 `tests/fixtures/csinventoryapi/synthetic_success.json` 是 synthetic fixture，只用于验证适配边界，不代表真实 CSInventoryAPI 响应。SteamWebAPI 的 synthetic fixture 也仅用于保留旧骨架测试。

API provider 返回的价格、求购价和挂单量允许为 `None`，不会用 `0` 伪造缺失值。不同币种的数据不会直接用于涨跌幅或收益计算。

注册 API 后，请提供以下脱敏材料再完成真实解析：

- 官方端点文档
- 认证方式
- 脱敏后的成功 JSON
- 脱敏后的错误 JSON，如 401、403、429、5xx

## 测试

```bash
pytest -v
```

测试覆盖：

- 移动加权平均成本
- 卖出持仓与已实现收益
- 可疑价格标记和告警
- 每日 OHLC 与中位数汇总
- 挂单估值、快速变现估值和未实现收益
- CSInventoryAPI provider 骨架，包括 401、403、429、500、timeout、无效 JSON、缺失字段和缺失配置
- 不同币种不直接计算涨跌幅或收益
- 批量同步中单个饰品失败不影响其他饰品

## Provider 扩展

所有价格来源必须实现 `PriceProvider` 并返回统一的 `PriceQuote`：

```python
class PriceProvider(ABC):
    name: str

    @abstractmethod
    def get_quote(self, item_id: int) -> PriceQuote:
        ...
```

CSInventoryAPI 第一阶段实现位于 `providers/csinventoryapi.py`。真实响应确认后，只需要在 provider 的 schema 映射边界补充真实字段映射，服务层仍然调用统一报价结构。
