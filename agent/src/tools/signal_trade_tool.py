"""AI signal-based trading tools.

Five tools:
- setup_signal_trade        — configure a watchlist + AI signal strategy
- run_signal_cycle          — analyse market with AI, auto-place trades
- get_signal_status         — view configs and recent cycle history
- start_signal_scheduler    — start the background auto-trading scheduler
- stop_signal_scheduler     — stop the background scheduler
"""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.trading.service import get_account, profile_by_id


# ---------------------------------------------------------------------------
# SetupSignalTradeTool
# ---------------------------------------------------------------------------


class SetupSignalTradeTool(BaseTool):
    """Configure an AI-driven signal trading strategy on one account.

    Usage examples:
      • setup_signal_trade profile_id=mt5-demo-trade watchlist=EURUSD,GBPUSD,XAUUSD
      • setup_signal_trade profile_id=mt5-live-trade watchlist=US30,NAS100 timeframe=4h risk_per_trade_pct=0.5
    """

    name = "setup_signal_trade"
    description = (
        "Configure AI-driven signal trading on a single account. "
        "The AI analyses price history for each symbol in your watchlist and "
        "automatically places buy/sell orders when it detects a high-confidence "
        "signal. No master/leader account required. "
        "Parameters: profile_id (required), watchlist (comma-separated symbols, required), "
        "timeframe (1m/5m/15m/1h/4h/1d, default 1h), "
        "lookback_bars (20-200, default 50), "
        "min_confidence (0.0-1.0, default 0.65 — minimum AI confidence to trade), "
        "risk_per_trade_pct (0.1-5.0, default 1.0 — % of equity per trade), "
        "max_positions (1-20, default 5), "
        "interval_minutes (int, e.g. 30 — run automatically every N minutes; omit for manual only), "
        "label (optional name)."
    )

    def run(self, **kwargs: Any) -> str:
        from src.copy_trade.signal_models import SignalConfig
        from src.copy_trade.state import (
            get_signal_config,
            load_all_signal_configs,
            new_signal_config_id,
            save_signal_config,
        )

        profile_id = str(kwargs.get("profile_id", "")).strip()
        if not profile_id:
            return "Error: profile_id is required."

        watchlist_raw = str(kwargs.get("watchlist", "")).strip()
        if not watchlist_raw:
            return "Error: watchlist is required (comma-separated symbols, e.g. EURUSD,GBPUSD)."
        watchlist = [s.strip().upper() for s in watchlist_raw.split(",") if s.strip()]
        if not watchlist:
            return "Error: watchlist contains no valid symbols."

        # Validate profile exists.
        try:
            profile_by_id(profile_id)
        except Exception as exc:
            return f"Error: profile '{profile_id}' not found — {exc}"

        timeframe = str(kwargs.get("timeframe", "1h")).strip().lower()
        valid_timeframes = {"1m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "1d", "1w"}
        if timeframe not in valid_timeframes:
            return f"Error: timeframe must be one of {sorted(valid_timeframes)}."

        try:
            lookback_bars = int(kwargs.get("lookback_bars", 50))
            if not (20 <= lookback_bars <= 200):
                return "Error: lookback_bars must be between 20 and 200."
        except (TypeError, ValueError):
            return "Error: lookback_bars must be an integer."

        try:
            min_confidence = float(kwargs.get("min_confidence", 0.65))
            if not (0.0 <= min_confidence <= 1.0):
                return "Error: min_confidence must be between 0.0 and 1.0."
        except (TypeError, ValueError):
            return "Error: min_confidence must be a number."

        try:
            risk_per_trade_pct = float(kwargs.get("risk_per_trade_pct", 1.0))
            if not (0.1 <= risk_per_trade_pct <= 5.0):
                return "Error: risk_per_trade_pct must be between 0.1 and 5.0."
        except (TypeError, ValueError):
            return "Error: risk_per_trade_pct must be a number."

        try:
            max_positions = int(kwargs.get("max_positions", 5))
            if not (1 <= max_positions <= 20):
                return "Error: max_positions must be between 1 and 20."
        except (TypeError, ValueError):
            return "Error: max_positions must be an integer."

        interval_raw = kwargs.get("interval_minutes")
        interval_minutes: int | None = None
        if interval_raw is not None and str(interval_raw).strip():
            try:
                interval_minutes = int(interval_raw)
                if interval_minutes < 5:
                    return "Error: interval_minutes must be at least 5."
            except (TypeError, ValueError):
                return "Error: interval_minutes must be an integer."

        label = str(kwargs.get("label", "")).strip()

        # Reuse existing config if updating.
        config_id_hint = str(kwargs.get("config_id", "")).strip()
        existing = get_signal_config(config_id_hint) if config_id_hint else None
        config_id = existing.config_id if existing else new_signal_config_id()

        config = SignalConfig(
            config_id=config_id,
            profile_id=profile_id,
            watchlist=watchlist,
            timeframe=timeframe,
            lookback_bars=lookback_bars,
            min_confidence=min_confidence,
            risk_per_trade_pct=risk_per_trade_pct,
            max_positions=max_positions,
            interval_minutes=interval_minutes,
            enabled=True,
            label=label,
        )
        save_signal_config(config)

        total = len(load_all_signal_configs())
        action = "Updated" if existing else "Created"
        lines = [
            f"{action} AI signal trading config [{config_id}]",
            f"  Account    : {profile_id}",
            f"  Watchlist  : {', '.join(watchlist)}",
            f"  Timeframe  : {timeframe}  |  Lookback: {lookback_bars} bars",
            f"  AI trades when confidence ≥ {min_confidence:.0%}",
            f"  Risk       : {risk_per_trade_pct}% of equity per trade",
            f"  Max open   : {max_positions} positions",
        ]
        if interval_minutes:
            lines.append(f"  Auto-run   : every {interval_minutes} minutes")
        else:
            lines.append("  Auto-run   : manual only (use start_signal_scheduler to enable)")
        if label:
            lines.append(f"  Label      : {label}")
        lines.append(f"\n({total} signal config(s) total)")
        if interval_minutes:
            lines.append("\nStart the scheduler: start_signal_scheduler")
        lines.append(
            "Run manually: run_signal_cycle config_id=" + config_id
            + "\nPreview: run_signal_cycle config_id=" + config_id + " dry_run=true"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# RunSignalCycleTool
# ---------------------------------------------------------------------------


class RunSignalCycleTool(BaseTool):
    """Run one AI signal analysis cycle: analyse market, then auto-place trades.

    The AI fetches recent price bars for each symbol in your watchlist, analyses
    trend and momentum, then decides to buy, sell, or hold. Trades meeting the
    confidence threshold are placed automatically.

    Use dry_run=true to preview signals without placing any real orders.
    """

    name = "run_signal_cycle"
    description = (
        "Analyse the market with AI and auto-place trades on the configured account. "
        "For each symbol in the watchlist the AI reviews recent price history and "
        "returns a buy/sell/hold signal with a confidence score. "
        "Signals above the min_confidence threshold are executed as market orders. "
        "Parameters: config_id (required), dry_run (true/false, default false — "
        "set true to preview without placing real orders), session_id (optional)."
    )

    def run(self, **kwargs: Any) -> str:
        from src.copy_trade.signal_engine import run_signal_cycle
        from src.copy_trade.state import get_signal_config

        config_id = str(kwargs.get("config_id", "")).strip()
        if not config_id:
            return "Error: config_id is required. Use get_signal_status to list configs."

        config = get_signal_config(config_id)
        if config is None:
            return f"Error: signal config '{config_id}' not found."
        if not config.enabled:
            return f"Signal config '{config_id}' is disabled. Enable it with setup_signal_trade config_id={config_id} enabled=true."

        dry_raw = str(kwargs.get("dry_run", "false")).lower()
        dry_run = dry_raw in ("true", "1", "yes")
        session_id = str(kwargs.get("session_id", "")).strip()

        result = run_signal_cycle(config, session_id=session_id, dry_run=dry_run)

        lines: list[str] = []
        mode = " [DRY RUN — no orders placed]" if dry_run else ""
        label = f" ({config.label})" if config.label else ""
        lines.append(f"AI Signal Cycle{mode} — {config_id}{label}")
        lines.append(f"Account: {config.profile_id}  |  {result.ts}")
        lines.append("")

        # Signals section.
        if result.signals:
            lines.append(f"Signals ({len(result.signals)}):")
            for s in result.signals:
                direction = s.get("direction", "hold").upper()
                conf = s.get("confidence", 0.0)
                sym = s.get("symbol", "?")
                pct = s.get("position_size_pct", 0)
                icon = {"BUY": "▲", "SELL": "▼", "HOLD": "—"}.get(direction, "?")
                lines.append(
                    f"  {icon} {sym:<12} {direction:<4}  conf={conf:.0%}  size={pct:.0f}%"
                )
                if s.get("reasoning"):
                    lines.append(f"     └─ {s['reasoning'][:100]}")
        lines.append("")

        if result.orders_placed:
            placed_label = "Would trade" if dry_run else "Orders placed"
            lines.append(f"{placed_label} ({len(result.orders_placed)}):")
            for o in result.orders_placed:
                sym = o.get("symbol", "?")
                side = o.get("side", "?").upper()
                qty = o.get("quantity", 0)
                conf = o.get("confidence", o.get("broker_response", {}).get("confidence", ""))
                lines.append(f"  ✓ {side} {qty:.4f} {sym}" + (f"  (conf={conf:.0%})" if conf else ""))

        if result.orders_skipped:
            lines.append(f"\nSkipped ({len(result.orders_skipped)}):")
            for o in result.orders_skipped:
                sym = o.get("symbol", "?")
                reason = o.get("reason", "")
                conf = o.get("confidence", "")
                conf_str = f" conf={conf:.0%}" if conf else ""
                lines.append(f"  – {sym}{conf_str}: {reason}")

        if result.errors:
            lines.append(f"\nErrors ({len(result.errors)}):")
            for e in result.errors:
                sym = e.get("symbol", "?")
                err = e.get("error", str(e))
                lines.append(f"  ✗ {sym}: {err[:100]}")

        if result.equity_before is not None:
            lines.append(f"\nEquity: ${result.equity_before:,.2f}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# GetSignalStatusTool
# ---------------------------------------------------------------------------


class GetSignalStatusTool(BaseTool):
    """View all AI signal trading configs and recent cycle history."""

    name = "get_signal_status"
    description = (
        "List AI signal trading configurations and view recent cycle history. "
        "Parameters: config_id (optional — show detail for one config), "
        "cycles (int, default 3 — number of recent cycles to show)."
    )

    def run(self, **kwargs: Any) -> str:
        from src.copy_trade.state import (
            load_all_signal_configs,
            load_recent_signal_cycles,
        )

        configs = load_all_signal_configs()
        if not configs:
            return (
                "No AI signal trading configs found.\n"
                "Create one with: setup_signal_trade profile_id=<id> watchlist=EURUSD,GBPUSD"
            )

        config_id = str(kwargs.get("config_id", "")).strip()
        n_cycles = int(kwargs.get("cycles", 3))

        if config_id:
            cfg = configs.get(config_id)
            if cfg is None:
                return f"Config '{config_id}' not found."
            configs = {config_id: cfg}

        from src.copy_trade.signal_scheduler import scheduler_running

        sched_status = "running" if scheduler_running() else "stopped"
        lines: list[str] = [f"AI Signal Trading — {len(configs)} config(s)  |  Scheduler: {sched_status}", ""]
        for cid, cfg in configs.items():
            status = "✅ enabled" if cfg.enabled else "⏸ disabled"
            label = f" — {cfg.label}" if cfg.label else ""
            lines.append(f"{status}  [{cid}]{label}")
            lines.append(f"  Account   : {cfg.profile_id}")
            lines.append(f"  Watchlist : {', '.join(cfg.watchlist)}")
            lines.append(
                f"  Timeframe : {cfg.timeframe}  |  Lookback: {cfg.lookback_bars} bars  "
                f"|  Min confidence: {cfg.min_confidence:.0%}"
            )
            lines.append(
                f"  Risk      : {cfg.risk_per_trade_pct}% per trade  "
                f"|  Max positions: {cfg.max_positions}"
            )
            if cfg.interval_minutes:
                lines.append(f"  Auto-run  : every {cfg.interval_minutes} min")
            else:
                lines.append("  Auto-run  : manual only")

            cycles = load_recent_signal_cycles(cid, n=n_cycles)
            if cycles:
                lines.append(f"\n  Recent cycles ({len(cycles)}):")
                for cyc in cycles:
                    ts = str(cyc.get("ts", ""))[:16]
                    n_sig = len(cyc.get("signals", []))
                    n_placed = len(cyc.get("orders_placed", []))
                    n_skip = len(cyc.get("orders_skipped", []))
                    n_err = len(cyc.get("errors", []))
                    eq = cyc.get("equity_before")
                    eq_str = f"  equity=${eq:,.2f}" if eq is not None else ""
                    lines.append(
                        f"    {ts}  signals={n_sig}  placed={n_placed}  "
                        f"skipped={n_skip}  errors={n_err}{eq_str}"
                    )
            lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# StartSignalSchedulerTool
# ---------------------------------------------------------------------------


class StartSignalSchedulerTool(BaseTool):
    """Start the background AI signal scheduler.

    Once started, the scheduler runs in a background thread and fires a signal
    cycle for every enabled config whose ``interval_minutes`` has elapsed. It
    survives conversational turns and keeps trading automatically until stopped.

    To configure an auto-run interval use ``setup_signal_trade ... interval_minutes=30``.
    """

    name = "start_signal_scheduler"
    description = (
        "Start the background scheduler that automatically runs AI signal cycles "
        "on the configured interval (e.g. every 30 minutes). "
        "Only configs with interval_minutes set are affected. "
        "The scheduler runs in the background — you do not need to do anything "
        "else; it will keep trading until you call stop_signal_scheduler. "
        "No parameters required."
    )

    def run(self, **kwargs: Any) -> str:
        from src.copy_trade.signal_scheduler import scheduler_running, start_scheduler
        from src.copy_trade.state import load_all_signal_configs

        configs = load_all_signal_configs()
        scheduled = [c for c in configs.values() if c.enabled and c.interval_minutes]

        if not scheduled:
            return (
                "No configs have interval_minutes set.\n"
                "Add an interval first:\n"
                "  setup_signal_trade config_id=<id> interval_minutes=30\n"
                "Then start the scheduler again."
            )

        if scheduler_running():
            lines = ["Scheduler is already running."]
        else:
            start_scheduler()
            lines = ["✅ Background signal scheduler started."]

        lines.append("")
        lines.append(f"Auto-trading {len(scheduled)} config(s):")
        for cfg in scheduled:
            label = f" ({cfg.label})" if cfg.label else ""
            lines.append(
                f"  • [{cfg.config_id}]{label}  every {cfg.interval_minutes} min  "
                f"— {', '.join(cfg.watchlist)}"
            )
        lines.append("")
        lines.append("The AI will scan and trade automatically on its schedule.")
        lines.append("Stop anytime: stop_signal_scheduler")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# StopSignalSchedulerTool
# ---------------------------------------------------------------------------


class StopSignalSchedulerTool(BaseTool):
    """Stop the background AI signal scheduler."""

    name = "stop_signal_scheduler"
    description = (
        "Stop the background AI signal scheduler. "
        "Any cycle currently in progress will finish before the thread exits. "
        "Existing positions are NOT closed — only new automatic cycles stop. "
        "No parameters required."
    )

    def run(self, **kwargs: Any) -> str:
        from src.copy_trade.signal_scheduler import scheduler_running, stop_scheduler

        if not scheduler_running():
            return "Scheduler is not currently running."

        stop_scheduler()
        return (
            "⏹ Background signal scheduler stopped.\n"
            "Existing positions remain open — no orders were placed or cancelled.\n"
            "Restart anytime: start_signal_scheduler"
        )
