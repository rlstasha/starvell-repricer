from app.core.config import get_settings
from app.repricer.worker_groups import ALL_WORKER_GROUPS


def main() -> int:
    settings = get_settings()
    lines: list[str] = [
        f"Global limit: {settings.global_request_limit_per_minute}/min",
        f"Proxy mode: {settings.proxy_mode}",
        "",
    ]

    total_proxy_limit = 0
    for info in settings.worker_group_infos:
        total_proxy_limit += info.request_limit_per_minute
        lines.extend(
            [
                f"proxy_{info.name}",
                "",
                "positions:",
                *[str(amount) for amount in info.positions],
                "",
                "limit:",
                f"{info.request_limit_per_minute}/min",
                "",
                "approx:",
                _frequency(info.request_limit_per_minute, len(info.positions)),
                "",
            ]
        )

    errors = _validate(settings, total_proxy_limit)
    if errors:
        lines.append("Configuration errors:")
        lines.extend(f"- {error}" for error in errors)
        print("\n".join(lines))
        return 1

    print("\n".join(lines).rstrip())
    return 0


def _validate(settings, total_proxy_limit: int) -> list[str]:
    errors: list[str] = []
    if total_proxy_limit > settings.global_request_limit_per_minute:
        errors.append(
            "sum of proxy limits is greater than GLOBAL_REQUEST_LIMIT_PER_MINUTE"
        )
    groups = {info.name for info in settings.worker_group_infos}
    if groups != set(ALL_WORKER_GROUPS):
        errors.append("proxy profile list is incomplete")

    assigned: dict[int, list[str]] = {}
    for info in settings.worker_group_infos:
        if not info.positions:
            errors.append(f"proxy_{info.name} has no assigned positions")
        for amount in info.positions:
            assigned.setdefault(amount, []).append(info.name)
    for amount, duplicate_groups in sorted(assigned.items()):
        if len(duplicate_groups) > 1:
            errors.append(
                f"position {amount} is assigned to multiple proxy profiles: "
                f"{', '.join(duplicate_groups)}"
            )
    return errors


def _frequency(request_limit: int, position_count: int) -> str:
    if request_limit <= 0 or position_count <= 0:
        return "not available"
    seconds = 60 * position_count / request_limit
    if seconds < 60:
        return f"{seconds:.1f} sec"
    return f"{seconds / 60:.1f} min"


if __name__ == "__main__":
    raise SystemExit(main())
