from __future__ import annotations

import pytest

from cs2_price_monitor.db import build_engine, build_session_factory, init_db


@pytest.fixture()
def session():
    engine = build_engine("sqlite:///:memory:")
    init_db(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        yield session

