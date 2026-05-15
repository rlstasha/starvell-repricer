from app.core.config import get_settings
from app.repricer.worker_groups import ALL_WORKER_GROUPS


REQUESTS_PER_POSITION_CHECK = 1


def main() -> int:
    settings = get_settings()
    lines: list[str] = [
        f"Global limit: {settings.global_request_limit_per_minute}/min",
        "",
    ]

    total_worker_limit = 0
    for info in settings.worker_group_infos:
        total_worker_limit += info.request_limit_per_minute
        positions = "\n".join(str(amount) for amount in info.positions)
        lines.extend(
            [
                info.name,
                "",
                "positions:",
                positions,
                "",
                "limit:",
                f"{info.request_limit_per_minute}/min",
                "",
                "approx:",
                _frequency(info.request_limit_per_minute, len(info.positions)),
                "",
            ]
        )

    errors = _validate(settings, total_worker_limit)
    if errors:
        lines.append("Configuration errors:")
        lines.extend(f"- {error}" for error in errors)
        print("\n".join(lines))
        return 1

    print("\n".join(lines).rstrip())
    return 0


def _validate(settings, total_worker_limit: int) -> list[str]:
    errors: list[str] = []
    if total_worker_limit > settings.global_request_limit_per_minute:
        errors.append(
            "sum of worker limits is greater than GLOBAL_REQUEST_LIMIT_PER_MINUTE"
        )
    groups = {info.name for info in settings.worker_group_infos}
    if groups != set(ALL_WORKER_GROUPS):
        errors.append("worker group list is incomplete")
    assigned: dict[int, list[str]] = {}
    for info in settings.worker_group_infos:
        if not info.positions:
            errors.append(f"{info.name} has no assigned positions")
        for amount in info.positions:
            assigned.setdefault(amount, []).append(info.name)
    duplicates = {
        amount: groups
        for amount, groups in assigned.items()
        if len(groups) > 1
    }
    for amount, duplicate_groups in sorted(duplicates.items()):
        errors.append(
            f"position {amount} is assigned to multiple groups: "
            f"{', '.join(duplicate_groups)}"
        )
    return errors


def _frequency(request_limit: int, position_count: int) -> str:
    checks_per_minute = request_limit / REQUESTS_PER_POSITION_CHECK
    if checks_per_minute <= 0 or position_count <= 0:
        return "not available"
    seconds = 60 * position_count / checks_per_minute
    if seconds < 60:
        return f"{seconds:.1f} sec"
    return f"{seconds / 60:.1f} min"


if __name__ == "__main__":
    raise SystemExit(main())
