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

from datetime import datetime, timezone

from src.copy_trade.engine import _extract_equity, _extract_positions
from src.copy_trade.models import MIN_QTY_THRESHOLD, OrderResult
from src.copy_trade.signal_generator import generate_signal
from src.copy_trade.signal_models import Signal, SignalConfig, SignalCycleResult
from src.copy_trade.state import (
    append_signal_cycle_result,
    get_day_start_equity,
    get_prop_firm_rules,
    load_tracked_positions,
    remove_tracked_position,
    save_skipped_signal,
    save_tracked_position,
    save_trade_outcome,
    utc_now_iso,
)
from src.trading.service import close_position, get_account, get_historical_bars, get_positions, get_quote, place_order

logger = logging.getLogger(__name__)

_ATR_PERIOD = 14
_SL_ATR_MULTIPLE = 2.0   # stop loss = 2× ATR — wide enough to survive noise
_TP1_ATR_MULTIPLE = 1.5  # TP1 = 1.5× ATR — first target, closes 50% of position
_TP2_ATR_MULTIPLE = 3.0  # TP2 = 3× ATR — bonus target, closes remaining 50%


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
        equity, balance = _extract_equity(acct)
        # Fall back to balance when equity field is missing OR returned as zero.
        if (equity is None or equity == 0) and balance is not None and balance > 0:
            equity = balance
            logger.debug("[signal] Using balance as equity fallback: %.2f", equity)
        result.equity_before = equity
        logger.info("[signal] Account equity=%.2f balance=%.2f profile=%s",
                    equity or 0, balance or 0, config.profile_id)
    except Exception as exc:
        logger.warning("[signal] Could not read equity (profile=%s): %s", config.profile_id, exc)
        result.errors.append({"symbol": "*", "error": f"account fetch failed: {exc}"})

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

    # 3. Market hours guard — skip if forex is closed (weekend).
    now_dt = datetime.now(timezone.utc)
    from src.copy_trade.market_hours import is_market_open
    market_open, market_closed_reason = is_market_open(now_dt)
    if not market_open:
        logger.info("[signal] Market closed — skipping cycle: %s", market_closed_reason)
        result.errors.append({"symbol": "*", "error": market_closed_reason, "market_closed": True})
        append_signal_cycle_result(result)
        return result

    # 4. News filter — skip entire cycle if near a high-impact economic release.
    if getattr(config, "news_filter_enabled", True):
        from src.copy_trade.news_filter import is_news_blackout
        in_blackout, blackout_reason = is_news_blackout(
            now_dt, blackout_minutes=getattr(config, "news_blackout_minutes", 30)
        )
        if in_blackout:
            logger.info("[signal] NEWS BLACKOUT — skipping cycle: %s", blackout_reason)
            result.errors.append({
                "symbol": "*",
                "error": f"News blackout: {blackout_reason}",
                "news_blackout": True,
            })
            append_signal_cycle_result(result)
            return result

    # 4. Time-based exit — close positions older than max_trade_hours.
    max_hours = getattr(config, "max_trade_hours", 48)
    tracked = load_tracked_positions(config.config_id)
    for pid, pos_info in list(tracked.items()):
        try:
            entry_dt = datetime.fromisoformat(pos_info["entry_time"])
            age_hours = (now_dt - entry_dt).total_seconds() / 3600
        except Exception:
            continue
        if age_hours >= max_hours:
            try:
                if not dry_run:
                    close_position(
                        int(pos_info["position_id"]),
                        config.profile_id,
                        volume=int(pos_info["volume"]),
                    )
                    remove_tracked_position(config.config_id, pid)
                    # Record outcome — fetch current quote for approximate exit price.
                    try:
                        q = get_quote(pos_info["symbol"], config.profile_id)
                        exit_px = float(next(
                            (q.get(k) for k in ("last", "price", "close", "bid", "ask") if q.get(k)), 0
                        ) or 0)
                        ep = float(pos_info.get("entry_price", 0))
                        if ep > 0 and exit_px > 0:
                            raw_pnl = (exit_px - ep) / ep * 100
                            pnl = raw_pnl if pos_info.get("side") == "buy" else -raw_pnl
                            save_trade_outcome(
                                config.config_id, pos_info["symbol"], pos_info.get("side", ""),
                                ep, exit_px, pnl, age_hours, f"time_exit_{max_hours}h",
                            )
                    except Exception:
                        pass
                result.orders_placed.append({
                    "symbol": pos_info["symbol"],
                    "side": "close",
                    "reason": f"time_exit_{max_hours}h",
                    "position_id": pid,
                    "age_hours": round(age_hours, 1),
                    "status": "dry_run" if dry_run else "closed",
                })
                logger.info(
                    "[signal] Time exit: closed %s %s after %.1fh",
                    pos_info["side"].upper(), pos_info["symbol"], age_hours,
                )
            except Exception as exc:
                logger.warning("[signal] Time exit failed for position %s: %s", pid, exc)

    # Refresh tracked positions after time exits.
    tracked = load_tracked_positions(config.config_id)

    # 4. Read current positions (to avoid doubling into existing).
    current_positions: dict[str, float] = {}
    try:
        pos_raw = get_positions(config.profile_id)
        current_positions = _extract_positions(pos_raw)
    except Exception as exc:
        logger.debug("[signal] Could not read positions: %s", exc)

    # 4. Generate signals for each symbol — keep bars for ATR-based SL/TP later.
    import time as _time_fetch
    signals: list[Signal] = []
    bars_by_symbol: dict[str, dict] = {}
    for symbol in config.watchlist:
        _time_fetch.sleep(0.5)  # brief pause between symbols to avoid connection bursts
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

        # Pass tracked position so the AI can evaluate hold-vs-exit.
        open_pos = next(
            (p for p in tracked.values() if p.get("symbol") == symbol),
            None,
        )
        sig = generate_signal(symbol, bars_raw, quote_raw, config, open_position=open_pos)
        signals.append(sig)
        result.signals.append(sig.to_dict())

        # Surface silent fallback failures (empty bars, LLM crash, parse error) as errors
        # so they show up in cycle stats rather than looking like genuine HOLD signals.
        if sig.direction == "hold" and sig.confidence == 0.0 and sig.reasoning:
            _FALLBACK_PREFIXES = ("No historical data", "LLM error:", "Could not parse")
            if any(sig.reasoning.startswith(p) for p in _FALLBACK_PREFIXES):
                result.errors.append({"symbol": symbol, "error": sig.reasoning})

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
    all_buys = [s for s in actionable if s.direction == "buy"]
    buys = all_buys[:available_slots]  # respect position cap for new entries

    to_execute = sells + buys

    # 5. Signal reversal exit — close tracked positions that conflict with new signals.
    for sig in to_execute:
        for pid, pos_info in list(tracked.items()):
            if pos_info.get("symbol") != sig.symbol:
                continue
            tracked_side = pos_info.get("side", "")
            if (tracked_side == "buy" and sig.direction == "sell") or (
                tracked_side == "sell" and sig.direction == "buy"
            ):
                try:
                    if not dry_run:
                        close_position(
                            int(pos_info["position_id"]),
                            config.profile_id,
                            volume=int(pos_info["volume"]),
                        )
                        remove_tracked_position(config.config_id, pid)
                        # Record outcome using the current signal price as exit.
                        ep = float(pos_info.get("entry_price", 0))
                        if ep > 0 and sig.current_price > 0:
                            raw_pnl = (sig.current_price - ep) / ep * 100
                            pnl = raw_pnl if tracked_side == "buy" else -raw_pnl
                            try:
                                entry_dt2 = datetime.fromisoformat(pos_info.get("entry_time", ""))
                                dur_h = (now_dt - entry_dt2).total_seconds() / 3600
                            except Exception:
                                dur_h = 0.0
                            save_trade_outcome(
                                config.config_id, sig.symbol, tracked_side,
                                ep, sig.current_price, pnl, dur_h,
                                f"reversal_{sig.direction}",
                            )
                    result.orders_placed.append({
                        "symbol": sig.symbol,
                        "side": "close",
                        "reason": f"signal_reversal_{sig.direction}",
                        "position_id": pid,
                        "status": "dry_run" if dry_run else "closed",
                    })
                    logger.info(
                        "[signal] Reversal exit: closed %s %s — new signal is %s",
                        tracked_side.upper(), sig.symbol, sig.direction.upper(),
                    )
                except Exception as exc:
                    logger.warning("[signal] Reversal exit failed for position %s: %s", pid, exc)

    # Refresh tracked positions after reversal exits.
    if not dry_run:
        tracked = load_tracked_positions(config.config_id)

    # 6. Place orders — single order per signal with SL=2×ATR, TP=3×ATR.
    # One connection per order keeps us well below cTrader's rate limit
    # (the cycle already opens ~9 connections for bars/quotes/account).
    import time as _time

    for sig in to_execute:
        # Pause between orders to avoid hitting cTrader connection rate limits.
        _time.sleep(1.5)

        # Compute order quantity from equity risk.
        # Use sig.current_price; fall back to most-recent bar close if the
        # live quote wasn't available (spot subscription returning bid=0).
        effective_price = sig.current_price
        if effective_price <= 0:
            for bar in reversed(bars_by_symbol.get(sig.symbol, {}).get("bars", [])):
                try:
                    v = float(bar.get("close") or 0)
                    if v > 0:
                        effective_price = v
                        logger.info("[signal] %s price fallback from bar close: %.5f", sig.symbol, v)
                        break
                except (TypeError, ValueError):
                    pass

        qty = 0.0
        effective_equity = equity if (equity is not None and equity > 0) else 0.0
        if effective_price > 0 and effective_equity > 0:
            risk_notional = effective_equity * (config.risk_per_trade_pct / 100.0) * (sig.position_size_pct / 100.0)
            qty = risk_notional / effective_price
            qty = round(qty, 8)

        if qty < MIN_QTY_THRESHOLD:
            if effective_equity <= 0:
                why = f"equity={equity} (balance fallback failed)"
            elif effective_price <= 0:
                why = "price=0 (quote and bar close both unavailable)"
            else:
                why = (f"qty={qty:.8f} tiny — equity={effective_equity:.2f}, "
                       f"price={effective_price:.5f}, risk={config.risk_per_trade_pct}%, "
                       f"size={sig.position_size_pct:.0f}%")
            result.orders_skipped.append({
                "symbol": sig.symbol,
                "direction": sig.direction,
                "confidence": sig.confidence,
                "reason": why,
            })
            logger.warning(
                "[signal] %s %s skipped — %s",
                sig.direction.upper(), sig.symbol, why,
            )
            continue

        # Compute ATR-based SL and single TP (3×ATR) — one order per signal.
        atr = _compute_atr(bars_by_symbol.get(sig.symbol, {}))
        if atr and sig.current_price > 0:
            sl_dist = round(_SL_ATR_MULTIPLE * atr, 5)
            tp_dist = round(_TP2_ATR_MULTIPLE * atr, 5)
            if sig.direction == "buy":
                sl = round(sig.current_price - sl_dist, 5)
                tp = round(sig.current_price + tp_dist, 5)
            else:
                sl = round(sig.current_price + sl_dist, 5)
                tp = round(sig.current_price - tp_dist, 5)
            logger.info(
                "[signal] %s %s SL=%.5f TP=%.5f (ATR=%.5f)",
                sig.direction.upper(), sig.symbol, sl, tp, atr,
            )
        else:
            sl = None
            tp = None

        orders_to_place = [{"qty": qty, "stop_loss": sl, "take_profit": tp, "tp_label": "TP"}]

        for order_spec in orders_to_place:
            o_qty = order_spec["qty"]
            o_sl = order_spec["stop_loss"]
            o_tp = order_spec["take_profit"]
            tp_label = order_spec["tp_label"]

            if dry_run:
                result.orders_placed.append({
                    "symbol": sig.symbol,
                    "side": sig.direction,
                    "quantity": o_qty,
                    "confidence": sig.confidence,
                    "stop_loss": o_sl,
                    "take_profit": o_tp,
                    "tp_label": tp_label,
                    "status": "dry_run",
                    "reasoning": sig.reasoning,
                })
                logger.info(
                    "[signal][DRY RUN] Would %s %.4f %s (conf=%.2f) SL=%s TP=%s [%s]",
                    sig.direction.upper(), o_qty, sig.symbol, sig.confidence, o_sl, o_tp, tp_label,
                )
                continue

            try:
                broker_resp = place_order(
                    symbol=sig.symbol,
                    profile_id=config.profile_id,
                    side=sig.direction,
                    quantity=o_qty,
                    order_type="market",
                    stop_loss=o_sl,
                    take_profit=o_tp,
                    session_id=session_id,
                )
                pos_id = broker_resp.get("position_id")
                vol = broker_resp.get("volume", max(1000, int(round(o_qty))))
                result.orders_placed.append(
                    OrderResult(
                        symbol=sig.symbol,
                        side=sig.direction,
                        quantity=o_qty,
                        status="placed",
                        broker_response=broker_resp,
                    ).to_dict()
                )
                if pos_id:
                    save_tracked_position(
                        config_id=config.config_id,
                        position_id=str(pos_id),
                        symbol=sig.symbol,
                        side=sig.direction,
                        entry_time=ts,
                        entry_price=sig.current_price,
                        volume=int(vol),
                    )
                logger.info(
                    "[signal] %s %.4f %s (conf=%.2f) [%s] — OK pos_id=%s",
                    sig.direction.upper(), o_qty, sig.symbol, sig.confidence, tp_label, pos_id,
                )
            except Exception as exc:
                result.errors.append(
                    OrderResult(
                        symbol=sig.symbol,
                        side=sig.direction,
                        quantity=o_qty,
                        status="error",
                        error=str(exc),
                    ).to_dict()
                )
                logger.warning("[signal] Failed %s %s [%s]: %s", sig.direction, sig.symbol, tp_label, exc)

    # Skip-record signals that were held back by the position cap.
    cap_skipped = all_buys[available_slots:]
    for sig in cap_skipped:
        reason = "max_positions cap reached"
        result.orders_skipped.append({
            "symbol": sig.symbol,
            "direction": sig.direction,
            "confidence": sig.confidence,
            "reason": reason,
        })
        save_skipped_signal(config.config_id, sig.symbol, sig.direction, sig.confidence, reason)

    # Record holds / low-confidence skips.
    low_conf = [
        s for s in signals
        if s.direction in ("buy", "sell") and s.confidence < config.min_confidence
    ]
    for sig in low_conf:
        reason = f"confidence {sig.confidence:.2f} < min {config.min_confidence:.2f}"
        result.orders_skipped.append({
            "symbol": sig.symbol,
            "direction": sig.direction,
            "confidence": sig.confidence,
            "reason": reason,
        })
        save_skipped_signal(config.config_id, sig.symbol, sig.direction, sig.confidence, reason)

    # 6. Snapshot equity after.
    if not dry_run:
        try:
            acct_after = get_account(config.profile_id)
            result.equity_after, _ = _extract_equity(acct_after)
        except Exception as exc:
            logger.debug("[signal] Could not read post-cycle equity: %s", exc)

    append_signal_cycle_result(result)
    return result
