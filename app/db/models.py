from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class PriorityLevel(StrEnum):
    HIGH = "high"
    NORMAL = "normal"


class PriceStrategyName(StrEnum):
    UNDERCUT_BY_1 = "undercut_by_1"


class FallbackBehavior(StrEnum):
    KEEP_CURRENT = "keep_current"
    SET_MAX_PRICE = "set_max_price"


class UpdateStatus(StrEnum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    DRY_RUN = "dry_run"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class Position(TimestampMixin, Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    robux_amount: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    lot_id: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default=PriorityLevel.NORMAL.value, nullable=False)
    strategy: Mapped[str] = mapped_column(
        String(32), default=PriceStrategyName.UNDERCUT_BY_1.value, nullable=False
    )

    settings: Mapped["PositionSettings"] = relationship(
        back_populates="position", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    state: Mapped["PositionState"] = relationship(
        back_populates="position", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )


class PositionSettings(TimestampMixin, Base):
    __tablename__ = "position_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"), unique=True)
    min_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    max_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    step: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    min_rating: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    ignore_no_rating: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    fallback_behavior: Mapped[str] = mapped_column(String(32), nullable=False)

    position: Mapped[Position] = relationship(back_populates="settings")


class PositionState(TimestampMixin, Base):
    __tablename__ = "position_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"), unique=True)
    last_seen_competitor_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    current_own_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    calculated_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    last_update_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_status: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    position: Mapped[Position] = relationship(back_populates="state")


class CompetitorSnapshot(Base):
    __tablename__ = "competitor_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"), index=True)
    seller_id: Mapped[str | None] = mapped_column(String(128))
    seller_username: Mapped[str | None] = mapped_column(String(255))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    has_rating: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool | None] = mapped_column(Boolean)
    is_ignored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ignore_reason: Mapped[str | None] = mapped_column(String(255))
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )


class PriceUpdateLog(Base):
    __tablename__ = "price_update_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"), index=True)
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    new_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    competitor_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    competitor_seller_id: Mapped[str | None] = mapped_column(String(128))
    competitor_seller_username: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )


class ApiRequestLog(Base):
    __tablename__ = "api_request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    request_type: Mapped[str] = mapped_column(String(64), nullable=False)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )


class AppSetting(TimestampMixin, Base):
    __tablename__ = "app_settings"
    __table_args__ = (UniqueConstraint("key", name="uq_app_settings_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class WorkerState(Base):
    __tablename__ = "worker_state"
    __table_args__ = (UniqueConstraint("name", name="uq_worker_state_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_cycle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_position_amount: Mapped[int | None] = mapped_column(Integer)
    last_status: Mapped[str | None] = mapped_column(String(32))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class BotState(Base):
    __tablename__ = "bot_state"
    __table_args__ = (UniqueConstraint("telegram_user_id", name="uq_bot_state_telegram_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
