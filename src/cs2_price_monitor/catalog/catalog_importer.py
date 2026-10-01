from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Iterable


ALLOWED_ITEM_TYPES = {
    "Case",
    "Weapon",
    "Knife",
    "Glove",
    "Sticker",
    "Agent",
    "Music Kit",
    "Graffiti",
    "Patch",
    "Other",
}

DEFAULT_FIELD_MAP = {
    "market_hash_name": "market_hash_name",
    "version": "version",
    "display_name_en": "display_name_en",
    "display_name_cn": "display_name_cn",
    "item_type": "item_type",
    "aliases": "aliases",
    "price_supported": "price_supported",
    "source_item_page": "item_page",
    "source_market_page": "market_page",
}


@dataclass(frozen=True)
class CatalogImportStats:
    """Import summary. / 导入统计。"""

    total_input: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    duplicate_count: int = 0
    missing_cn_count: int = 0
    unique_market_hash_names: int = 0
    unique_catalog_ids: int = 0
    duplicate_market_hash_name_groups: int = 0
    records_with_version: int = 0
    records_without_version: int = 0
    item_type_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogImportResult:
    """Imported catalog rows and stats. / 导入后的目录行和统计。"""

    rows: list[dict[str, Any]]
    stats: CatalogImportStats


def import_catalog_file(
    input_path: Path,
    output_path: Path,
    field_map: dict[str, str] | None = None,
    report_path: Path | None = None,
) -> CatalogImportResult:
    """Import a raw JSON or CSV catalog file. / 导入原始 JSON 或 CSV 目录文件。"""
    raw_rows = load_raw_rows(input_path)
    manual_rows = _load_manual_catalog_rows(output_path)
    result = normalize_catalog_rows(raw_rows, field_map=field_map)
    if manual_rows:
        result = _apply_manual_overrides(result, manual_rows)
    write_catalog(result.rows, output_path)
    if report_path is not None:
        write_import_report(result.stats, report_path)
    return result


def _load_manual_catalog_rows(output_path: Path) -> list[dict[str, Any]]:
    """Load existing curated rows before overwriting output."""
    if output_path.exists():
        rows = load_raw_rows(output_path)
        return [row for row in rows if str(row.get("display_name_cn", "") or "").strip()]
    return []


def _apply_manual_overrides(
    result: CatalogImportResult, manual_rows: list[dict[str, Any]]
) -> CatalogImportResult:
    """Apply curated Chinese names and aliases with manual priority."""
    by_identity = {(row["market_hash_name"], row["version"]): dict(row) for row in result.rows}
    for manual_raw in manual_rows:
        manual = _normalize_row(manual_raw, DEFAULT_FIELD_MAP)
        if manual is None:
            continue
        key = (manual["market_hash_name"], manual["version"])
        if key not in by_identity:
            by_identity[key] = manual
            continue
        existing = by_identity[key]
        by_identity[key] = {
            **existing,
            "display_name_cn": manual["display_name_cn"] or existing["display_name_cn"],
            "aliases": list(dict.fromkeys([*manual["aliases"], *existing["aliases"]])),
        }
    rows = sorted(
        by_identity.values(),
        key=lambda row: (row["market_hash_name"].casefold(), row["version"].casefold()),
    )
    counts = Counter(row["item_type"] for row in rows)
    stats = CatalogImportStats(
        total_input=result.stats.total_input,
        valid_count=len(rows),
        invalid_count=result.stats.invalid_count,
        duplicate_count=result.stats.duplicate_count,
        missing_cn_count=sum(1 for row in rows if not row["display_name_cn"]),
        unique_market_hash_names=len({row["market_hash_name"] for row in rows}),
        unique_catalog_ids=len({row["catalog_id"] for row in rows}),
        duplicate_market_hash_name_groups=result.stats.duplicate_market_hash_name_groups,
        records_with_version=sum(1 for row in rows if row["version"]),
        records_without_version=sum(1 for row in rows if not row["version"]),
        item_type_counts=dict(sorted(counts.items())),
    )
    return CatalogImportResult(rows=rows, stats=stats)


