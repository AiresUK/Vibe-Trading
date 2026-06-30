"""Data models for the Copy Trade engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

CopyTradeMode = Literal["mirror_positions"]

#: Minimum quantity change below which an order is skipped (avoid dust orders).
MIN_QTY_THRESHOLD = 0.0001


@dataclass
class CopyTradeConfig:
    """User-configured copy-trade pair.

    Attributes:
        config_id: Unique identifier (``ct_<hex>``).
        leader_profile_id: Connector profile id of the account to follow.
        follower_profile_id: Connector profile id of the account that mirrors.
        scale_ratio: Follower target qty = leader qty * scale_ratio (0 < ratio <= 10).
        max_order_notional: Optional per-order USD cap. ``None`` = unlimited.
        enabled: When False the config exists but no sync cycles are run.
        label: Optional human-readable label.
    """

    config_id: str
    leader_profile_id: str
    follower_profile_id: str
    scale_ratio: float = 1.0
    max_order_notional: float | None = None
    enabled: bool = True
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CopyTradeConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class PositionDelta:
    """One order the follower must place to converge toward the leader's position.

    Attributes:
        symbol: Ticker symbol.
        side: ``"buy"`` or ``"sell"``.
        quantity: Absolute share/unit quantity to trade.
        target_qty: What the follower should hold after this order.
        current_qty: What the follower holds before this order.
        reason: Human-readable explanation of the delta.
    """

    symbol: str
    side: str
    quantity: float
    target_qty: float
    current_qty: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrderResult:
    """Outcome of placing one copy-trade mirror order.

    Attributes:
        symbol: Ticker symbol.
        side: ``"buy"`` or ``"sell"``.
        quantity: Requested quantity.
        status: ``"placed"`` | ``"skipped"`` | ``"error"``.
        broker_response: Raw connector response dict.
        skip_reason: Reason when ``status == "skipped"``.
        error: Error message when ``status == "error"``.
    """

    symbol: str
    side: str
    quantity: float
    status: Literal["placed", "skipped", "error"]
    broker_response: dict[str, Any] = field(default_factory=dict)
    skip_reason: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CopyTradeCycleResult:
    """Summary of one copy-trade synchronisation cycle.

    Attributes:
        config_id: Which config this cycle ran under.
        orders_placed: OrderResult dicts where status == "placed".
        orders_skipped: OrderResult dicts where status == "skipped".
        errors: OrderResult dicts where status == "error".
        leader_positions: Raw positions from the leader at cycle start.
        follower_positions_before: Raw follower positions before any orders.
        equity_before: Follower account equity before orders (USD).
        equity_after: Follower account equity after orders (USD).
        balance_before: Follower account cash balance before orders (USD).
        ts: ISO-8601 UTC timestamp of cycle start.
    """

    config_id: str
    orders_placed: list[dict[str, Any]] = field(default_factory=list)
    orders_skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    leader_positions: list[dict[str, Any]] = field(default_factory=list)
    follower_positions_before: list[dict[str, Any]] = field(default_factory=list)
    equity_before: float | None = None
    equity_after: float | None = None
    balance_before: float | None = None
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DailyPnL:
    """Aggregated P&L for one calendar day.

    Attributes:
        date: Calendar date string (YYYY-MM-DD).
        equity_open: First equity reading of the day (follower account).
        equity_close: Last equity reading of the day (follower account).
        pnl: equity_close - equity_open.
        pnl_pct: P&L as a percentage of equity_open.
        cycles: Number of sync cycles run that day.
        orders_placed: Total orders placed that day.
        orders_skipped: Total orders skipped that day.
        errors: Total errors that day.
    """

    date: str
    equity_open: float | None = None
    equity_close: float | None = None
    pnl: float | None = None
    pnl_pct: float | None = None
    cycles: int = 0
    orders_placed: int = 0
    orders_skipped: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
