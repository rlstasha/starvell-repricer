from decimal import Decimal
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "production"
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

    own_seller_id: str | None = None
    own_seller_username: str | None = None

    request_limit_per_minute: int = Field(default=100, ge=1)
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

    @model_validator(mode="after")
    def validate_priority_percentages(self) -> "Settings":
        if self.high_priority_percent + self.normal_priority_percent != 100:
            raise ValueError("HIGH_PRIORITY_PERCENT and NORMAL_PRIORITY_PERCENT must sum to 100")
        return self

    @property
    def allowed_owner_telegram_ids(self) -> set[int]:
        if self.owner_telegram_ids:
            return {int(item) for item in self.owner_telegram_ids.split(",")}
        if self.owner_telegram_id is not None:
            return {self.owner_telegram_id}
        return set()


@lru_cache
def get_settings() -> Settings:
    return Settings()