def load_raw_rows(input_path: Path) -> list[dict[str, Any]]:
    """Load raw rows from JSON or CSV. / 从 JSON 或 CSV 加载原始行。"""
    suffix = input_path.suffix.lower()
    if suffix == ".json":
        loaded = json.loads(input_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            for key in ("items", "data", "rows"):
                if isinstance(loaded.get(key), list):
                    loaded = loaded[key]
                    break
        if not isinstance(loaded, list):
            raise ValueError("JSON catalog input must be a list or contain items/data/rows")
        return [row for row in loaded if isinstance(row, dict)]
    if suffix == ".csv":
        with input_path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    raise ValueError("Catalog input must be .json or .csv")


def normalize_catalog_rows(
    raw_rows: Iterable[dict[str, Any]],
    field_map: dict[str, str] | None = None,
) -> CatalogImportResult:
    """Normalize, validate, deduplicate, and sort rows. / 标准化、校验、去重并排序。"""
    mapping = {**DEFAULT_FIELD_MAP, **(field_map or {})}
    by_catalog_id: dict[str, dict[str, Any]] = {}
    market_name_counts: Counter[str] = Counter()
    total_input = 0
    invalid_count = 0
    duplicate_count = 0

    for raw in raw_rows:
        total_input += 1
        row = _normalize_row(raw, mapping)
        if row is None:
            invalid_count += 1
            continue
        market_name_counts[row["market_hash_name"]] += 1
        key = row["catalog_id"]
        if key in by_catalog_id:
            duplicate_count += 1
            by_catalog_id[key] = _merge_duplicate(by_catalog_id[key], row)
        else:
            by_catalog_id[key] = row

    rows = sorted(
        by_catalog_id.values(),
        key=lambda row: (row["market_hash_name"].casefold(), row["version"].casefold()),
    )
    counts = Counter(row["item_type"] for row in rows)
    stats = CatalogImportStats(
        total_input=total_input,
        valid_count=len(rows),
        invalid_count=invalid_count,
        duplicate_count=duplicate_count,
        missing_cn_count=sum(1 for row in rows if not row["display_name_cn"]),
        unique_market_hash_names=len({row["market_hash_name"] for row in rows}),
        unique_catalog_ids=len({row["catalog_id"] for row in rows}),
        duplicate_market_hash_name_groups=sum(1 for count in market_name_counts.values() if count > 1),
        records_with_version=sum(1 for row in rows if row["version"]),
        records_without_version=sum(1 for row in rows if not row["version"]),
        item_type_counts=dict(sorted(counts.items())),
    )
    return CatalogImportResult(rows=rows, stats=stats)


def write_catalog(rows: list[dict[str, Any]], output_path: Path) -> None:
    """Write normalized catalog JSON. / 写入标准化目录 JSON。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_import_report(stats: CatalogImportStats, output_path: Path) -> None:
    """Write import report JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "raw_records": stats.total_input,
                "unique_market_hash_names": stats.unique_market_hash_names,
                "unique_catalog_ids": stats.unique_catalog_ids,
                "duplicate_market_hash_name_groups": stats.duplicate_market_hash_name_groups,
                "records_with_version": stats.records_with_version,
                "records_without_version": stats.records_without_version,
                "invalid_records": stats.invalid_count,
                "item_type_counts": stats.item_type_counts,
                "missing_chinese_names": stats.missing_cn_count,
                "duplicate_records": stats.duplicate_count,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def load_catalog_stats(catalog_path: Path) -> CatalogImportStats:
    """Load stats for an existing normalized catalog. / 读取现有标准目录的统计。"""
    if catalog_path.exists():
        rows = load_raw_rows(catalog_path)
    else:
        resource = resources.files("cs2_price_monitor.catalog").joinpath("catalog_data.json")
        rows = json.loads(resource.read_text(encoding="utf-8"))
    counts = Counter(str(row.get("item_type", "Other")) for row in rows)
    market_names = [str(row.get("market_hash_name", "")).strip() for row in rows]
    catalog_ids = [str(row.get("catalog_id", "")).strip() for row in rows if row.get("catalog_id")]
    duplicate_count = len(catalog_ids) - len(set(catalog_ids))
    invalid_count = sum(1 for name in market_names if not name)
    return CatalogImportStats(
        total_input=len(rows),
        valid_count=len([name for name in market_names if name]),
        invalid_count=invalid_count,
        duplicate_count=duplicate_count,
        missing_cn_count=sum(1 for row in rows if not str(row.get("display_name_cn", "")).strip()),
        unique_market_hash_names=len(set(name for name in market_names if name)),
        unique_catalog_ids=len(set(catalog_ids)),
        duplicate_market_hash_name_groups=sum(1 for count in Counter(market_names).values() if count > 1),
        records_with_version=sum(1 for row in rows if str(row.get("version", "") or "").strip()),
        records_without_version=sum(1 for row in rows if not str(row.get("version", "") or "").strip()),
        item_type_counts=dict(sorted(counts.items())),
    )


def parse_field_map(values: list[str] | None) -> dict[str, str]:
    """Parse CLI field mappings. / 解析命令行字段映射。"""
    result: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"Invalid field mapping: {value}")
        target, source = value.split("=", 1)
        result[target.strip()] = source.strip()
    return result


