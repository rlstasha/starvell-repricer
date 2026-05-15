from dataclasses import dataclass


WORKER_GROUP_FAST_1 = "fast_1"
WORKER_GROUP_FAST_2 = "fast_2"
WORKER_GROUP_SLOW = "slow"
WORKER_GROUP_ALL = "all"

DEFAULT_WORKER_GROUP_POSITIONS = {
    WORKER_GROUP_FAST_1: (500, 800, 1000),
    WORKER_GROUP_FAST_2: (400, 1200, 1700, 2000),
    WORKER_GROUP_SLOW: (40, 80, 200, 2100, 2500, 3600, 4500, 10000, 22500),
}

ALL_WORKER_GROUPS = (
    WORKER_GROUP_FAST_1,
    WORKER_GROUP_FAST_2,
    WORKER_GROUP_SLOW,
)

LEGACY_ALL_POSITIONS = tuple(
    sorted(
        {
            amount
            for amounts in DEFAULT_WORKER_GROUP_POSITIONS.values()
            for amount in amounts
        }
    )
)

WORKER_GROUP_LABELS = {
    WORKER_GROUP_FAST_1: "Fast 1",
    WORKER_GROUP_FAST_2: "Fast 2",
    WORKER_GROUP_SLOW: "Slow",
    WORKER_GROUP_ALL: "Single-server worker",
}

WORKER_GROUP_ICONS = {
    WORKER_GROUP_FAST_1: "🚀",
    WORKER_GROUP_FAST_2: "🚀",
    WORKER_GROUP_SLOW: "🐢",
    WORKER_GROUP_ALL: "🖥️",
}

WORKER_GROUP_ALIASES = {
    "fast": WORKER_GROUP_FAST_1,
    "fast-1": WORKER_GROUP_FAST_1,
    "fast1": WORKER_GROUP_FAST_1,
    "worker_fast_1": WORKER_GROUP_FAST_1,
    "worker-fast-1": WORKER_GROUP_FAST_1,
    "medium": WORKER_GROUP_FAST_2,
    "fast-2": WORKER_GROUP_FAST_2,
    "fast2": WORKER_GROUP_FAST_2,
    "worker_fast_2": WORKER_GROUP_FAST_2,
    "worker-fast-2": WORKER_GROUP_FAST_2,
    "worker_slow": WORKER_GROUP_SLOW,
    "worker-slow": WORKER_GROUP_SLOW,
}


@dataclass(frozen=True)
class WorkerGroupInfo:
    name: str
    label: str
    icon: str
    positions: tuple[int, ...]
    request_limit_per_minute: int


def normalize_worker_group(value: str | None) -> str:
    normalized = (value or WORKER_GROUP_ALL).strip().lower().replace("-", "_")
    if normalized in {WORKER_GROUP_FAST_1, WORKER_GROUP_FAST_2, WORKER_GROUP_SLOW}:
        return normalized
    if normalized in {"", WORKER_GROUP_ALL, "legacy", "single", "single_server"}:
        return WORKER_GROUP_ALL
    alias = WORKER_GROUP_ALIASES.get(normalized) or WORKER_GROUP_ALIASES.get(
        normalized.replace("_", "-")
    )
    if alias:
        return alias
    raise ValueError("WORKER_GROUP must be fast_1, fast_2, slow, or all")


def parse_position_list(value: str | None, default: tuple[int, ...]) -> tuple[int, ...]:
    if value is None or not value.strip():
        return default

    positions: list[int] = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        if not item.isdigit():
            raise ValueError("worker position lists must contain comma-separated integers")
        amount = int(item)
        if amount <= 0:
            raise ValueError("worker position amounts must be positive")
        positions.append(amount)
    return tuple(dict.fromkeys(positions))


def default_positions_for_group(worker_group: str) -> tuple[int, ...]:
    group = normalize_worker_group(worker_group)
    if group == WORKER_GROUP_ALL:
        return LEGACY_ALL_POSITIONS
    return DEFAULT_WORKER_GROUP_POSITIONS[group]
