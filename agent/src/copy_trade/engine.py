"""Copy Trade engine: computes position deltas and places mirror orders.

Core flow per cycle:
1. Read leader positions via trading service.
2. Read follower positions via trading service.
3. Compute deltas (what follower must buy/sell to match leader * scale_ratio).
4. For each actionable delta, call place_order on the follower's profile.
5. Collect and return a CopyTradeCycleResult.

All order placement goes through the existing ``place_order`` in
``src.trading.service``, which routes live orders through the mandate gate,
order_guard, and audit ledger — the copy trade engine never bypasses those
safety layers.
"""

from __future__ import annotations

import logging
from typing import Any

from src.copy_trade.models import (
    MIN_QTY_THRESHOLD,
    CopyTradeConfig,
    CopyTradeCycleResult,
    OrderResult,
    PositionDelta,
)
from src.copy_trade.state import append_cycle_result, utc_now_iso
from src.trading.service import get_account, get_positions, place_order

logger = logging.getLogger(__name__)


def _extract_equity(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    """Parse connector ``get_account`` response into (equity, balance).

    Returns ``(None, None)`` when the account snapshot doesn't include equity
    (some brokers only expose balance).
    """
    equity = None
    balance = None
    for key in ("equity", "net_liquidation", "net_liq", "total_equity"):
        val = raw.get(key)
        if val is not None:
            try:
                equity = float(val)
                break
            except (TypeError, ValueError):
                pass
    for key in ("balance", "cash", "cash_balance", "buying_power"):
        val = raw.get(key)
        if val is not None:
            try:
                balance = float(val)
                break
            except (TypeError, ValueError):
                pass
    # Some connectors nest under "account" or "cash"
    for wrapper_key in ("account", "cash"):
        nested = raw.get(wrapper_key)
        if isinstance(nested, dict):
            if equity is None:
                for key in ("equity", "net_liquidation", "net_liq", "totalCash"):
                    val = nested.get(key)
                    if val is not None:
                        try:
                            equity = float(val)
                            break
                        except (TypeError, ValueError):
                            pass
            if balance is None:
                for key in ("balance", "cash", "availableFunds", "totalCash"):
                    val = nested.get(key)
                    if val is not None:
                        try:
                            balance = float(val)
                            break
                        except (TypeError, ValueError):
                            pass
    return equity, balance


def _extract_positions(raw: dict[str, Any]) -> dict[str, float]:
    """Parse connector ``get_positions`` response into {symbol: quantity}.

    Brokers return different shapes; we look for common keys (``positions``,
    ``holdings``) with ``symbol``/``ticker`` and ``quantity``/``qty``/``shares``.
    """
    positions: dict[str, float] = {}
    items: list[Any] = []

    for key in ("positions", "holdings", "data"):
        if key in raw and isinstance(raw[key], list):
            items = raw[key]
            break

    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = (
            item.get("symbol") or item.get("ticker") or item.get("code") or ""
        ).strip().upper()
        if not symbol:
            continue
        qty = None
        for qty_key in ("quantity", "qty", "shares", "position", "size"):
            if qty_key in item:
                try:
                    qty = float(item[qty_key])
                except (TypeError, ValueError):
                    pass
                break
        if qty is not None and qty != 0.0:
            positions[symbol] = qty

    return positions


def compute_deltas(
    leader_positions: dict[str, float],
    follower_positions: dict[str, float],
    scale_ratio: float,
) -> list[PositionDelta]:
    """Return ordered list of trades the follower must execute to mirror leader.

    Positions that the leader holds but the follower doesn't → buy.
    Positions that differ in quantity → buy/sell the difference.
    Positions the leader no longer holds but the follower still does → sell all.
    """
    all_symbols = set(leader_positions) | set(follower_positions)
    deltas: list[PositionDelta] = []

    for symbol in sorted(all_symbols):
        leader_qty = leader_positions.get(symbol, 0.0)
        target_qty = round(leader_qty * scale_ratio, 8)
        current_qty = follower_positions.get(symbol, 0.0)
        diff = target_qty - current_qty

        if abs(diff) < MIN_QTY_THRESHOLD:
            continue

        side = "buy" if diff > 0 else "sell"
        deltas.append(
            PositionDelta(
                symbol=symbol,
                side=side,
                quantity=abs(diff),
                target_qty=target_qty,
                current_qty=current_qty,
                reason=(
                    f"leader={leader_qty} scale={scale_ratio} "
                    f"target={target_qty} current={current_qty}"
                ),
            )
        )

    return deltas


def run_copy_trade_cycle(config: CopyTradeConfig, session_id: str = "") -> CopyTradeCycleResult:
    """Execute one copy-trade synchronisation cycle.

    Reads leader + follower positions, computes deltas, and places market
    orders on the follower account. All orders go through the existing
    ``place_order`` safety stack (mandate gate + audit).

    Args:
        config: Copy trade configuration.
        session_id: Originating session id, forwarded to ``place_order``.

    Returns:
        CopyTradeCycleResult summarising placed/skipped/errored orders.
    """
    ts = utc_now_iso()
    result = CopyTradeCycleResult(config_id=config.config_id, ts=ts)

    # 0. Snapshot follower equity before any orders.
    try:
        acct_before = get_account(config.follower_profile_id)
        result.equity_before, result.balance_before = _extract_equity(acct_before)
    except Exception as exc:
        logger.debug("[copy_trade] Could not read follower equity (before): %s", exc)

    # 1. Read leader positions.
    try:
        leader_raw = get_positions(config.leader_profile_id)
    except Exception as exc:
        result.errors.append(
            OrderResult(
                symbol="*",
                side="",
                quantity=0,
                status="error",
                error=f"Failed to read leader positions: {exc}",
            ).to_dict()
        )
        append_cycle_result(result)
        return result

    leader_positions = _extract_positions(leader_raw)
    result.leader_positions = [{"symbol": s, "quantity": q} for s, q in leader_positions.items()]

    # 2. Read follower positions.
    try:
        follower_raw = get_positions(config.follower_profile_id)
    except Exception as exc:
        result.errors.append(
            OrderResult(
                symbol="*",
                side="",
                quantity=0,
                status="error",
                error=f"Failed to read follower positions: {exc}",
            ).to_dict()
        )
        append_cycle_result(result)
        return result

    follower_positions = _extract_positions(follower_raw)
    result.follower_positions_before = [
        {"symbol": s, "quantity": q} for s, q in follower_positions.items()
    ]

    # 3. Compute deltas.
    deltas = compute_deltas(leader_positions, follower_positions, config.scale_ratio)

    if not deltas:
        logger.info("[copy_trade] config=%s — positions already in sync, no orders needed", config.config_id)
        append_cycle_result(result)
        return result

    # 4. Place orders.
    for delta in deltas:
        qty = delta.quantity

        # Apply optional per-order notional cap (requires a quote; skip cap if quote fails).
        if config.max_order_notional is not None:
            try:
                from src.trading.service import get_quote

                quote = get_quote(delta.symbol, config.follower_profile_id)
                price = float(
                    quote.get("last")
                    or quote.get("price")
                    or quote.get("close")
                    or 0
                )
                if price > 0:
                    max_qty = config.max_order_notional / price
                    if qty > max_qty:
                        qty = round(max_qty, 8)
                        logger.info(
                            "[copy_trade] %s qty capped at %.4f (notional cap $%.2f @ $%.4f)",
                            delta.symbol,
                            qty,
                            config.max_order_notional,
                            price,
                        )
            except Exception as exc:
                logger.warning("[copy_trade] Could not fetch quote for %s to apply cap: %s", delta.symbol, exc)

        if qty < MIN_QTY_THRESHOLD:
            result.orders_skipped.append(
                OrderResult(
                    symbol=delta.symbol,
                    side=delta.side,
                    quantity=qty,
                    status="skipped",
                    skip_reason="quantity below minimum threshold after notional cap",
                ).to_dict()
            )
            continue

        try:
            broker_resp = place_order(
                symbol=delta.symbol,
                profile_id=config.follower_profile_id,
                side=delta.side,
                quantity=qty,
                order_type="market",
                session_id=session_id,
            )
            result.orders_placed.append(
                OrderResult(
                    symbol=delta.symbol,
                    side=delta.side,
                    quantity=qty,
                    status="placed",
                    broker_response=broker_resp,
                ).to_dict()
            )
            logger.info(
                "[copy_trade] %s %s %.4f on %s — OK",
                delta.side.upper(),
                delta.symbol,
                qty,
                config.follower_profile_id,
            )
        except Exception as exc:
            error_msg = str(exc)
            result.errors.append(
                OrderResult(
                    symbol=delta.symbol,
                    side=delta.side,
                    quantity=qty,
                    status="error",
                    error=error_msg,
                ).to_dict()
            )
            logger.warning(
                "[copy_trade] Failed to place %s %s on %s: %s",
                delta.side,
                delta.symbol,
                config.follower_profile_id,
                error_msg,
            )

    # 5. Snapshot follower equity after orders.
    try:
        acct_after = get_account(config.follower_profile_id)
        result.equity_after, _ = _extract_equity(acct_after)
    except Exception as exc:
        logger.debug("[copy_trade] Could not read follower equity (after): %s", exc)

    append_cycle_result(result)
    return result
