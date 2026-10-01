from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import re
from typing import Any

from cs2_price_monitor.catalog.catalog_loader import load_catalog_rows


@dataclass(frozen=True)
class ItemInfo:
    """Searchable catalog item metadata."""

    catalog_id: str
    display_name_cn: str
    display_name_en: str
    market_hash_name: str
    version: str
    item_type: str
    aliases: tuple[str, ...] = field(default_factory=tuple)
    price_supported: bool = True
    source: str = ""
    source_item_page: str = ""
    source_market_page: str = ""


def get_all_items() -> list[ItemInfo]:
    """Return all catalog items. / 返回全部目录饰品。"""
    return list(_load_catalog())


def search_items(keyword: str, limit: int = 20) -> list[ItemInfo]:
    """Search catalog items by Chinese, English, aliases, and partial text. / 搜索目录饰品。"""
    normalized_keyword = _normalize(keyword)
    if len(normalized_keyword) < 2:
        return []

    scored: list[tuple[int, int, str, ItemInfo]] = []
    for item in _load_catalog():
        score = _match_score(normalized_keyword, item)
        if score is not None:
            scored.append((score, _item_type_priority(item), item.display_name_en, item))

    scored.sort(key=lambda row: (row[0], row[1], row[2]))
    return [item for _, _, _, item in scored[:limit]]


@lru_cache(maxsize=1)
def _load_catalog() -> tuple[ItemInfo, ...]:
    """Load item catalog data once. / 只加载一次目录数据。"""
    return tuple(_item_from_row(row) for row in load_catalog_rows())


def _item_from_row(row: dict[str, Any]) -> ItemInfo:
    """Convert one JSON row into ItemInfo. / 将 JSON 行转换为 ItemInfo。"""
    return ItemInfo(
        catalog_id=str(row.get("catalog_id", row["market_hash_name"])),
        display_name_cn=str(row.get("display_name_cn", "")),
        display_name_en=str(row["display_name_en"]),
        market_hash_name=str(row["market_hash_name"]),
        version=str(row.get("version", "") or ""),
        item_type=str(row["item_type"]),
        aliases=tuple(str(alias) for alias in row.get("aliases", [])),
        price_supported=bool(row.get("price_supported", row.get("supports_price_query", True))),
        source=str(row.get("source", "")),
        source_item_page=str(row.get("source_item_page", "")),
        source_market_page=str(row.get("source_market_page", "")),
    )


def _match_score(normalized_keyword: str, item: ItemInfo) -> int | None:
    """Return a lower score for stronger matches. / 分数越低匹配越强。"""
    name_fields = [
        item.market_hash_name,
        item.version,
        item.display_name_en,
        item.display_name_cn,
    ]
    alias_fields = list(item.aliases)
    normalized_names = [_normalize(field) for field in name_fields if field]
    normalized_aliases = [_normalize(field) for field in alias_fields if field]
    if any(field == normalized_keyword for field in normalized_names):
        return 0
    if any(field == normalized_keyword for field in normalized_aliases):
        return 0
    if any(field.startswith(normalized_keyword) for field in normalized_names):
        return 1
    if any(_token_startswith(field, normalized_keyword) for field in name_fields if field):
        return 2
    if any(normalized_keyword in field for field in normalized_names):
        return 5
    if any(field.startswith(normalized_keyword) for field in normalized_aliases):
        return 4
    if any(normalized_keyword in field for field in normalized_aliases):
        return 6
    return None


def _normalize(value: str) -> str:
    """Normalize search text by lowercasing and removing whitespace. / 小写化并移除空格。"""
    return re.sub(r"\s+", "", value.casefold())


def _token_startswith(value: str, normalized_keyword: str) -> bool:
    """Return whether any readable token starts with the normalized keyword."""
    return any(_normalize(token).startswith(normalized_keyword) for token in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", value))


def _item_type_priority(item: ItemInfo) -> int:
    """Prefer container results when search quality is otherwise equal."""
    if item.item_type == "Case":
        return 0
    return 1
