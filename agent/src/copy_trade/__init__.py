"""Copy Trade engine — public API."""

from src.copy_trade.engine import compute_deltas, run_copy_trade_cycle
from src.copy_trade.models import CopyTradeConfig, CopyTradeCycleResult, DailyPnL, PositionDelta
from src.copy_trade.prop_firm import PropFirmRules, check_rules
from src.copy_trade.reporter import build_daily_pnl_report, format_pnl_report_text
from src.copy_trade.signal_engine import run_signal_cycle
from src.copy_trade.signal_models import Signal, SignalConfig, SignalCycleResult
from src.copy_trade.state import (
    delete_config,
    delete_signal_config,
    get_config,
    get_day_start_equity,
    get_prop_firm_rules,
    get_signal_config,
    load_all_configs,
    load_all_signal_configs,
    load_recent_cycles,
    load_recent_signal_cycles,
    new_config_id,
    new_signal_config_id,
    save_config,
    save_prop_firm_rules,
    save_signal_config,
)

__all__ = [
    "compute_deltas",
    "run_copy_trade_cycle",
    "run_signal_cycle",
    "build_daily_pnl_report",
    "format_pnl_report_text",
    "check_rules",
    "CopyTradeConfig",
    "CopyTradeCycleResult",
    "DailyPnL",
    "PositionDelta",
    "PropFirmRules",
    "Signal",
    "SignalConfig",
    "SignalCycleResult",
    "delete_config",
    "delete_signal_config",
    "get_config",
    "get_day_start_equity",
    "get_prop_firm_rules",
    "get_signal_config",
    "load_all_configs",
    "load_all_signal_configs",
    "load_recent_cycles",
    "load_recent_signal_cycles",
    "new_config_id",
    "new_signal_config_id",
    "save_config",
    "save_prop_firm_rules",
    "save_signal_config",
]
