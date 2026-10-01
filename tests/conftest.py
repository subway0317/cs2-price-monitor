from __future__ import annotations

import pytest

from cs2_price_monitor.catalog import item_catalog
from cs2_price_monitor.db import build_engine, build_session_factory, init_db


@pytest.fixture(autouse=True)
def clear_catalog_cache():
    item_catalog._load_catalog.cache_clear()
    yield
    item_catalog._load_catalog.cache_clear()


@pytest.fixture()
def session():
    engine = build_engine("sqlite:///:memory:")
    init_db(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        yield session
