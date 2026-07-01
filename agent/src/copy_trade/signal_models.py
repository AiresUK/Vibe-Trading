"""Data models for AI-driven signal generation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass
class SignalConfig:
    """Configuration for AI-driven signal-based trading.

    Attributes:
        config_id: Unique identifier (``sig_<hex>``).
        profile_id: Connector profile to trade on.
        watchlist: Symbols to analyse each cycle.
        timeframe: Bar period for analysis (e.g. ``"1h"``, ``"4h"``, ``"1d"``).
        lookback_bars: Number of historical bars to feed the LLM (20–200).
        min_confidence: Minimum LLM confidence to act on a signal (0–1).
        risk_per_trade_pct: Equity % to size each position (e.g. 1.0 = 1%).
        max_positions: Maximum concurrent open positions.
        enabled: When False no cycles are run.
        label: Optional human-readable label.
    """

    config_id: str
    profile_id: str
    watchlist: list[str] = field(default_factory=list)
    timeframe: str = "1h"
    lookback_bars: int = 50
    min_confidence: float = 0.65
    risk_per_trade_pct: float = 1.0
    max_positions: int = 5
    interval_minutes: int | None = 15    # minutes between auto-cycles; None = manual only
    max_trade_hours: int = 48            # auto-close open positions after this many hours
    enabled: bool = True
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SignalConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Signal:
    """AI-generated signal for one symbol.

    Attributes:
        symbol: Ticker symbol.
        direction: ``"buy"``, ``"sell"``, or ``"hold"``.
        confidence: LLM confidence 0.0–1.0.
        reasoning: LLM explanation.
        position_size_pct: Suggested size as % of max risk (0–100).
        current_price: Mid price at signal time.
    """

    symbol: str
    direction: Literal["buy", "sell", "hold"]
    confidence: float
    reasoning: str = ""
    position_size_pct: float = 100.0
    current_price: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SignalCycleResult:
    """Summary of one AI signal cycle.

    Attributes:
        config_id: Which signal config ran.
        ts: ISO-8601 UTC timestamp.
        signals: All signals generated (including holds).
        orders_placed: Orders successfully placed.
        orders_skipped: Orders skipped (low confidence, max positions, etc.).
        errors: Errors encountered.
        equity_before: Account equity before orders.
        equity_after: Account equity after orders.
    """

    config_id: str
    ts: str = ""
    signals: list[dict[str, Any]] = field(default_factory=list)
    orders_placed: list[dict[str, Any]] = field(default_factory=list)
    orders_skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    equity_before: float | None = None
    equity_after: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
