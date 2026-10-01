from __future__ import annotations

from importlib import resources
import json
from pathlib import Path
from typing import Any


DEFAULT_GENERATED_CATALOG_PATH = Path("data/catalog_generated/catalog_data.json")


def load_catalog_rows() -> list[dict[str, Any]]:
    """Load catalog rows from generated data or bundled examples. / 从生成目录或内置示例加载目录。"""
    if DEFAULT_GENERATED_CATALOG_PATH.exists():
        return json.loads(DEFAULT_GENERATED_CATALOG_PATH.read_text(encoding="utf-8"))
    resource = resources.files("cs2_price_monitor.catalog").joinpath("catalog_data.json")
    return json.loads(resource.read_text(encoding="utf-8"))
