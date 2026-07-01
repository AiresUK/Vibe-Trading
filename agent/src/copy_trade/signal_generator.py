"""AI-driven signal generator.

Uses the platform's configured LLM (via ``src.providers.llm.build_llm``) to
analyse recent price bars for each symbol in a watchlist and return structured
trading signals (buy / sell / hold).

The LLM receives a compact OHLCV table plus basic statistical context and is
asked to respond with a single JSON object.  No internet access or external
data provider is required beyond the broker's own historical-bar endpoint.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from typing import Any

from src.copy_trade.signal_models import Signal, SignalConfig

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are an expert professional trader and technical analyst specialising in forex, \
indices, and commodities.

You will receive recent OHLCV price data for a single instrument and must return \
a trading signal as a single JSON object with exactly these keys:

{
  "direction":        "buy" | "sell" | "hold",
  "confidence":       <float 0.0–1.0>,
  "reasoning":        "<1-3 sentence explanation>",
  "position_size_pct": <integer 0–100>
}

Rules:
- direction: "buy" if you expect price to rise, "sell" to fall, "hold" if unclear.
- confidence: how certain you are (0.5 = coin flip, 0.9 = very strong signal).
- reasoning: concise explanation referencing the actual price data provided.
- position_size_pct: relative to the maximum allowed risk — 100 = full size, \
50 = half size.  Set to 0 when direction is "hold".
- Return ONLY the JSON object, no markdown fences, no extra text.
"""


def _format_bars(bars: list[dict[str, Any]], limit: int) -> str:
    """Format OHLCV bars as a compact table for the LLM prompt."""
    tail = bars[-limit:]
    lines = ["timestamp,open,high,low,close,volume"]
    for b in tail:
        ts = str(b.get("timestamp") or b.get("time") or b.get("date") or "")[:16]
        o = b.get("open", "")
        h = b.get("high", "")
        lo = b.get("low", "")
        c = b.get("close", "")
        v = b.get("volume", "")
        lines.append(f"{ts},{o},{h},{lo},{c},{v}")
    return "\n".join(lines)


def _compute_context(bars: list[dict[str, Any]]) -> str:
    """Derive basic statistical context from bars to enrich the prompt."""
    closes: list[float] = []
    for b in bars:
        try:
            closes.append(float(b.get("close", 0)))
        except (TypeError, ValueError):
            pass
    if len(closes) < 5:
        return ""

    latest = closes[-1]
    sma20 = statistics.mean(closes[-20:]) if len(closes) >= 20 else statistics.mean(closes)
    trend = "above" if latest > sma20 else "below"

    highs = [float(b.get("high", 0)) for b in bars[-14:] if b.get("high")]
    lows = [float(b.get("low", 0)) for b in bars[-14:] if b.get("low")]
    atr_approx = statistics.mean([h - l for h, l in zip(highs, lows)]) if highs and lows else 0

    pct_change = ((latest - closes[-2]) / closes[-2] * 100) if len(closes) >= 2 else 0

    return (
        f"\nContext: latest close={latest:.5f}, 20-bar SMA={sma20:.5f} "
        f"(price is {trend} SMA), approx 14-bar ATR={atr_approx:.5f}, "
        f"last bar change={pct_change:+.3f}%"
    )


