from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class MarketOffer:
    position_amount: int
    price: Decimal
    seller_id: str | None
    seller_username: str | None
    rating: Decimal | None
    is_active: bool | None = None
    raw_payload: dict[str, Any] | None = field(default=None, compare=False)

    @property
    def key(self) -> str:
        seller_part = self.seller_id or self.seller_username or "unknown"
        return f"{self.position_amount}:{seller_part}:{self.price}"


@dataclass(frozen=True)
class OwnLot:
    position_amount: int
    price: Decimal
    lot_id: str | None = None
    raw_payload: dict[str, Any] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class AccountInfo:
    seller_id: str | None
    seller_username: str | None
    raw_payload: dict[str, Any] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class UpdateResult:
    success: bool
    raw_payload: dict[str, Any] | None = field(default=None, compare=False)

