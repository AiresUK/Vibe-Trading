"""AI signal execution engine.

Flow per cycle:
1. Snapshot account equity.
2. For each symbol in watchlist, fetch bars + quote.
3. Call LLM signal generator for each symbol.
4. Filter: direction != hold AND confidence >= min_confidence.
5. Size positions from equity * risk_per_trade_pct * (position_size_pct / 100).
6. Apply max_positions cap (highest-confidence signals win).
7. Place market orders via existing place_order safety stack.
8. Snapshot equity after.
9. Persist cycle result.
"""

from __future__ import annotations

import logging
from typing import Any

from src.copy_trade.engine import _extract_equity, _extract_positions
from src.copy_trade.models import MIN_QTY_THRESHOLD, OrderResult
from src.copy_trade.signal_generator import generate_signal
from src.copy_trade.signal_models import Signal, SignalConfig, SignalCycleResult
from src.copy_trade.state import (
    append_signal_cycle_result,
    get_day_start_equity,
    get_prop_firm_rules,
    utc_now_iso,
)
from src.trading.service import get_account, get_historical_bars, get_positions, get_quote, place_order

logger = logging.getLogger(__name__)

_ATR_PERIOD = 14
_SL_ATR_MULTIPLE = 2.0   # stop loss = 2× ATR from entry
_TP_ATR_MULTIPLE = 3.0   # take profit = 3× ATR (1:1.5 R:R) — bonus target only


def _compute_atr(bars_raw: dict, period: int = _ATR_PERIOD) -> float | None:
    """Approximate ATR from raw bars dict returned by get_historical_bars."""
    bars = bars_raw.get("bars", [])
    if len(bars) < 2:
        return None
    true_ranges: list[float] = []
    for i in range(1, len(bars)):
        high = float(bars[i].get("high", 0))
        low = float(bars[i].get("low", 0))
        prev_close = float(bars[i - 1].get("close", 0))
        if high == 0 or low == 0:
            continue
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    if not true_ranges:
        return None
    recent = true_ranges[-period:]
    return sum(recent) / len(recent)


