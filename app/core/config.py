from decimal import Decimal
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.repricer.worker_groups import (
    ALL_WORKER_GROUPS,
    DEFAULT_WORKER_GROUP_POSITIONS,
    WORKER_GROUP_ALL,
    WORKER_GROUP_FAST_1,
    WORKER_GROUP_FAST_2,
    WORKER_GROUP_ICONS,
    WORKER_GROUP_LABELS,
    WORKER_GROUP_SLOW,
    WorkerGroupInfo,
    default_positions_for_group,
    normalize_worker_group,
    parse_position_list,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "production"
    app_mode: str = "all"
    log_level: str = "INFO"
    dry_run: bool = True

    database_url: str = "postgresql+asyncpg://repricer:repricer@localhost:5432/repricer"
    redis_url: str = "redis://localhost:6379/0"

    telegram_bot_token: str = ""
    owner_telegram_ids: str = ""
    owner_telegram_id: int | None = None

    market_base_url: str = "https://starvell.com"
    market_api_token: str = ""
    market_session_cookie: str = ""
    market_csrf_token: str = ""
    market_account_info_url: str = ""
    market_my_lots_url: str = ""
    market_offers_url: str = "/roblox/packages"
    market_offers_api_url: str = "/api/offers/list-by-category"
    market_offers_limit: int = Field(default=100, ge=1, le=500)

    own_seller_id: str | None = None
    own_seller_username: str | None = None

    proxy_mode: str = "enabled"
    proxy_fast_1_url: str = ""
    proxy_fast_2_url: str = ""
    proxy_slow_url: str = ""
    proxy_fast_1_positions: str = ""
    proxy_fast_2_positions: str = ""
    proxy_slow_positions: str = ""
    proxy_fast_1_request_limit_per_minute: int | None = Field(default=None, ge=1)
    proxy_fast_2_request_limit_per_minute: int | None = Field(default=None, ge=1)
    proxy_slow_request_limit_per_minute: int | None = Field(default=None, ge=1)
    request_burst_limit: int = Field(default=5, ge=1)
    request_min_delay_ms: int = Field(default=300, ge=0)
    request_max_delay_ms: int = Field(default=5000, ge=0)
    request_jitter_ms: int = Field(default=200, ge=0)
    request_backoff_factor: float = Field(default=2.0, ge=1.0)
    safe_mode_enabled: bool = True
    safe_mode_on_429: bool = True
    safe_mode_on_403: bool = True
    safe_mode_cooldown_seconds: float = Field(default=300.0, ge=1)

    request_limit_per_minute: int = Field(default=100, ge=1)
    global_request_limit_per_minute: int = Field(default=300, ge=1)
    worker_fast_1_request_limit_per_minute: int = Field(default=100, ge=1)
    worker_fast_2_request_limit_per_minute: int = Field(default=100, ge=1)
    worker_slow_request_limit_per_minute: int = Field(default=100, ge=1)
    worker_fast_1_positions: str = "500,800,1000"
    worker_fast_2_positions: str = "400,1200,1700,2000"
    worker_slow_positions: str = "40,80,200,2100,2500,3600,4500,10000,22500"
    worker_group: str = WORKER_GROUP_ALL
    public_ip: str | None = None
    position_lock_ttl_seconds: int = Field(default=30, ge=1)
    worker_error_backoff_seconds: float = Field(default=10.0, ge=0)
    worker_safe_mode_error_threshold: int = Field(default=5, ge=1)
    worker_safe_mode_seconds: float = Field(default=60.0, ge=1)
    default_min_rating: Decimal = Decimal("4.5")
    default_price_step: Decimal = Decimal("1")
    default_ignore_no_rating: bool = True
    default_fallback_behavior: str = "keep_current"
    default_min_price: Decimal = Decimal("0")
    default_max_price: Decimal = Decimal("999999")

    high_priority_percent: int = Field(default=70, ge=0, le=100)
    normal_priority_percent: int = Field(default=30, ge=0, le=100)
    scheduler_idle_sleep_seconds: float = Field(default=1.0, ge=0.1)

    @field_validator("owner_telegram_ids")
    @classmethod
    def validate_owner_telegram_ids(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""

        ids: list[str] = []
        for raw_item in value.split(","):
            item = raw_item.strip()
            if not item or not item.isdigit() or int(item) <= 0:
                raise ValueError(
                    "OWNER_TELEGRAM_IDS must contain comma-separated positive integer IDs"
                )
            ids.append(item)
        return ",".join(ids)

    @field_validator("app_mode")
    @classmethod
    def validate_app_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized in {"all", "bot", "worker"}:
            return normalized
        raise ValueError("APP_MODE must be all, bot, or worker")

    @field_validator("worker_group")
    @classmethod
    def validate_worker_group(cls, value: str) -> str:
        return normalize_worker_group(value)

    @field_validator("proxy_mode")
    @classmethod
    def validate_proxy_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized in {"enabled", "disabled"}:
            return normalized
        raise ValueError("PROXY_MODE must be enabled or disabled")

    @field_validator("proxy_fast_1_url", "proxy_fast_2_url", "proxy_slow_url")
    @classmethod
    def validate_proxy_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not value.startswith(("http://", "https://", "socks5://")):
            raise ValueError("proxy URL must start with http://, https://, or socks5://")
        return value

    @field_validator(
        "worker_fast_1_positions",
        "worker_fast_2_positions",
        "worker_slow_positions",
        "proxy_fast_1_positions",
        "proxy_fast_2_positions",
        "proxy_slow_positions",
    )
    @classmethod
    def validate_worker_positions(cls, value: str) -> str:
        parse_position_list(value, ())
        return value

    @model_validator(mode="after")
    def validate_priority_percentages(self) -> "Settings":
        if self.high_priority_percent + self.normal_priority_percent != 100:
            raise ValueError("HIGH_PRIORITY_PERCENT and NORMAL_PRIORITY_PERCENT must sum to 100")
        if self.request_min_delay_ms > self.request_max_delay_ms:
            raise ValueError("REQUEST_MIN_DELAY_MS must be <= REQUEST_MAX_DELAY_MS")
        proxy_limit_total = sum(
            info.request_limit_per_minute
            for info in self.worker_group_infos
        )
        if proxy_limit_total > self.global_request_limit_per_minute:
            raise ValueError(
                "sum of proxy request limits must not exceed GLOBAL_REQUEST_LIMIT_PER_MINUTE"
            )
        assigned: dict[int, list[str]] = {}
        for group, positions in self.worker_group_positions.items():
            for amount in positions:
                assigned.setdefault(amount, []).append(group)
        duplicates = {
            amount: groups
            for amount, groups in assigned.items()
            if len(groups) > 1
        }
        if duplicates:
            details = ", ".join(
                f"{amount}: {'/'.join(groups)}"
                for amount, groups in sorted(duplicates.items())
            )
            raise ValueError(
                "positions must not be assigned to multiple proxy profiles: "
                f"{details}"
            )
        return self

    @property
    def allowed_owner_telegram_ids(self) -> set[int]:
        if self.owner_telegram_ids:
            return {int(item) for item in self.owner_telegram_ids.split(",")}
        if self.owner_telegram_id is not None:
            return {self.owner_telegram_id}
        return set()

    @property
    def assigned_positions(self) -> tuple[int, ...]:
        group = normalize_worker_group(self.worker_group)
        positions = self.worker_group_positions
        if group == WORKER_GROUP_ALL:
            return tuple(
                sorted(
                    {
                        amount
                        for group_positions in positions.values()
                        for amount in group_positions
                    }
                )
            )
        return positions.get(group, default_positions_for_group(group))

    @property
    def worker_request_limit_per_minute(self) -> int:
        group = normalize_worker_group(self.worker_group)
        if group in ALL_WORKER_GROUPS:
            return self.proxy_request_limits[group]
        return self.request_limit_per_minute

    @property
    def worker_group_positions(self) -> dict[str, tuple[int, ...]]:
        return {
            WORKER_GROUP_FAST_1: self._positions_for_group(
                proxy_value=self.proxy_fast_1_positions,
                worker_value=self.worker_fast_1_positions,
                default=DEFAULT_WORKER_GROUP_POSITIONS[WORKER_GROUP_FAST_1],
            ),
            WORKER_GROUP_FAST_2: self._positions_for_group(
                proxy_value=self.proxy_fast_2_positions,
                worker_value=self.worker_fast_2_positions,
                default=DEFAULT_WORKER_GROUP_POSITIONS[WORKER_GROUP_FAST_2],
            ),
            WORKER_GROUP_SLOW: self._positions_for_group(
                proxy_value=self.proxy_slow_positions,
                worker_value=self.worker_slow_positions,
                default=DEFAULT_WORKER_GROUP_POSITIONS[WORKER_GROUP_SLOW],
            ),
        }

    @property
    def proxy_urls(self) -> dict[str, str]:
        return {
            WORKER_GROUP_FAST_1: self.proxy_fast_1_url,
            WORKER_GROUP_FAST_2: self.proxy_fast_2_url,
            WORKER_GROUP_SLOW: self.proxy_slow_url,
        }

    @property
    def proxy_profiles_enabled(self) -> bool:
        return self.proxy_mode == "enabled" and any(self.proxy_urls.values())

    @property
    def proxy_request_limits(self) -> dict[str, int]:
        return {
            WORKER_GROUP_FAST_1: (
                self.proxy_fast_1_request_limit_per_minute
                or self.worker_fast_1_request_limit_per_minute
            ),
            WORKER_GROUP_FAST_2: (
                self.proxy_fast_2_request_limit_per_minute
                or self.worker_fast_2_request_limit_per_minute
            ),
            WORKER_GROUP_SLOW: (
                self.proxy_slow_request_limit_per_minute
                or self.worker_slow_request_limit_per_minute
            ),
        }

    def proxy_url_for_group(self, worker_group: str | None = None) -> str | None:
        if self.proxy_mode != "enabled":
            return None
        group = normalize_worker_group(worker_group or self.worker_group)
        if group == WORKER_GROUP_ALL:
            return None
        return self.proxy_urls.get(group) or None

    @property
    def worker_group_infos(self) -> list[WorkerGroupInfo]:
        limits = {
            WORKER_GROUP_FAST_1: self.proxy_request_limits[WORKER_GROUP_FAST_1],
            WORKER_GROUP_FAST_2: self.proxy_request_limits[WORKER_GROUP_FAST_2],
            WORKER_GROUP_SLOW: self.proxy_request_limits[WORKER_GROUP_SLOW],
        }
        positions = self.worker_group_positions
        return [
            WorkerGroupInfo(
                name=group,
                label=WORKER_GROUP_LABELS[group],
                icon=WORKER_GROUP_ICONS[group],
                positions=positions[group],
                request_limit_per_minute=limits[group],
            )
            for group in ALL_WORKER_GROUPS
        ]

    def _positions_for_group(
        self,
        *,
        proxy_value: str,
        worker_value: str,
        default: tuple[int, ...],
    ) -> tuple[int, ...]:
        if proxy_value.strip():
            return parse_position_list(proxy_value, default)
        return parse_position_list(worker_value, default)


@lru_cache
def get_settings() -> Settings:
    return Settings()
