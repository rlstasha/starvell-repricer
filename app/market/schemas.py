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
class MyLotSummary:
    lot_id: str | None
    title: str | None
    position_amount: int | None
    price: Decimal | None
    seller_id: str | None = None
    stock: int | None = None
    is_active: bool | None = None
    raw_payload: dict[str, Any] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class AccountInfo:
    seller_id: str | None
    seller_username: str | None
    raw_payload: dict[str, Any] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class StarvellConnectionCheck:
    account_endpoint_configured: bool
    lots_endpoint_configured: bool
    authorized: bool | None
    account_info: AccountInfo | None
    my_lots: list[MyLotSummary]
    account_status_code: int | None = None
    lots_status_code: int | None = None
    account_error: str | None = None
    lots_error: str | None = None


@dataclass(frozen=True)
class UpdateResult:
    success: bool
    raw_payload: dict[str, Any] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class PriceUpdateAttemptResult:
    variant: str
    method: str
    url: str
    request_content_type: str
    payload: dict[str, Any]
    status_code: int | None
    response_content_type: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: str | None = None
    success: bool = False
    reason: str | None = None