def run_signal_cycle(
    config: SignalConfig,
    session_id: str = "",
    dry_run: bool = False,
) -> SignalCycleResult:
    """Execute one AI signal cycle: analyse market, generate signals, place orders.

    Args:
        config: Signal configuration.
        session_id: Originating session id forwarded to ``place_order``.
        dry_run: If True, compute signals and show what would be traded but do
            not place any real orders.

    Returns:
        SignalCycleResult with full details of signals generated and orders placed.
    """
    ts = utc_now_iso()
    result = SignalCycleResult(config_id=config.config_id, ts=ts)

    # 1. Snapshot equity.
    equity: float | None = None
    try:
        acct = get_account(config.profile_id)
        equity, _ = _extract_equity(acct)
        result.equity_before = equity
    except Exception as exc:
        logger.debug("[signal] Could not read equity: %s", exc)

    # 2. Prop firm rule check — halt before generating any signals.
    pf_rules = get_prop_firm_rules(config.config_id)
    if pf_rules is not None and pf_rules.enabled and equity is not None:
        from src.copy_trade.prop_firm import check_rules

        day_start = get_day_start_equity(config.config_id)
        pf_check = check_rules(pf_rules, equity, day_start, ts=ts)

        if pf_check.should_halt:
            halt_msgs = [c.message for c in pf_check.checks if c.status == "halt"]
            logger.warning("[signal] PROP FIRM HALT config=%s: %s", config.config_id, halt_msgs)
            result.errors.append({
                "symbol": "*",
                "error": f"Prop firm rule halt: {'; '.join(halt_msgs)}",
                "prop_firm_halted": True,
            })
            append_signal_cycle_result(result)
            return result

        if pf_check.should_alert:
            alert_msgs = [c.message for c in pf_check.checks if c.status in ("alert", "target_reached")]
            logger.info("[signal] PROP FIRM ALERT config=%s: %s", config.config_id, alert_msgs)

    # 3. Read current positions (to avoid doubling into existing).
    current_positions: dict[str, float] = {}
    try:
        pos_raw = get_positions(config.profile_id)
        current_positions = _extract_positions(pos_raw)
    except Exception as exc:
        logger.debug("[signal] Could not read positions: %s", exc)

    # 4. Generate signals for each symbol — keep bars for ATR-based SL/TP later.
    signals: list[Signal] = []
    bars_by_symbol: dict[str, dict] = {}
    for symbol in config.watchlist:
        try:
            bars_raw = get_historical_bars(
                symbol,
                profile_id=config.profile_id,
                period=config.timeframe,
                limit=config.lookback_bars + 10,  # small buffer
            )
        except Exception as exc:
            logger.warning("[signal] Could not fetch bars for %s: %s", symbol, exc)
            result.errors.append({"symbol": symbol, "error": f"bars fetch failed: {exc}"})
            bars_raw = {}

        bars_by_symbol[symbol] = bars_raw

        try:
            quote_raw = get_quote(symbol, config.profile_id)
        except Exception as exc:
            logger.debug("[signal] Could not fetch quote for %s: %s", symbol, exc)
            quote_raw = {}

        sig = generate_signal(symbol, bars_raw, quote_raw, config)
        signals.append(sig)
        result.signals.append(sig.to_dict())

    # 5. Filter actionable signals.
    actionable = [
        s for s in signals
        if s.direction in ("buy", "sell") and s.confidence >= config.min_confidence
    ]

    # Sort by confidence descending; cap at max_positions.
    actionable.sort(key=lambda s: s.confidence, reverse=True)

    # Count existing open positions toward the cap.
    open_count = len(current_positions)
    available_slots = max(0, config.max_positions - open_count)

    # Separate buys (new positions) and sells (closing positions).
    # Sells on held symbols can always proceed; new buys consume slots.
    sells = [s for s in actionable if s.direction == "sell"]
    buys = [s for s in actionable if s.direction == "buy"]
    buys = buys[:available_slots]  # respect position cap for new entries

    to_execute = sells + buys

    # 5. Place orders.
    for sig in to_execute:
        # Compute order quantity from equity risk.
        qty = 0.0
        if sig.current_price > 0 and equity is not None and equity > 0:
            risk_notional = equity * (config.risk_per_trade_pct / 100.0) * (sig.position_size_pct / 100.0)
            qty = risk_notional / sig.current_price
            qty = round(qty, 8)

        if qty < MIN_QTY_THRESHOLD:
            skip_detail = {
                "symbol": sig.symbol,
                "direction": sig.direction,
                "confidence": sig.confidence,
                "reason": "quantity below minimum (price or equity unavailable)",
            }
            result.orders_skipped.append(skip_detail)
            logger.info("[signal] %s %s skipped — qty too small", sig.direction.upper(), sig.symbol)
            continue

        # Compute ATR-based stop loss (2× ATR) and take profit (3× ATR bonus).
        stop_loss: float | None = None
        take_profit: float | None = None
        atr = _compute_atr(bars_by_symbol.get(sig.symbol, {}))
        if atr and sig.current_price > 0:
            sl_dist = round(_SL_ATR_MULTIPLE * atr, 5)
            tp_dist = round(_TP_ATR_MULTIPLE * atr, 5)
            if sig.direction == "buy":
                stop_loss = round(sig.current_price - sl_dist, 5)
                take_profit = round(sig.current_price + tp_dist, 5)
            else:
                stop_loss = round(sig.current_price + sl_dist, 5)
                take_profit = round(sig.current_price - tp_dist, 5)
            logger.info(
                "[signal] %s %s SL=%.5f TP=%.5f (ATR=%.5f)",
                sig.direction.upper(), sig.symbol, stop_loss, take_profit, atr,
            )

        if dry_run:
            result.orders_placed.append({
                "symbol": sig.symbol,
                "side": sig.direction,
                "quantity": qty,
                "confidence": sig.confidence,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "status": "dry_run",
                "reasoning": sig.reasoning,
            })
            logger.info(
                "[signal][DRY RUN] Would %s %.4f %s (conf=%.2f) SL=%s TP=%s",
                sig.direction.upper(), qty, sig.symbol, sig.confidence, stop_loss, take_profit,
            )
            continue

        try:
            broker_resp = place_order(
                symbol=sig.symbol,
                profile_id=config.profile_id,
                side=sig.direction,
                quantity=qty,
                order_type="market",
                stop_loss=stop_loss,
                take_profit=take_profit,
                session_id=session_id,
            )
            result.orders_placed.append(
                OrderResult(
                    symbol=sig.symbol,
                    side=sig.direction,
                    quantity=qty,
                    status="placed",
                    broker_response=broker_resp,
                ).to_dict()
            )
            logger.info(
                "[signal] %s %.4f %s (conf=%.2f) — OK",
                sig.direction.upper(), qty, sig.symbol, sig.confidence,
            )
        except Exception as exc:
            result.errors.append(
                OrderResult(
                    symbol=sig.symbol,
                    side=sig.direction,
                    quantity=qty,
                    status="error",
                    error=str(exc),
                ).to_dict()
            )
            logger.warning("[signal] Failed %s %s: %s", sig.direction, sig.symbol, exc)

    # Skip-record signals that were held back by the position cap.
    cap_skipped = [s for s in buys[available_slots:] if available_slots < len(buys)]
    for sig in cap_skipped:
        result.orders_skipped.append({
            "symbol": sig.symbol,
            "direction": sig.direction,
            "confidence": sig.confidence,
            "reason": "max_positions cap reached",
        })

    # Record holds / low-confidence skips.
    low_conf = [
        s for s in signals
        if s.direction in ("buy", "sell") and s.confidence < config.min_confidence
    ]
    for sig in low_conf:
        result.orders_skipped.append({
            "symbol": sig.symbol,
            "direction": sig.direction,
            "confidence": sig.confidence,
            "reason": f"confidence {sig.confidence:.2f} < min {config.min_confidence:.2f}",
        })

    # 6. Snapshot equity after.
    if not dry_run:
        try:
            acct_after = get_account(config.profile_id)
            result.equity_after, _ = _extract_equity(acct_after)
        except Exception as exc:
            logger.debug("[signal] Could not read post-cycle equity: %s", exc)

    append_signal_cycle_result(result)
    return result
