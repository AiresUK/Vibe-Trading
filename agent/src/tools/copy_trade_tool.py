"""Copy Trade tools — auto-discovered by the tool registry.

Seven tools:
    setup_copy_trade        — create / update a copy-trade config
    run_copy_trade_cycle    — execute one sync cycle (read leader, mirror to follower)
    get_copy_trade_status   — list configs + recent cycle history
    get_copy_trade_pnl      — daily P&L report with equity curve
    stop_copy_trade         — disable or delete a copy-trade config
    setup_prop_firm_rules   — attach prop firm drawdown/target rules to a config
    get_prop_firm_status    — live prop firm rule check against current equity
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)


def _ok(**payload: Any) -> str:
    return json.dumps({"status": "ok", **payload}, ensure_ascii=False, default=str)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "error": message, **extra}, ensure_ascii=False)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Tool 1: setup_copy_trade
# ---------------------------------------------------------------------------


class SetupCopyTradeTool(BaseTool):
    """Create or update a copy-trade configuration."""

    name = "setup_copy_trade"
    description = (
        "Configure a copy-trade pair: a leader account whose positions are mirrored "
        "onto a follower account. Persists the config to ~/.vibe-trading/copy_trade/. "
        "Returns a config_id you can pass to run_copy_trade_cycle. "
        "Use run_copy_trade_cycle to execute a sync immediately, or let the scheduler "
        "call it on a cadence.\n\n"
        "scale_ratio controls position sizing: follower qty = leader qty * scale_ratio. "
        "For example, scale_ratio=0.5 means the follower holds half the leader's qty. "
        "max_order_notional caps the USD value of any single order."
    )
    parameters = {
        "type": "object",
        "properties": {
            "leader_profile_id": {
                "type": "string",
                "description": "Connector profile id of the account to copy FROM (e.g. 'alpaca-live').",
            },
            "follower_profile_id": {
                "type": "string",
                "description": "Connector profile id of the account to copy INTO (e.g. 'alpaca-paper').",
            },
            "scale_ratio": {
                "type": "number",
                "description": "Position size multiplier. follower qty = leader qty * scale_ratio. Default 1.0.",
                "default": 1.0,
            },
            "max_order_notional": {
                "type": "number",
                "description": "Optional maximum USD value per single order. Omit for no cap.",
            },
            "label": {
                "type": "string",
                "description": "Optional human-readable label for this copy-trade pair.",
            },
            "config_id": {
                "type": "string",
                "description": "Existing config id to update. Omit to create a new config.",
            },
        },
        "required": ["leader_profile_id", "follower_profile_id"],
    }
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        try:
            from src.copy_trade import (
                CopyTradeConfig,
                get_config,
                new_config_id,
                save_config,
            )
            from src.trading.profiles import profile_by_id

            leader_id = str(kwargs["leader_profile_id"]).strip()
            follower_id = str(kwargs["follower_profile_id"]).strip()

            # Validate both profiles exist.
            try:
                profile_by_id(leader_id)
            except Exception:
                return _err(f"Leader profile '{leader_id}' not found. Run list_trading_connectors to see available profiles.")
            try:
                profile_by_id(follower_id)
            except Exception:
                return _err(f"Follower profile '{follower_id}' not found. Run list_trading_connectors to see available profiles.")

            if leader_id == follower_id:
                return _err("leader_profile_id and follower_profile_id must be different accounts.")

            scale_ratio = _float_or_none(kwargs.get("scale_ratio")) or 1.0
            if not (0 < scale_ratio <= 10):
                return _err("scale_ratio must be between 0 (exclusive) and 10 (inclusive).")

            raw_config_id = kwargs.get("config_id", "")
            config_id = str(raw_config_id).strip() if raw_config_id else ""

            if config_id:
                existing = get_config(config_id)
                if existing is None:
                    return _err(f"No config found with id '{config_id}'. Omit config_id to create a new one.")
            else:
                config_id = new_config_id()

            config = CopyTradeConfig(
                config_id=config_id,
                leader_profile_id=leader_id,
                follower_profile_id=follower_id,
                scale_ratio=scale_ratio,
                max_order_notional=_float_or_none(kwargs.get("max_order_notional")),
                enabled=True,
                label=str(kwargs.get("label", "")).strip(),
            )
            save_config(config)

            return _ok(
                config_id=config_id,
                leader=leader_id,
                follower=follower_id,
                scale_ratio=scale_ratio,
                max_order_notional=config.max_order_notional,
                label=config.label,
                message=(
                    f"Copy-trade config '{config_id}' saved. "
                    "Call run_copy_trade_cycle with this config_id to sync positions now."
                ),
            )
        except Exception as exc:
            logger.exception("setup_copy_trade failed")
            return _err(str(exc))


# ---------------------------------------------------------------------------
# Tool 2: run_copy_trade_cycle
# ---------------------------------------------------------------------------


class RunCopyTradeCycleTool(BaseTool):
    """Execute one copy-trade synchronisation cycle."""

    name = "run_copy_trade_cycle"
    description = (
        "Run one copy-trade sync cycle: reads the leader's current positions, "
        "computes what the follower needs to buy/sell to match them (scaled by "
        "the configured scale_ratio), and places market orders on the follower. "
        "Orders go through the existing mandate gate and order_guard safety checks. "
        "Returns a summary of placed / skipped / errored orders. "
        "Call this on a schedule (e.g. every 5 minutes during market hours) to "
        "keep the follower account in continuous sync with the leader."
    )
    parameters = {
        "type": "object",
        "properties": {
            "config_id": {
                "type": "string",
                "description": "Copy-trade config id returned by setup_copy_trade.",
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "When true, compute deltas and return what WOULD be traded "
                    "without placing any real orders. Useful for previewing sync."
                ),
                "default": False,
            },
        },
        "required": ["config_id"],
    }
    is_readonly = False
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        try:
            from src.copy_trade import (
                compute_deltas,
                get_config,
                run_copy_trade_cycle,
            )
            from src.copy_trade.engine import _extract_positions
            from src.trading.service import get_positions

            config_id = str(kwargs["config_id"]).strip()
            dry_run = bool(kwargs.get("dry_run", False))

            config = get_config(config_id)
            if config is None:
                return _err(f"No copy-trade config found with id '{config_id}'.")
            if not config.enabled:
                return _err(f"Config '{config_id}' is disabled. Use setup_copy_trade to re-enable it.")

            if dry_run:
                leader_raw = get_positions(config.leader_profile_id)
                follower_raw = get_positions(config.follower_profile_id)
                leader_pos = _extract_positions(leader_raw)
                follower_pos = _extract_positions(follower_raw)
                deltas = compute_deltas(leader_pos, follower_pos, config.scale_ratio)
                return _ok(
                    dry_run=True,
                    config_id=config_id,
                    leader_positions=[{"symbol": s, "qty": q} for s, q in leader_pos.items()],
                    follower_positions=[{"symbol": s, "qty": q} for s, q in follower_pos.items()],
                    pending_orders=[d.to_dict() for d in deltas],
                    message=(
                        f"{len(deltas)} order(s) would be placed to sync positions."
                        if deltas
                        else "Positions already in sync — no orders needed."
                    ),
                )

            result = run_copy_trade_cycle(config)
            placed = len(result.orders_placed)
            skipped = len(result.orders_skipped)
            errors = len(result.errors)

            return _ok(
                config_id=config_id,
                ts=result.ts,
                placed=placed,
                skipped=skipped,
                errors=errors,
                orders_placed=result.orders_placed,
                orders_skipped=result.orders_skipped,
                order_errors=result.errors,
                leader_positions=result.leader_positions,
                follower_positions_before=result.follower_positions_before,
                message=(
                    f"Cycle complete: {placed} placed, {skipped} skipped, {errors} errors."
                ),
            )
        except Exception as exc:
            logger.exception("run_copy_trade_cycle failed")
            return _err(str(exc))


# ---------------------------------------------------------------------------
# Tool 3: get_copy_trade_status
# ---------------------------------------------------------------------------


class GetCopyTradeStatusTool(BaseTool):
    """List copy-trade configs and recent cycle history."""

    name = "get_copy_trade_status"
    description = (
        "List all copy-trade configurations and, optionally, recent sync cycle "
        "results for a specific config. Use this to check whether copy trading "
        "is active, review recent order history, and diagnose sync issues."
    )
    parameters = {
        "type": "object",
        "properties": {
            "config_id": {
                "type": "string",
                "description": "Optional config id to fetch recent cycle history for.",
            },
            "history_n": {
                "type": "integer",
                "description": "Number of recent cycles to return when config_id is provided (default 10).",
                "default": 10,
            },
        },
        "required": [],
    }
    is_readonly = True
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        try:
            from src.copy_trade import load_all_configs, load_recent_cycles

            all_configs = load_all_configs()
            configs_summary = [
                {
                    "config_id": c.config_id,
                    "label": c.label,
                    "leader": c.leader_profile_id,
                    "follower": c.follower_profile_id,
                    "scale_ratio": c.scale_ratio,
                    "max_order_notional": c.max_order_notional,
                    "enabled": c.enabled,
                }
                for c in all_configs.values()
            ]

            config_id = str(kwargs.get("config_id", "")).strip() or None
            history: list[Any] = []
            if config_id:
                if config_id not in all_configs:
                    return _err(f"No config found with id '{config_id}'.")
                n = int(kwargs.get("history_n", 10))
                history = load_recent_cycles(config_id, n)

            return _ok(
                configs=configs_summary,
                total_configs=len(configs_summary),
                recent_cycles=history,
                message=(
                    f"{len(configs_summary)} copy-trade config(s) found."
                    if configs_summary
                    else "No copy-trade configs set up yet. Call setup_copy_trade to create one."
                ),
            )
        except Exception as exc:
            logger.exception("get_copy_trade_status failed")
            return _err(str(exc))


# ---------------------------------------------------------------------------
# Tool 4: stop_copy_trade
# ---------------------------------------------------------------------------


class StopCopyTradeTool(BaseTool):
    """Disable or permanently delete a copy-trade configuration."""

    name = "stop_copy_trade"
    description = (
        "Disable a copy-trade config (keeps history, stops future cycles) or "
        "permanently delete it. Disabling is reversible; deletion is not."
    )
    parameters = {
        "type": "object",
        "properties": {
            "config_id": {
                "type": "string",
                "description": "Copy-trade config id to stop.",
            },
            "permanent": {
                "type": "boolean",
                "description": (
                    "When true, permanently delete the config and its cycle history. "
                    "When false (default), just disable it (reversible)."
                ),
                "default": False,
            },
        },
        "required": ["config_id"],
    }
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        try:
            from src.copy_trade import delete_config, get_config, save_config

            config_id = str(kwargs["config_id"]).strip()
            permanent = bool(kwargs.get("permanent", False))

            config = get_config(config_id)
            if config is None:
                return _err(f"No copy-trade config found with id '{config_id}'.")

            if permanent:
                delete_config(config_id)
                return _ok(
                    config_id=config_id,
                    action="deleted",
                    message=f"Config '{config_id}' permanently deleted.",
                )
            else:
                from dataclasses import replace

                disabled = replace(config, enabled=False)
                save_config(disabled)
                return _ok(
                    config_id=config_id,
                    action="disabled",
                    message=(
                        f"Config '{config_id}' disabled. No further cycles will run. "
                        "Call setup_copy_trade with the same config_id to re-enable."
                    ),
                )
        except Exception as exc:
            logger.exception("stop_copy_trade failed")
            return _err(str(exc))


# ---------------------------------------------------------------------------
# Tool 5: get_copy_trade_pnl
# ---------------------------------------------------------------------------


class GetCopyTradePnLTool(BaseTool):
    """Daily P&L report for a copy-trade config."""

    name = "get_copy_trade_pnl"
    description = (
        "Return a daily P&L breakdown for a copy-trade config: equity open/close, "
        "daily P&L in USD and %, win rate, best/worst day, and an equity curve for "
        "charting. Equity snapshots are captured automatically at the start and end "
        "of every run_copy_trade_cycle call. Call this to review performance."
    )
    parameters = {
        "type": "object",
        "properties": {
            "config_id": {
                "type": "string",
                "description": "Copy-trade config id to report on.",
            },
            "days": {
                "type": "integer",
                "description": "Number of calendar days to include in the report (default 7, max 90).",
                "default": 7,
            },
        },
        "required": ["config_id"],
    }
    is_readonly = True
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        try:
            from src.copy_trade import get_config
            from src.copy_trade.reporter import build_daily_pnl_report, format_pnl_report_text

            config_id = str(kwargs["config_id"]).strip()
            days = max(1, min(int(kwargs.get("days", 7)), 90))

            if get_config(config_id) is None:
                return _err(f"No copy-trade config found with id '{config_id}'.")

            report = build_daily_pnl_report(config_id, days=days)
            summary = report["summary"]

            if summary["days_with_data"] == 0:
                return _ok(
                    config_id=config_id,
                    report=report,
                    text=(
                        "No equity data yet. Equity snapshots are captured each time "
                        "run_copy_trade_cycle runs — call that first to start tracking P&L."
                    ),
                )

            text = format_pnl_report_text(report)
            return _ok(
                config_id=config_id,
                report=report,
                text=text,
            )
        except Exception as exc:
            logger.exception("get_copy_trade_pnl failed")
            return _err(str(exc))


# ---------------------------------------------------------------------------
# Tool 6: setup_prop_firm_rules
# ---------------------------------------------------------------------------


class SetupPropFirmRulesTool(BaseTool):
    """Attach prop firm trading rules to a copy-trade config."""

    name = "setup_prop_firm_rules"
    description = (
        "Configure prop firm drawdown and profit-target rules for a copy-trade config. "
        "Once set, every run_copy_trade_cycle call checks these rules before placing orders "
        "and halts automatically if a limit is approached, protecting the funded account. "
        "Use a preset (ftmo, apex, mff, tft, e8) or enter custom limits manually."
    )
    parameters = {
        "type": "object",
        "properties": {
            "config_id": {
                "type": "string",
                "description": "Copy-trade config id to attach these rules to.",
            },
            "initial_balance": {
                "type": "number",
                "description": "Funded account starting balance (e.g. 50000 for a $50k account).",
            },
            "preset": {
                "type": "string",
                "description": (
                    "Named prop firm preset: 'ftmo', 'apex', 'mff' (MyForexFunds), "
                    "'tft' (The Funded Trader), 'e8' (E8 Funding). "
                    "Overrides daily/total drawdown and profit target with firm-specific values."
                ),
            },
            "firm_name": {
                "type": "string",
                "description": "Human-readable firm name (e.g. 'FTMO'). Auto-set when using a preset.",
            },
            "daily_drawdown_limit_pct": {
                "type": "number",
                "description": "Max allowed daily equity drop as % of day-start equity (e.g. 5.0 for 5%).",
            },
            "total_drawdown_limit_pct": {
                "type": "number",
                "description": "Max allowed total equity drop from initial_balance (e.g. 10.0 for 10%).",
            },
            "profit_target_pct": {
                "type": "number",
                "description": "Challenge profit target as % of initial_balance (e.g. 10.0 for 10%).",
            },
            "alert_pct": {
                "type": "number",
                "description": "Alert when this % of a limit is consumed (default 80).",
                "default": 80.0,
            },
            "halt_pct": {
                "type": "number",
                "description": "Halt orders when this % of a limit is consumed (default 95).",
                "default": 95.0,
            },
        },
        "required": ["config_id", "initial_balance"],
    }
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        try:
            from src.copy_trade import get_config
            from src.copy_trade.prop_firm import PROP_FIRM_PRESETS, PropFirmRules
            from src.copy_trade.state import save_prop_firm_rules

            config_id = str(kwargs["config_id"]).strip()
            if get_config(config_id) is None:
                return _err(f"No copy-trade config found with id '{config_id}'.")

            initial_balance = float(kwargs["initial_balance"])
            if initial_balance <= 0:
                return _err("initial_balance must be positive.")

            preset = str(kwargs.get("preset", "")).strip().lower()

            if preset:
                rules = PropFirmRules.from_preset(config_id, preset, initial_balance)
                if not rules.firm_name:
                    return _err(
                        f"Unknown preset '{preset}'. "
                        f"Available: {', '.join(PROP_FIRM_PRESETS.keys())}"
                    )
            else:
                rules = PropFirmRules(
                    config_id=config_id,
                    firm_name=str(kwargs.get("firm_name", "")).strip(),
                    initial_balance=initial_balance,
                    daily_drawdown_limit_pct=float(kwargs.get("daily_drawdown_limit_pct", 5.0)),
                    total_drawdown_limit_pct=float(kwargs.get("total_drawdown_limit_pct", 10.0)),
                    profit_target_pct=float(kwargs.get("profit_target_pct", 10.0)),
                    alert_pct=float(kwargs.get("alert_pct", 80.0)),
                    halt_pct=float(kwargs.get("halt_pct", 95.0)),
                )

            save_prop_firm_rules(rules)

            daily_buffer = round(initial_balance * rules.daily_drawdown_limit_pct / 100, 2)
            total_buffer = round(initial_balance * rules.total_drawdown_limit_pct / 100, 2)
            profit_needed = round(initial_balance * rules.profit_target_pct / 100, 2)

            return _ok(
                config_id=config_id,
                firm_name=rules.firm_name,
                initial_balance=initial_balance,
                daily_drawdown_limit_pct=rules.daily_drawdown_limit_pct,
                total_drawdown_limit_pct=rules.total_drawdown_limit_pct,
                profit_target_pct=rules.profit_target_pct,
                alert_pct=rules.alert_pct,
                halt_pct=rules.halt_pct,
                daily_buffer_currency=daily_buffer,
                total_buffer_currency=total_buffer,
                profit_target_currency=profit_needed,
                message=(
                    f"Prop firm rules set for '{config_id}' "
                    f"({rules.firm_name or 'custom'}, ${initial_balance:,.0f} account). "
                    f"Daily buffer: ${daily_buffer:,.2f} | Total buffer: ${total_buffer:,.2f} | "
                    f"Target profit: ${profit_needed:,.2f}. "
                    "run_copy_trade_cycle will now enforce these limits automatically."
                ),
            )
        except Exception as exc:
            logger.exception("setup_prop_firm_rules failed")
            return _err(str(exc))


# ---------------------------------------------------------------------------
# Tool 7: get_prop_firm_status
# ---------------------------------------------------------------------------


class GetPropFirmStatusTool(BaseTool):
    """Live prop firm rule check against the follower account's current equity."""

    name = "get_prop_firm_status"
    description = (
        "Run a live prop firm rule check: reads the follower account's current equity "
        "and evaluates it against the configured daily drawdown, total drawdown, and "
        "profit target limits. Shows exactly how much buffer remains before a halt "
        "and tracks progress toward the challenge profit target."
    )
    parameters = {
        "type": "object",
        "properties": {
            "config_id": {
                "type": "string",
                "description": "Copy-trade config id to check.",
            },
        },
        "required": ["config_id"],
    }
    is_readonly = True
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        try:
            from src.copy_trade import get_config
            from src.copy_trade.prop_firm import check_rules
            from src.copy_trade.state import (
                get_day_start_equity,
                get_prop_firm_rules,
            )
            from src.copy_trade.state import utc_now_iso
            from src.copy_trade.engine import _extract_equity
            from src.trading.service import get_account

            config_id = str(kwargs["config_id"]).strip()
            config = get_config(config_id)
            if config is None:
                return _err(f"No copy-trade config found with id '{config_id}'.")

            rules = get_prop_firm_rules(config_id)
            if rules is None:
                return _err(
                    f"No prop firm rules configured for '{config_id}'. "
                    "Call setup_prop_firm_rules first."
                )

            # Read current equity from follower account.
            try:
                acct = get_account(config.follower_profile_id)
                current_equity, _ = _extract_equity(acct)
            except Exception as exc:
                return _err(f"Could not read follower account equity: {exc}")

            if current_equity is None:
                return _err(
                    "Follower account did not return equity data. "
                    "Try running run_copy_trade_cycle once to capture a snapshot."
                )

            day_start = get_day_start_equity(config_id)
            ts = utc_now_iso()
            result = check_rules(rules, current_equity, day_start, ts=ts)

            # Format a readable status block.
            status_icon = "🛑 HALTED" if result.should_halt else ("⚠️ ALERT" if result.should_alert else "✅ OK")
            lines = [
                f"Prop Firm Status — {rules.firm_name or 'Custom'} ({config_id})",
                f"Overall: {status_icon}",
                f"Account: ${current_equity:,.2f}  |  Initial: ${rules.initial_balance:,.2f}",
                "─" * 48,
            ]
            lines.extend(result.summary_lines())

            return _ok(
                config_id=config_id,
                firm_name=rules.firm_name,
                should_halt=result.should_halt,
                should_alert=result.should_alert,
                current_equity=current_equity,
                day_start_equity=day_start,
                initial_balance=rules.initial_balance,
                checks=[c.to_dict() for c in result.checks],
                text="\n".join(lines),
            )
        except Exception as exc:
            logger.exception("get_prop_firm_status failed")
            return _err(str(exc))
