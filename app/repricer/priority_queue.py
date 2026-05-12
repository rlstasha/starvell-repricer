from collections import deque
from dataclasses import dataclass

from app.db.models import Position, PriorityLevel


@dataclass(frozen=True)
class QueueItem:
    position_id: int
    robux_amount: int


class WeightedPositionQueue:
    def __init__(self, *, high_weight: int = 5, normal_weight: int = 1):
        self.high_weight = high_weight
        self.normal_weight = normal_weight
        self._queue: deque[QueueItem] = deque()
        self._signature: tuple[tuple[int, str], ...] = ()

    def update(self, positions: list[Position]) -> None:
        signature = tuple(sorted((position.id, position.priority) for position in positions))
        if signature == self._signature and self._queue:
            return

        items: list[QueueItem] = []
        for position in sorted(positions, key=lambda item: item.robux_amount):
            weight = (
                self.high_weight
                if position.priority == PriorityLevel.HIGH.value
                else self.normal_weight
            )
            items.extend(
                QueueItem(position_id=position.id, robux_amount=position.robux_amount)
                for _ in range(weight)
            )

        self._queue = deque(items)
        self._signature = signature

    def next(self) -> QueueItem | None:
        if not self._queue:
            return None
        item = self._queue.popleft()
        self._queue.append(item)
        return item

