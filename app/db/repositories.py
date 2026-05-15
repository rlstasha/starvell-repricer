from datetime import UTC, datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    AppSetting,
    ApiRequestLog,
    BotState,
    CompetitorSnapshot,
    FallbackBehavior,
    Position,
    PositionSettings,
    PositionState,
    PriceUpdateLog,
    PriorityLevel,
    WorkerHeartbeat,
    UpdateStatus,
    WorkerState,
)
from app.market.schemas import MarketOffer


DEFAULT_POSITIONS = [
    40,
    80,
    200,
    400,
    500,
    800,
    1000,
    1200,
    1700,
    2000,
    2100,
    2500,
    3600,
    4500,
    10000,
    22500,
]
HIGH_PRIORITY_POSITIONS = {400, 500, 800, 1000, 1200, 1700, 2000}
DEFAULT_LOT_IDS = {
    80: "1996",
    200: "1998",
    400: "1999",
    500: "2000",
    800: "2002",
    1000: "2003",
    1200: "2004",
    1700: "2005",
    2000: "2006",
    2100: "2007",
    2500: "2008",
    3600: "2009",
    4500: "2010",
    10000: "2011",
    22500: "2012",
}


class PositionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def seed_default_positions(
        self,
        *,
        min_price: Decimal,
        max_price: Decimal,
        step: Decimal,
        min_rating: Decimal,
        ignore_no_rating: bool,
        fallback_behavior: str = FallbackBehavior.KEEP_CURRENT.value,
    ) -> None:
        existing = {
            position.robux_amount: position
            for position in await self.session.scalars(
                select(Position).where(Position.robux_amount.in_(DEFAULT_POSITIONS))
            )
        }
        for amount, position in existing.items():
            if position.lot_id is None and amount in DEFAULT_LOT_IDS:
                position.lot_id = DEFAULT_LOT_IDS[amount]

        for amount in DEFAULT_POSITIONS:
            if amount in existing:
                continue

            position = Position(
                robux_amount=amount,
                lot_id=DEFAULT_LOT_IDS.get(amount),
                enabled=True,
                priority=(
                    PriorityLevel.HIGH.value
                    if amount in HIGH_PRIORITY_POSITIONS
                    else PriorityLevel.NORMAL.value
                ),
            )
            position.settings = PositionSettings(
                min_price=min_price,
                max_price=max_price,
                step=step,
                min_rating=min_rating,
                ignore_no_rating=ignore_no_rating,
                fallback_behavior=fallback_behavior,
            )
            position.state = PositionState()
            self.session.add(position)

    async def list_positions(self) -> list[Position]:
        result = await self.session.scalars(
            select(Position)
            .options(selectinload(Position.settings), selectinload(Position.state))
            .order_by(Position.robux_amount.asc())
        )
        return list(result)

    async def list_enabled_positions(self) -> list[Position]:
        result = await self.session.scalars(
            select(Position)
            .where(Position.enabled.is_(True))
            .options(selectinload(Position.settings), selectinload(Position.state))
            .order_by(Position.robux_amount.asc())
        )
        return list(result)

    async def get_by_amount(self, amount: int) -> Position | None:
        return await self.session.scalar(
            select(Position)
            .where(Position.robux_amount == amount)
            .options(selectinload(Position.settings), selectinload(Position.state))
        )

    async def set_enabled(self, amount: int, enabled: bool) -> bool:
        position = await self.get_by_amount(amount)
        if position is None:
            return False
        position.enabled = enabled
        return True

    async def set_lot_id(self, amount: int, lot_id: str | None) -> bool:
        position = await self.get_by_amount(amount)
        if position is None:
            return False
        position.lot_id = lot_id
        return True

    async def toggle_priority(self, amount: int) -> Position | None:
        position = await self.get_by_amount(amount)
        if position is None:
            return None
        position.priority = (
            PriorityLevel.NORMAL.value
            if position.priority == PriorityLevel.HIGH.value
            else PriorityLevel.HIGH.value
        )
        return position

    async def update_setting(self, amount: int, field_name: str, value) -> bool:
        position = await self.get_by_amount(amount)
        if position is None or position.settings is None:
            return False
        if not hasattr(position.settings, field_name):
            raise ValueError(f"Unknown setting: {field_name}")
        setattr(position.settings, field_name, value)
        return True

    async def update_state(
        self,
        position: Position,
        *,
        last_seen_competitor_price: Decimal | None,
        current_own_price: Decimal | None,
        calculated_price: Decimal | None,
        error_status: str | None,
        error_message: str | None,
        success: bool,
    ) -> None:
        now = datetime.now(UTC)
        if position.state is None:
            position.state = PositionState()
        position.state.last_seen_competitor_price = last_seen_competitor_price
        position.state.current_own_price = current_own_price
        position.state.calculated_price = calculated_price
        position.state.last_update_time = now
        position.state.error_status = error_status
        position.state.error_message = error_message
        if success:
            position.state.last_success_time = now

    async def add_competitor_snapshots(
        self,
        position: Position,
        offers: Iterable[MarketOffer],
        ignored_reasons: dict[str, str],
    ) -> None:
        for offer in offers:
            key = offer.key
            self.session.add(
                CompetitorSnapshot(
                    position_id=position.id,
                    seller_id=offer.seller_id,
                    seller_username=offer.seller_username,
                    price=offer.price,
                    rating=offer.rating,
                    has_rating=offer.rating is not None,
                    is_active=offer.is_active,
                    is_ignored=key in ignored_reasons,
                    ignore_reason=ignored_reasons.get(key),
                    raw_payload=offer.raw_payload,
                )
            )

    async def add_price_log(
        self,
        position: Position,
        *,
        old_price: Decimal | None,
        new_price: Decimal | None,
        competitor_price: Decimal | None,
        competitor_seller_id: str | None,
        competitor_seller_username: str | None,
        status: str | UpdateStatus,
        reason: str | None,
    ) -> None:
        self.session.add(
            PriceUpdateLog(
                position_id=position.id,
                old_price=old_price,
                new_price=new_price,
                competitor_price=competitor_price,
                competitor_seller_id=competitor_seller_id,
                competitor_seller_username=competitor_seller_username,
                status=status.value if isinstance(status, UpdateStatus) else status,
                reason=reason,
            )
        )

    async def list_recent_competitors(
        self,
        position: Position,
        *,
        limit: int = 10,
    ) -> list[CompetitorSnapshot]:
        result = await self.session.scalars(
            select(CompetitorSnapshot)
            .where(CompetitorSnapshot.position_id == position.id)
            .order_by(CompetitorSnapshot.seen_at.desc())
            .limit(limit)
        )
        return list(result)

    async def list_recent_active_competitors(
        self,
        position: Position,
        *,
        limit: int = 20,
    ) -> list[CompetitorSnapshot]:
        result = await self.session.scalars(
            select(CompetitorSnapshot)
            .where(
                CompetitorSnapshot.position_id == position.id,
                CompetitorSnapshot.is_ignored.is_(False),
            )
            .order_by(CompetitorSnapshot.seen_at.desc())
            .limit(limit)
        )
        return list(result)

    async def count_price_logs(self, status: str | UpdateStatus) -> int:
        status_value = status.value if isinstance(status, UpdateStatus) else status
        return int(
            await self.session.scalar(
                select(func.count()).select_from(PriceUpdateLog).where(PriceUpdateLog.status == status_value)
            )
            or 0
        )

    async def list_recent_price_logs(self, *, limit: int = 10) -> list[PriceUpdateLog]:
        result = await self.session.scalars(
            select(PriceUpdateLog).order_by(PriceUpdateLog.created_at.desc()).limit(limit)
        )
        return list(result)

    async def list_recent_price_logs_with_amounts(
        self, *, limit: int = 10
    ) -> list[tuple[PriceUpdateLog, int | None]]:
        result = await self.session.execute(
            select(PriceUpdateLog, Position.robux_amount)
            .join(Position, Position.id == PriceUpdateLog.position_id, isouter=True)
            .order_by(PriceUpdateLog.created_at.desc())
            .limit(limit)
        )
        return [(log, amount) for log, amount in result.all()]

    async def list_latest_price_logs_by_position(self) -> list[tuple[Position, PriceUpdateLog | None]]:
        positions = await self.list_positions()
        items: list[tuple[Position, PriceUpdateLog | None]] = []
        for position in positions:
            log = await self.session.scalar(
                select(PriceUpdateLog)
                .where(PriceUpdateLog.position_id == position.id)
                .order_by(PriceUpdateLog.created_at.desc())
                .limit(1)
            )
            items.append((position, log))
        return items

    async def list_recent_errors(self, *, limit: int = 5) -> list[PriceUpdateLog]:
        result = await self.session.scalars(
            select(PriceUpdateLog)
            .where(PriceUpdateLog.status == UpdateStatus.FAILED.value)
            .order_by(PriceUpdateLog.created_at.desc())
            .limit(limit)
        )
        return list(result)

    async def list_recent_errors_with_positions(
        self, *, limit: int = 5
    ) -> list[tuple[PriceUpdateLog, Position | None]]:
        result = await self.session.execute(
            select(PriceUpdateLog, Position)
            .join(Position, Position.id == PriceUpdateLog.position_id, isouter=True)
            .where(PriceUpdateLog.status == UpdateStatus.FAILED.value)
            .order_by(PriceUpdateLog.created_at.desc())
            .limit(limit)
        )
        return [(log, position) for log, position in result.all()]

    async def count_by_priority(self, *, enabled_only: bool = False) -> dict[str, int]:
        query = select(Position.priority, func.count()).group_by(Position.priority)
        if enabled_only:
            query = query.where(Position.enabled.is_(True))
        result = await self.session.execute(query)
        return {priority: int(count) for priority, count in result.all()}


class ApiLogRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(
        self,
        *,
        method: str,
        url: str,
        status_code: int | None,
        request_type: str,
        position_id: int | None = None,
    ) -> None:
        self.session.add(
            ApiRequestLog(
                method=method,
                url=url,
                status_code=status_code,
                request_type=request_type,
                position_id=position_id,
            )
        )


class BotStateRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def set_state(self, telegram_user_id: int, state: str, payload: dict | None = None) -> None:
        existing = await self.session.scalar(
            select(BotState).where(BotState.telegram_user_id == telegram_user_id)
        )
        if existing:
            existing.state = state
            existing.payload_json = payload
            return
        self.session.add(
            BotState(telegram_user_id=telegram_user_id, state=state, payload_json=payload)
        )


class AppSettingsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def ensure_defaults(self, *, dry_run: bool) -> None:
        await self.set_default("dry_run", self._bool_to_string(dry_run))

    async def set_default(self, key: str, value: str) -> None:
        existing = await self.get(key)
        if existing is None:
            self.session.add(AppSetting(key=key, value=value))

    async def get(self, key: str) -> AppSetting | None:
        return await self.session.scalar(select(AppSetting).where(AppSetting.key == key))

    async def get_value(self, key: str, default: str | None = None) -> str | None:
        setting = await self.get(key)
        return setting.value if setting else default

    async def set_value(self, key: str, value: str) -> None:
        setting = await self.get(key)
        if setting:
            setting.value = value
            return
        self.session.add(AppSetting(key=key, value=value))

    async def get_bool(self, key: str, default: bool = False) -> bool:
        value = await self.get_value(key)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on", "вкл", "да"}

    async def set_bool(self, key: str, value: bool) -> None:
        await self.set_value(key, self._bool_to_string(value))

    def _bool_to_string(self, value: bool) -> str:
        return "true" if value else "false"


class WorkerStateRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, name: str = "repricer") -> WorkerState | None:
        return await self.session.scalar(select(WorkerState).where(WorkerState.name == name))

    async def list_all(self) -> list[WorkerState]:
        result = await self.session.scalars(
            select(WorkerState).order_by(WorkerState.updated_at.desc())
        )
        return list(result)

    async def mark_cycle(
        self,
        *,
        name: str = "repricer",
        position_amount: int | None,
        status: str,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        state = await self.get(name)
        if state is None:
            self.session.add(
                WorkerState(
                    name=name,
                    last_heartbeat_at=now,
                    last_cycle_at=now,
                    last_position_amount=position_amount,
                    last_status=status,
                    last_error=error,
                )
            )
            return

        state.last_heartbeat_at = now
        state.last_cycle_at = now
        state.last_position_amount = position_amount
        state.last_status = status
        state.last_error = error


class WorkerHeartbeatRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(
        self,
        *,
        worker_group: str,
        hostname: str | None,
        public_ip: str | None,
        assigned_positions: list[int],
        request_limit_per_minute: int,
        status: str,
        errors_429: int,
        errors_403: int,
        errors_timeout: int,
        consecutive_errors: int,
        safe_mode: bool,
        dry_run: bool,
    ) -> None:
        now = datetime.now(UTC)
        heartbeat = await self.session.scalar(
            select(WorkerHeartbeat).where(WorkerHeartbeat.worker_group == worker_group)
        )
        if heartbeat is None:
            self.session.add(
                WorkerHeartbeat(
                    worker_group=worker_group,
                    hostname=hostname,
                    public_ip=public_ip,
                    assigned_positions=assigned_positions,
                    request_limit_per_minute=request_limit_per_minute,
                    last_seen_at=now,
                    status=status,
                    errors_429=errors_429,
                    errors_403=errors_403,
                    errors_timeout=errors_timeout,
                    consecutive_errors=consecutive_errors,
                    safe_mode=safe_mode,
                    dry_run=dry_run,
                )
            )
            return

        heartbeat.hostname = hostname
        heartbeat.public_ip = public_ip
        heartbeat.assigned_positions = assigned_positions
        heartbeat.request_limit_per_minute = request_limit_per_minute
        heartbeat.last_seen_at = now
        heartbeat.status = status
        heartbeat.errors_429 = errors_429
        heartbeat.errors_403 = errors_403
        heartbeat.errors_timeout = errors_timeout
        heartbeat.consecutive_errors = consecutive_errors
        heartbeat.safe_mode = safe_mode
        heartbeat.dry_run = dry_run

    async def list_all(self) -> list[WorkerHeartbeat]:
        result = await self.session.scalars(
            select(WorkerHeartbeat).order_by(WorkerHeartbeat.worker_group.asc())
        )
        return list(result)
