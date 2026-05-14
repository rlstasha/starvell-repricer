from collections import deque
from dataclasses import dataclass

from app.db.models import Position, PriorityLevel


@dataclass(frozen=True)
class QueueItem:
    position_id: int
    robux_amount: int


class PercentagePositionQueue:
    def __init__(self, *, high_percent: int = 70, normal_percent: int = 30):
        if high_percent < 0 or normal_percent < 0:
            raise ValueError("priority percentages cannot be negative")
        if high_percent + normal_percent != 100:
            raise ValueError("priority percentages must sum to 100")
        self.high_percent = high_percent
        self.normal_percent = normal_percent
        self._high_queue: deque[QueueItem] = deque()
        self._normal_queue: deque[QueueItem] = deque()
        self._priority_pattern: deque[str] = deque(self._build_priority_pattern())
        self._signature: tuple[tuple[int, str], ...] = ()

    def update(self, positions: list[Position]) -> None:
        signature = tuple(sorted((position.id, position.priority) for position in positions))
        if signature == self._signature and (self._high_queue or self._normal_queue):
            return

        high_items: list[QueueItem] = []
        normal_items: list[QueueItem] = []
        for position in sorted(positions, key=lambda item: item.robux_amount):
            item = QueueItem(position_id=position.id, robux_amount=position.robux_amount)
            if position.priority == PriorityLevel.HIGH.value:
                high_items.append(item)
            else:
                normal_items.append(item)

        self._high_queue = deque(high_items)
        self._normal_queue = deque(normal_items)
        self._signature = signature

    def next(self) -> QueueItem | None:
        if not self._high_queue and not self._normal_queue:
            return None
        if not self._high_queue:
            return self._next_from(self._normal_queue)
        if not self._normal_queue:
            return self._next_from(self._high_queue)

        for _ in range(len(self._priority_pattern)):
            priority = self._priority_pattern.popleft()
            self._priority_pattern.append(priority)
            if priority == PriorityLevel.HIGH.value and self._high_queue:
                return self._next_from(self._high_queue)
            if priority == PriorityLevel.NORMAL.value and self._normal_queue:
                return self._next_from(self._normal_queue)

        return self._next_from(self._high_queue or self._normal_queue)

    def _build_priority_pattern(self) -> list[str]:
        pattern: list[str] = []
        high_used = 0
        for index in range(100):
            if high_used * 100 < (index + 1) * self.high_percent:
                pattern.append(PriorityLevel.HIGH.value)
                high_used += 1
            else:
                pattern.append(PriorityLevel.NORMAL.value)
        return pattern

    def _next_from(self, queue: deque[QueueItem]) -> QueueItem:
        item = queue.popleft()
        queue.append(item)
        return item


WeightedPositionQueue = PercentagePositionQueue
