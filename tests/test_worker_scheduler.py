from app.core.config import Settings
from app.db.models import Position
from app.repricer.scheduler import RepricerScheduler


def test_worker_scheduler_filters_fast_2_positions_only() -> None:
    settings = Settings(_env_file=None, worker_group="fast_2")
    scheduler = RepricerScheduler(
        settings=settings,
        session_factory=object(),
        redis=object(),
    )
    positions = [
        Position(robux_amount=400),
        Position(robux_amount=500),
        Position(robux_amount=1200),
        Position(robux_amount=1700),
        Position(robux_amount=2000),
        Position(robux_amount=22500),
    ]

    filtered = scheduler._filter_assigned_positions(positions)

    assert [position.robux_amount for position in filtered] == [400, 1200, 1700, 2000]
