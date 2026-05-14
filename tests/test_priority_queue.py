from app.db.models import Position, PriorityLevel
from app.repricer.priority_queue import PercentagePositionQueue


def position(position_id: int, amount: int, priority: str) -> Position:
    return Position(id=position_id, robux_amount=amount, priority=priority)


def test_percentage_queue_allocates_70_percent_to_high_priority() -> None:
    queue = PercentagePositionQueue(high_percent=70, normal_percent=30)
    queue.update(
        [
            position(1, 400, PriorityLevel.HIGH.value),
            position(2, 80, PriorityLevel.NORMAL.value),
        ]
    )

    amounts = [queue.next().robux_amount for _ in range(100)]

    assert amounts.count(400) == 70
    assert amounts.count(80) == 30


def test_percentage_queue_falls_back_when_one_priority_is_empty() -> None:
    queue = PercentagePositionQueue(high_percent=70, normal_percent=30)
    queue.update([position(1, 400, PriorityLevel.HIGH.value)])

    amounts = [queue.next().robux_amount for _ in range(5)]

    assert amounts == [400, 400, 400, 400, 400]