def _extract_bars(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise broker historical-bars response into a list of OHLCV dicts."""
    for key in ("bars", "candles", "data", "history", "ohlcv"):
        val = raw.get(key)
        if isinstance(val, list):
            return val
    if isinstance(raw.get("result"), list):
        return raw["result"]
    return []


def _parse_signal_json(text: str, symbol: str) -> dict[str, Any] | None:
    """Extract the JSON object from LLM response text."""
    # Strip markdown fences if present.
    text = re.sub(r"```(?:json)?", "", text).strip()
    # Try to find first {...} block.
    match = re.search(r"\{[^{}]+\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("[signal] Failed to parse LLM JSON for %s: %s | raw=%r", symbol, exc, text[:200])
        return None


def generate_signal(
    symbol: str,
    bars_raw: dict[str, Any],
    quote_raw: dict[str, Any],
    config: SignalConfig,
    open_position: dict[str, Any] | None = None,
) -> Signal:
    """Call the configured LLM and return a trading Signal for *symbol*.

    Falls back to ``Signal(direction="hold", confidence=0.0)`` on any error so
    the cycle can continue with other symbols.

    Args:
        open_position: Tracked position dict for this symbol (if one exists).
            When provided the LLM is explicitly asked to evaluate whether to
            hold the current position or exit early.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from src.providers.llm import build_llm

    bars = _extract_bars(bars_raw)
    if not bars:
        logger.warning("[signal] No bars for %s — skipping", symbol)
        return Signal(symbol=symbol, direction="hold", confidence=0.0, reasoning="No historical data available.")

    # Best-effort current price from quote.
    current_price = 0.0
    for key in ("last", "price", "close", "bid", "ask"):
        try:
            val = float(quote_raw.get(key) or 0)
            if val > 0:
                current_price = val
                break
        except (TypeError, ValueError):
            pass

    ohlcv_table = _format_bars(bars, limit=config.lookback_bars)
    context = _compute_context(bars)

    # Build open-position context paragraph if we have a live trade.
    position_ctx = ""
    if open_position:
        pos_side = open_position.get("side", "")
        entry_price = 0.0
        try:
            entry_price = float(open_position.get("entry_price", 0))
        except (TypeError, ValueError):
            pass
        entry_time = open_position.get("entry_time", "")
        if entry_price > 0 and current_price > 0:
            raw_pnl = (current_price - entry_price) / entry_price * 100
            pnl_pct = raw_pnl if pos_side == "buy" else -raw_pnl
            position_ctx = (
                f"\nOPEN POSITION: You have an open {pos_side.upper()} entered at {entry_price:.5f} "
                f"(opened {entry_time}). Current price: {current_price:.5f} "
                f"(unrealised P&L: {pnl_pct:+.2f}%). "
                "Re-evaluate this position: if the trade is going against you and structure supports exit, "
                "signal the opposite direction to close it early. "
                "If momentum still favours the original direction, signal that same direction (or 'hold' if unsure)."
            )

    user_msg = (
        f"Symbol: {symbol}\n"
        f"Timeframe: {config.timeframe}\n"
        f"Bars (oldest → newest):\n{ohlcv_table}"
        f"{context}"
        f"{position_ctx}\n\n"
        "Provide your trading signal as JSON."
    )

    try:
        llm = build_llm()
        response = llm.invoke(
            [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=user_msg)]
        )
        raw_text = str(getattr(response, "content", response) or "")
    except Exception as exc:
        logger.warning("[signal] LLM call failed for %s: %s", symbol, exc)
        return Signal(symbol=symbol, direction="hold", confidence=0.0, reasoning=f"LLM error: {exc}")

    parsed = _parse_signal_json(raw_text, symbol)
    if parsed is None:
        return Signal(symbol=symbol, direction="hold", confidence=0.0, reasoning="Could not parse LLM response.")

    direction = str(parsed.get("direction", "hold")).lower()
    if direction not in ("buy", "sell", "hold"):
        direction = "hold"

    try:
        confidence = float(parsed.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.0

    try:
        position_size_pct = float(parsed.get("position_size_pct", 100.0))
        position_size_pct = max(0.0, min(100.0, position_size_pct))
    except (TypeError, ValueError):
        position_size_pct = 100.0

    reasoning = str(parsed.get("reasoning", ""))[:500]

    logger.info(
        "[signal] %s → %s (conf=%.2f, size=%d%%) | %s",
        symbol, direction.upper(), confidence, int(position_size_pct), reasoning[:80],
    )

    return Signal(
        symbol=symbol,
        direction=direction,  # type: ignore[arg-type]
        confidence=confidence,
        reasoning=reasoning,
        position_size_pct=position_size_pct,
        current_price=current_price,
    )