def _normalize_row(raw: dict[str, Any], field_map: dict[str, str]) -> dict[str, Any] | None:
    """Normalize one raw row."""
    market_hash_name = _clean_string(raw.get(field_map["market_hash_name"]))
    if not market_hash_name:
        return None
    version = _clean_string(raw.get(field_map["version"]))
    display_name_en = _clean_string(raw.get(field_map["display_name_en"])) or market_hash_name
    display_name_cn = _clean_string(raw.get(field_map["display_name_cn"]))
    source_item_page = _clean_string(raw.get(field_map["source_item_page"]))
    source_market_page = _clean_string(raw.get(field_map["source_market_page"]))
    item_type = _normalize_item_type(
        raw.get(field_map["item_type"]),
        source_item_page=source_item_page,
        source_market_page=source_market_page,
        market_hash_name=market_hash_name,
    )
    aliases = _normalize_aliases(raw.get(field_map["aliases"]))
    if version:
        aliases = list(dict.fromkeys([*aliases, version, version.replace("Phase", "phase")]))
    price_supported = _to_bool(raw.get(field_map["price_supported"]), default=True)
    return {
        "catalog_id": _catalog_id(market_hash_name, version),
        "market_hash_name": market_hash_name,
        "version": version,
        "display_name_en": display_name_en,
        "display_name_cn": display_name_cn,
        "item_type": item_type,
        "aliases": aliases,
        "price_supported": price_supported,
        "source": _clean_string(raw.get("source")) or "skinport",
        "source_item_page": source_item_page,
        "source_market_page": source_market_page,
    }


def _merge_duplicate(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge duplicate catalog identity rows."""
    aliases = list(dict.fromkeys([*existing["aliases"], *incoming["aliases"]]))
    display_name_cn = existing["display_name_cn"] or incoming["display_name_cn"]
    item_type = existing["item_type"] if existing["item_type"] != "Other" else incoming["item_type"]
    return {
        **existing,
        "display_name_cn": display_name_cn,
        "item_type": item_type,
        "aliases": aliases,
        "price_supported": bool(existing["price_supported"] or incoming["price_supported"]),
        "source_item_page": existing["source_item_page"] or incoming["source_item_page"],
        "source_market_page": existing["source_market_page"] or incoming["source_market_page"],
    }


def _clean_string(value: Any) -> str:
    """Clean optional text. / 清理可选文本。"""
    return "" if value is None else str(value).strip()


def _normalize_item_type(
    value: Any,
    *,
    source_item_page: str = "",
    source_market_page: str = "",
    market_hash_name: str = "",
) -> str:
    """Normalize item type using controlled, explicit rules."""
    cleaned = _clean_string(value)
    if cleaned in ALLOWED_ITEM_TYPES:
        return cleaned
    page = f"{source_item_page} {source_market_page}".casefold()
    name = market_hash_name.casefold()
    if "/market/knife" in page:
        return "Knife"
    if "/market/gloves" in page:
        return "Glove"
    if "/market/sticker" in page or name.startswith("sticker |"):
        return "Sticker"
    if "/market/agent" in page:
        return "Agent"
    if "/market/music-kit" in page or name.startswith("music kit |"):
        return "Music Kit"
    if "/market/graffiti" in page:
        return "Graffiti"
    if "/market/patch" in page:
        return "Patch"
    if "/market/container" in page or name.endswith(" case") or "weapon case" in name or "capsule" in name:
        return "Case"
    if " | " in market_hash_name and "(" in market_hash_name and ")" in market_hash_name:
        return "Weapon"
    return "Other"


def _catalog_id(market_hash_name: str, version: str) -> str:
    """Build a deterministic catalog identity from market_hash_name and version."""
    raw = f"{market_hash_name}\u001f{version}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"skinport-{digest}"


def _normalize_aliases(value: Any) -> list[str]:
    """Normalize aliases from list or separated text. / 从列表或分隔文本标准化别名。"""
    if value is None:
        return []
    if isinstance(value, list):
        raw_aliases = value
    else:
        raw_aliases = str(value).replace("|", ",").replace(";", ",").split(",")
    aliases = [_clean_string(alias) for alias in raw_aliases]
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _to_bool(value: Any, default: bool) -> bool:
    """Convert common boolean values. / 转换常见布尔值。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "y"}
