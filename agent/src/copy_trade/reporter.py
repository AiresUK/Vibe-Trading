"""Daily P&L reporter for the Copy Trade engine.

Aggregates per-cycle equity snapshots into daily P&L rows and running totals.
All values are in the follower account's base currency (typically USD).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from src.copy_trade.models import DailyPnL
from src.copy_trade.state import load_recent_cycles


def build_daily_pnl_report(
    config_id: str,
    days: int = 7,
) -> dict[str, Any]:
    """Build a daily P&L summary from stored cycle history.

    Args:
        config_id: Copy-trade config to report on.
        days: How many calendar days back to include (default 7).

    Returns:
        Dict with ``daily`` list (newest first), ``summary`` totals, and
        ``equity_curve`` list of ``{ts, equity}`` points for charting.
    """
    # Load enough cycles to cover ``days`` of history.
    cycles = load_recent_cycles(config_id, n=min(days * 48, 500))

    # ---- Build per-day buckets ------------------------------------------
    buckets: dict[str, DailyPnL] = {}
    equity_curve: list[dict[str, Any]] = []

    for cycle in reversed(cycles):  # oldest first for equity_open logic
        date = str(cycle.get("ts", ""))[:10]
        if not date:
            continue

        if date not in buckets:
            buckets[date] = DailyPnL(date=date)

        day = buckets[date]
        day.cycles += 1
        day.orders_placed += len(cycle.get("orders_placed", []))
        day.orders_skipped += len(cycle.get("orders_skipped", []))
        day.errors += len(cycle.get("errors", []))

        eq_before = cycle.get("equity_before")
        eq_after = cycle.get("equity_after")

        # First reading of the day sets equity_open.
        if eq_before is not None and day.equity_open is None:
            try:
                day.equity_open = float(eq_before)
            except (TypeError, ValueError):
                pass

        # Latest reading of the day updates equity_close.
        for val in (eq_after, eq_before):
            if val is not None:
                try:
                    day.equity_close = float(val)
                    break
                except (TypeError, ValueError):
                    pass

        # Equity curve point.
        ts = cycle.get("ts", "")
        for val in (eq_after, eq_before):
            if val is not None:
                try:
                    equity_curve.append({"ts": ts, "equity": float(val)})
                    break
                except (TypeError, ValueError):
                    pass

    # ---- Compute per-day P&L -------------------------------------------
    for day in buckets.values():
        if day.equity_open is not None and day.equity_close is not None:
            day.pnl = round(day.equity_close - day.equity_open, 4)
            if day.equity_open != 0:
                day.pnl_pct = round((day.pnl / day.equity_open) * 100, 4)

    # ---- Summary totals ------------------------------------------------
    all_days = sorted(buckets.values(), key=lambda d: d.date, reverse=True)

    # Limit to requested window.
    recent_days = all_days[:days]

    total_pnl = sum(d.pnl for d in recent_days if d.pnl is not None)
    winning_days = sum(1 for d in recent_days if d.pnl is not None and d.pnl > 0)
    losing_days = sum(1 for d in recent_days if d.pnl is not None and d.pnl < 0)
    flat_days = sum(1 for d in recent_days if d.pnl is not None and d.pnl == 0)
    days_with_data = sum(1 for d in recent_days if d.pnl is not None)

    best_day = max((d for d in recent_days if d.pnl is not None), key=lambda d: d.pnl, default=None)
    worst_day = min((d for d in recent_days if d.pnl is not None), key=lambda d: d.pnl, default=None)

    avg_daily_pnl = round(total_pnl / days_with_data, 4) if days_with_data else None

    # Starting equity = equity_open of the oldest day with data.
    oldest_with_data = next((d for d in reversed(recent_days) if d.equity_open is not None), None)
    starting_equity = oldest_with_data.equity_open if oldest_with_data else None
    latest_equity = next((d.equity_close for d in recent_days if d.equity_close is not None), None)
    total_pnl_pct = (
        round((total_pnl / starting_equity) * 100, 4)
        if starting_equity and starting_equity != 0
        else None
    )

    summary = {
        "period_days": days,
        "days_with_data": days_with_data,
        "total_pnl": round(total_pnl, 4),
        "total_pnl_pct": total_pnl_pct,
        "avg_daily_pnl": avg_daily_pnl,
        "winning_days": winning_days,
        "losing_days": losing_days,
        "flat_days": flat_days,
        "win_rate_pct": round(winning_days / days_with_data * 100, 1) if days_with_data else None,
        "best_day": {"date": best_day.date, "pnl": best_day.pnl, "pnl_pct": best_day.pnl_pct} if best_day else None,
        "worst_day": {"date": worst_day.date, "pnl": worst_day.pnl, "pnl_pct": worst_day.pnl_pct} if worst_day else None,
        "starting_equity": starting_equity,
        "latest_equity": latest_equity,
    }

    return {
        "config_id": config_id,
        "daily": [d.to_dict() for d in recent_days],
        "summary": summary,
        "equity_curve": equity_curve[-200:],  # cap chart data points
    }


def format_pnl_report_text(report: dict[str, Any]) -> str:
    """Return a compact, human-readable P&L summary for chat display."""
    summary = report.get("summary", {})
    daily = report.get("daily", [])

    lines = [
        f"Copy Trade P&L — last {summary.get('period_days', '?')} days",
        "─" * 42,
    ]

    total_pnl = summary.get("total_pnl")
    total_pnl_pct = summary.get("total_pnl_pct")
    avg = summary.get("avg_daily_pnl")
    wr = summary.get("win_rate_pct")

    if total_pnl is not None:
        sign = "+" if total_pnl >= 0 else ""
        pct_str = f" ({sign}{total_pnl_pct:.2f}%)" if total_pnl_pct is not None else ""
        lines.append(f"Total P&L:      {sign}${total_pnl:,.2f}{pct_str}")
    if avg is not None:
        sign = "+" if avg >= 0 else ""
        lines.append(f"Avg daily P&L:  {sign}${avg:,.2f}")
    if wr is not None:
        w = summary.get("winning_days", 0)
        l = summary.get("losing_days", 0)
        lines.append(f"Win rate:       {wr:.1f}%  ({w}W / {l}L)")

    best = summary.get("best_day")
    worst = summary.get("worst_day")
    if best:
        lines.append(f"Best day:       +${best['pnl']:,.2f}  ({best['date']})")
    if worst and worst["pnl"] is not None and worst["pnl"] < 0:
        lines.append(f"Worst day:       -${abs(worst['pnl']):,.2f}  ({worst['date']})")

    eq = summary.get("latest_equity")
    if eq is not None:
        lines.append(f"Current equity: ${eq:,.2f}")

    if daily:
        lines.append("")
        lines.append("Daily breakdown:")
        lines.append(f"{'Date':<12} {'P&L':>10} {'%':>7}  {'Orders':>6}")
        lines.append("─" * 42)
        for d in daily:
            pnl = d.get("pnl")
            pnl_pct = d.get("pnl_pct")
            orders = d.get("orders_placed", 0)
            if pnl is not None:
                sign = "+" if pnl >= 0 else ""
                pct = f"{sign}{pnl_pct:.2f}%" if pnl_pct is not None else "  n/a"
                lines.append(f"{d['date']:<12} {sign}${pnl:>8,.2f} {pct:>7}  {orders:>6}")
            else:
                lines.append(f"{d['date']:<12} {'n/a':>10} {'':>7}  {orders:>6}")

    return "\n".join(lines)
