from decimal import Decimal

import pytest

from app.core.config import Settings
from app.db.models import FallbackBehavior, Position, PositionSettings, PositionState, UpdateStatus
from app.market.schemas import OwnLot
from app.repricer.engine import RepricerEngine


class FakeStarvellClient:
    def __init__(self, *, fresh_cache: bool = True) -> None:
        self.fresh_cache = fresh_cache
        self.updated_prices: list[Decimal] = []

    def own_lot_cache_status(self, lot_id):
        return {
            "fresh": self.fresh_cache,
            "reason": "cache_fresh" if self.fresh_cache else "cache_empty",
            "cache_age_seconds": 1.0 if self.fresh_cache else None,
            "cache_ttl_seconds": 30.0,
        }

    def cached_own_lot(self, lot_id):
        if not self.fresh_cache:
            return None
        return OwnLot(position_amount=500, price=Decimal("306.00"), lot_id=str(lot_id))

    async def update_my_lot_price(self, position_amount, lot_id, new_price, *, allow_real_write):
        self.updated_prices.append(new_price)


class FakePositionRepository:
    def __init__(self) -> None:
        self.state_updates = []
        self.price_logs = []

    async def update_state(self, *args, **kwargs):
        self.state_updates.append(kwargs)

    async def add_price_log(self, *args, **kwargs):
        self.price_logs.append(kwargs)


def _position() -> Position:
    position = Position(robux_amount=500, lot_id="2000", enabled=True)
    position.settings = PositionSettings(
        min_price=Decimal("300.00"),
        max_price=Decimal("400.00"),
        step=Decimal("0.60"),
        min_rating=Decimal("4.5"),
        ignore_no_rating=True,
        fallback_behavior=FallbackBehavior.KEEP_CURRENT.value,
    )
    position.state = PositionState(current_own_price=Decimal("306.00"))
    return position


def _engine(*, fresh_cache: bool = True) -> tuple[RepricerEngine, FakeStarvellClient, FakePositionRepository]:
    settings = Settings(
        _env_file=None,
        watcher_event_fast_path_enabled=True,
        dry_run=False,
    )
    starvell = FakeStarvellClient(fresh_cache=fresh_cache)
    positions = FakePositionRepository()
    engine = RepricerEngine(session=None, settings=settings, starvell_client=starvell)
    engine.positions = positions
    return engine, starvell, positions


@pytest.mark.asyncio
async def test_event_fast_path_updates_without_market_fetch() -> None:
    engine, starvell, positions = _engine()
    event = {
        "source": "price_watcher",
        "offer_id": "222760",
        "new_price": "307.00",
        "seller_id": "seller-2",
        "seller_username": "seller2",
        "rating": "4.9",
        "is_active": True,
        "detected_at_ms": 1000,
    }

    result = await engine._process_watcher_price_event_loaded_position(_position(), event)

    assert result is not None
    assert result.status == UpdateStatus.SUCCESS.value
    assert result.new_price == Decimal("306.40")
    assert result.phase_metrics["market_fetch_ms"] == 0.0
    assert starvell.updated_prices == [Decimal("306.40")]
    assert positions.price_logs[0]["status"] == UpdateStatus.SUCCESS.value


@pytest.mark.asyncio
async def test_event_fast_path_falls_back_when_own_lot_cache_is_not_fresh() -> None:
    engine, starvell, positions = _engine(fresh_cache=False)
    event = {
        "source": "price_watcher",
        "offer_id": "222760",
        "new_price": "307.00",
        "seller_id": "seller-2",
        "rating": "4.9",
        "is_active": True,
    }

    result = await engine._process_watcher_price_event_loaded_position(_position(), event)

    assert result is None
    assert starvell.updated_prices == []
    assert positions.price_logs == []
