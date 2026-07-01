"""Prop firm rule monitor for the Copy Trade engine.

Before each sync cycle, the monitor checks the follower account's current equity
against the prop firm's drawdown and profit rules. If a halt threshold is hit, the
cycle skips all orders and returns a clear explanation so the account isn't blown.

Supported rules (all industry-standard):
  daily_drawdown   — equity must not fall > X% from today's starting equity
  total_drawdown   — equity must not fall > X% from the initial funded balance
  profit_target    — informational: track progress toward the challenge target

Alert vs halt:
  alert_pct  (default 80%) — warn when X% of a limit is consumed (still trades)
  halt_pct   (default 95%) — block new orders when X% of a limit is consumed
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

RuleStatus = Literal["ok", "alert", "halt", "target_reached"]

# ---- Well-known prop firm presets ---------------------------------------

PROP_FIRM_PRESETS: dict[str, dict[str, Any]] = {
    "ftmo": {
        "firm_name": "FTMO",
        "daily_drawdown_limit_pct": 5.0,
        "total_drawdown_limit_pct": 10.0,
        "profit_target_pct": 10.0,
    },
    "apex": {
        "firm_name": "Apex Trader Funding",
        "daily_drawdown_limit_pct": 3.0,   # trailing drawdown
        "total_drawdown_limit_pct": 6.0,
        "profit_target_pct": 6.0,
    },
    "mff": {
        "firm_name": "MyForexFunds",
        "daily_drawdown_limit_pct": 5.0,
        "total_drawdown_limit_pct": 10.0,
        "profit_target_pct": 8.0,
    },
    "tft": {
        "firm_name": "The Funded Trader",
        "daily_drawdown_limit_pct": 5.0,
        "total_drawdown_limit_pct": 10.0,
        "profit_target_pct": 10.0,
    },
    "e8": {
        "firm_name": "E8 Funding",
        "daily_drawdown_limit_pct": 5.0,
        "total_drawdown_limit_pct": 8.0,
        "profit_target_pct": 8.0,
    },
}


# ---- Models ------------------------------------------------------------


@dataclass
class PropFirmRules:
    """Prop firm trading rules attached to a copy-trade config.

    Attributes:
        config_id: Matching CopyTradeConfig id.
        firm_name: Human-readable firm name (e.g. ``FTMO``).
        initial_balance: Funded account starting balance in account currency.
        daily_drawdown_limit_pct: Max allowed equity drop from day's open (%).
        total_drawdown_limit_pct: Max allowed equity drop from initial_balance (%).
        profit_target_pct: Challenge profit target as % of initial_balance.
        alert_pct: Fire an alert when this % of a limit is consumed (default 80).
        halt_pct: Block new orders when this % of a limit is consumed (default 95).
        enabled: When False, rules are stored but not enforced.
    """

    config_id: str
    firm_name: str = ""
    initial_balance: float = 0.0
    daily_drawdown_limit_pct: float = 5.0
    total_drawdown_limit_pct: float = 10.0
    profit_target_pct: float = 10.0
    alert_pct: float = 50.0
    halt_pct: float = 70.0
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PropFirmRules":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_preset(cls, config_id: str, preset: str, initial_balance: float) -> "PropFirmRules":
        """Build rules from a named preset (ftmo, apex, mff, tft, e8)."""
        key = preset.strip().lower().replace(" ", "").replace("-", "")
        defaults = PROP_FIRM_PRESETS.get(key, {})
        return cls(
            config_id=config_id,
            initial_balance=initial_balance,
            **{k: v for k, v in defaults.items() if k in cls.__dataclass_fields__},
        )


@dataclass
class RuleCheck:
    """Result of evaluating one prop firm rule.

    Attributes:
        rule: Rule name (``daily_drawdown`` | ``total_drawdown`` | ``profit_target``).
        status: ``ok`` | ``alert`` | ``halt`` | ``target_reached``.
        current_pct: Current drawdown or profit as a percentage.
        limit_pct: The rule's hard limit percentage.
        used_of_limit_pct: How much of the limit is consumed (0–100%).
        remaining_currency: How much equity can still be lost before the limit trips.
        message: Human-readable explanation.
    """

    rule: str
    status: RuleStatus
    current_pct: float
    limit_pct: float
    used_of_limit_pct: float
    remaining_currency: float
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PropFirmCheckResult:
    """Aggregate result of all prop firm rule checks for one cycle.

    Attributes:
        config_id: Which config was checked.
        should_halt: True if any rule is at/past the halt threshold.
        should_alert: True if any rule is in the alert zone (but not halted).
        checks: Individual RuleCheck results.
        current_equity: Equity reading used for this check.
        day_start_equity: Equity at start of today (used for daily drawdown).
        ts: ISO-8601 UTC timestamp.
    """

    config_id: str
    should_halt: bool = False
    should_alert: bool = False
    checks: list[RuleCheck] = field(default_factory=list)
    current_equity: float | None = None
    day_start_equity: float | None = None
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def summary_lines(self) -> list[str]:
        """Return compact status lines for chat output."""
        lines = []
        for c in self.checks:
            icon = {"ok": "✓", "alert": "⚠", "halt": "✗", "target_reached": "★"}.get(c.status, "?")
            lines.append(f"  {icon} {c.rule}: {c.message}")
        return lines


# ---- Core check logic --------------------------------------------------


def _pct_drop(from_value: float, current: float) -> float:
    """Return percentage drop from from_value to current (positive = dropped)."""
    if from_value <= 0:
        return 0.0
    return round((from_value - current) / from_value * 100, 4)


def _pct_gain(from_value: float, current: float) -> float:
    """Return percentage gain from from_value to current."""
    if from_value <= 0:
        return 0.0
    return round((current - from_value) / from_value * 100, 4)


def _rule_status(used_of_limit: float, alert_pct: float, halt_pct: float) -> RuleStatus:
    if used_of_limit >= halt_pct:
        return "halt"
    if used_of_limit >= alert_pct:
        return "alert"
    return "ok"


def check_rules(
    rules: PropFirmRules,
    current_equity: float,
    day_start_equity: float | None,
    ts: str = "",
) -> PropFirmCheckResult:
    """Evaluate all prop firm rules against current equity.

    Args:
        rules: The configured prop firm rules.
        current_equity: Latest follower account equity.
        day_start_equity: First equity reading of today (for daily drawdown).
        ts: ISO-8601 timestamp for this check.

    Returns:
        PropFirmCheckResult with per-rule verdicts and aggregate halt/alert flags.
    """
    checks: list[RuleCheck] = []

    # ---- 1. Daily drawdown -------------------------------------------
    if day_start_equity is not None and day_start_equity > 0:
        daily_dd_pct = _pct_drop(day_start_equity, current_equity)
        daily_limit = rules.daily_drawdown_limit_pct
        used = round(daily_dd_pct / daily_limit * 100, 2) if daily_limit > 0 else 0.0
        status = _rule_status(used, rules.alert_pct, rules.halt_pct)
        remaining = round(day_start_equity * (daily_limit / 100) - (day_start_equity - current_equity), 2)

        if status == "halt":
            msg = (
                f"HALT — daily drawdown {daily_dd_pct:.2f}% exceeds {daily_limit}% limit. "
                f"No new orders until tomorrow."
            )
        elif status == "alert":
            msg = (
                f"ALERT — daily drawdown at {daily_dd_pct:.2f}% ({used:.0f}% of {daily_limit}% limit). "
                f"${remaining:,.2f} remaining before halt."
            )
        else:
            msg = f"{daily_dd_pct:.2f}% used of {daily_limit}% daily limit (${remaining:,.2f} buffer)."

        checks.append(RuleCheck(
            rule="daily_drawdown",
            status=status,
            current_pct=daily_dd_pct,
            limit_pct=daily_limit,
            used_of_limit_pct=used,
            remaining_currency=max(remaining, 0.0),
            message=msg,
        ))
    else:
        checks.append(RuleCheck(
            rule="daily_drawdown",
            status="ok",
            current_pct=0.0,
            limit_pct=rules.daily_drawdown_limit_pct,
            used_of_limit_pct=0.0,
            remaining_currency=rules.initial_balance * rules.daily_drawdown_limit_pct / 100,
            message="No day-start equity yet — will track from next cycle.",
        ))

    # ---- 2. Total drawdown ------------------------------------------
    if rules.initial_balance > 0:
        total_dd_pct = _pct_drop(rules.initial_balance, current_equity)
        total_limit = rules.total_drawdown_limit_pct
        used = round(total_dd_pct / total_limit * 100, 2) if total_limit > 0 else 0.0
        status = _rule_status(used, rules.alert_pct, rules.halt_pct)
        remaining = round(rules.initial_balance * (total_limit / 100) - (rules.initial_balance - current_equity), 2)

        if status == "halt":
            msg = (
                f"HALT — total drawdown {total_dd_pct:.2f}% exceeds {total_limit}% limit. "
                f"Account at risk — stop trading immediately."
            )
        elif status == "alert":
            msg = (
                f"ALERT — total drawdown at {total_dd_pct:.2f}% ({used:.0f}% of {total_limit}% limit). "
                f"${remaining:,.2f} remaining before account fails."
            )
        else:
            msg = f"{total_dd_pct:.2f}% drawdown from initial ${rules.initial_balance:,.2f} (${remaining:,.2f} buffer)."

        checks.append(RuleCheck(
            rule="total_drawdown",
            status=status,
            current_pct=total_dd_pct,
            limit_pct=total_limit,
            used_of_limit_pct=used,
            remaining_currency=max(remaining, 0.0),
            message=msg,
        ))

    # ---- 3. Profit target -------------------------------------------
    if rules.initial_balance > 0 and rules.profit_target_pct > 0:
        profit_pct = _pct_gain(rules.initial_balance, current_equity)
        target = rules.profit_target_pct
        progress = round(profit_pct / target * 100, 2) if target > 0 else 0.0
        target_currency = round(rules.initial_balance * target / 100, 2)
        current_profit = round(current_equity - rules.initial_balance, 2)
        remaining_to_target = round(target_currency - current_profit, 2)

        if profit_pct >= target:
            status = "target_reached"
            msg = (
                f"TARGET REACHED — profit {profit_pct:.2f}% exceeds {target}% target. "
                f"Consider requesting a payout."
            )
        elif progress >= 75:
            status = "alert"
            msg = (
                f"Profit {profit_pct:.2f}% — {progress:.0f}% of {target}% target reached. "
                f"${remaining_to_target:,.2f} to go."
            )
        else:
            status = "ok"
            msg = f"Profit {profit_pct:.2f}% — {progress:.0f}% of {target}% target (${remaining_to_target:,.2f} to go)."

        checks.append(RuleCheck(
            rule="profit_target",
            status=status,
            current_pct=profit_pct,
            limit_pct=target,
            used_of_limit_pct=progress,
            remaining_currency=max(remaining_to_target, 0.0),
            message=msg,
        ))

    should_halt = any(c.status == "halt" for c in checks)
    should_alert = not should_halt and any(c.status in ("alert", "target_reached") for c in checks)

    return PropFirmCheckResult(
        config_id=rules.config_id,
        should_halt=should_halt,
        should_alert=should_alert,
        checks=checks,
        current_equity=current_equity,
        day_start_equity=day_start_equity,
        ts=ts,
    )
