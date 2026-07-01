"""Persistent state store for Copy Trade configs and cycle history.

Configs are stored in ``~/.vibe-trading/copy_trade/configs.json``.
Cycle results (last N per config) are in ``~/.vibe-trading/copy_trade/cycles/<config_id>.jsonl``.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.copy_trade.models import CopyTradeConfig, CopyTradeCycleResult
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.copy_trade.prop_firm import PropFirmRules
    from src.copy_trade.signal_models import SignalConfig, SignalCycleResult

logger = logging.getLogger(__name__)

_MAX_CYCLE_HISTORY = 50


def _copy_trade_root() -> Path:
    root = Path.home() / ".vibe-trading" / "copy_trade"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _configs_path() -> Path:
    return _copy_trade_root() / "configs.json"


def _prop_firm_rules_path() -> Path:
    return _copy_trade_root() / "prop_firm_rules.json"


def _cycle_log_path(config_id: str) -> Path:
    cycles_dir = _copy_trade_root() / "cycles"
    cycles_dir.mkdir(parents=True, exist_ok=True)
    return cycles_dir / f"{config_id}.jsonl"


def new_config_id() -> str:
    return f"ct_{uuid.uuid4().hex[:12]}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---- Config CRUD -------------------------------------------------------


def load_all_configs() -> dict[str, CopyTradeConfig]:
    """Return all stored configs keyed by config_id."""
    path = _configs_path()
    if not path.exists():
        return {}
    try:
        raw: dict[str, Any] = json.loads(path.read_text())
        return {cid: CopyTradeConfig.from_dict(data) for cid, data in raw.items()}
    except Exception as exc:
        logger.warning("Could not load copy trade configs: %s", exc)
        return {}


def save_config(config: CopyTradeConfig) -> None:
    """Persist a single config (upsert by config_id)."""
    all_configs = load_all_configs()
    all_configs[config.config_id] = config
    _configs_path().write_text(
        json.dumps({cid: c.to_dict() for cid, c in all_configs.items()}, indent=2),
        encoding="utf-8",
    )


def delete_config(config_id: str) -> bool:
    """Remove a config; returns True if it existed."""
    all_configs = load_all_configs()
    if config_id not in all_configs:
        return False
    del all_configs[config_id]
    _configs_path().write_text(
        json.dumps({cid: c.to_dict() for cid, c in all_configs.items()}, indent=2),
        encoding="utf-8",
    )
    return True


def get_config(config_id: str) -> CopyTradeConfig | None:
    return load_all_configs().get(config_id)


# ---- Cycle history -----------------------------------------------------


def append_cycle_result(result: CopyTradeCycleResult) -> None:
    """Append a cycle result to the per-config JSONL log (capped at 50 entries)."""
    log_path = _cycle_log_path(result.config_id)
    lines: list[str] = []
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").splitlines()
    lines.append(json.dumps(result.to_dict(), ensure_ascii=False, default=str))
    # Keep only the last N entries.
    if len(lines) > _MAX_CYCLE_HISTORY:
        lines = lines[-_MAX_CYCLE_HISTORY:]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_recent_cycles(config_id: str, n: int = 10) -> list[dict[str, Any]]:
    """Return the last ``n`` cycle results for a config, newest first."""
    log_path = _cycle_log_path(config_id)
    if not log_path.exists():
        return []
    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    tail = lines[-n:]
    results = []
    for line in reversed(tail):
        try:
            results.append(json.loads(line))
        except Exception:
            pass
    return results


def get_day_start_equity(config_id: str) -> float | None:
    """Return the first equity_before reading from today's cycles, or None.

    Used by the prop firm monitor to compute daily drawdown from today's open.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    log_path = _cycle_log_path(config_id)
    if not log_path.exists():
        return None
    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    for line in lines:  # oldest first (file is append-only)
        try:
            record = json.loads(line)
            ts = str(record.get("ts", ""))[:10]
            if ts == today:
                eq = record.get("equity_before")
                if eq is not None:
                    return float(eq)
        except Exception:
            pass
    return None


# ---- Prop firm rules CRUD ---------------------------------------------


def load_all_prop_firm_rules() -> dict[str, Any]:
    """Return all stored prop firm rule sets keyed by config_id."""
    path = _prop_firm_rules_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not load prop firm rules: %s", exc)
        return {}


def save_prop_firm_rules(rules: "PropFirmRules") -> None:
    """Persist prop firm rules for a config (upsert by config_id)."""
    all_rules = load_all_prop_firm_rules()
    all_rules[rules.config_id] = rules.to_dict()
    _prop_firm_rules_path().write_text(
        json.dumps(all_rules, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def get_prop_firm_rules(config_id: str) -> "PropFirmRules | None":
    """Load prop firm rules for a config, or None if not configured."""
    from src.copy_trade.prop_firm import PropFirmRules

    all_rules = load_all_prop_firm_rules()
    data = all_rules.get(config_id)
    if data is None:
        return None
    try:
        return PropFirmRules.from_dict(data)
    except Exception as exc:
        logger.warning("Could not parse prop firm rules for %s: %s", config_id, exc)
        return None


def delete_prop_firm_rules(config_id: str) -> bool:
    """Remove prop firm rules for a config; returns True if they existed."""
    all_rules = load_all_prop_firm_rules()
    if config_id not in all_rules:
        return False
    del all_rules[config_id]
    _prop_firm_rules_path().write_text(
        json.dumps(all_rules, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return True


# ---- Signal config CRUD -----------------------------------------------


def _signal_configs_path() -> Path:
    return _copy_trade_root() / "signal_configs.json"


def _signal_cycle_log_path(config_id: str) -> Path:
    cycles_dir = _copy_trade_root() / "signal_cycles"
    cycles_dir.mkdir(parents=True, exist_ok=True)
    return cycles_dir / f"{config_id}.jsonl"


def new_signal_config_id() -> str:
    return f"sig_{uuid.uuid4().hex[:12]}"


def load_all_signal_configs() -> "dict[str, SignalConfig]":
    """Return all stored signal configs keyed by config_id."""
    from src.copy_trade.signal_models import SignalConfig

    path = _signal_configs_path()
    if not path.exists():
        return {}
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return {cid: SignalConfig.from_dict(data) for cid, data in raw.items()}
    except Exception as exc:
        logger.warning("Could not load signal configs: %s", exc)
        return {}


def save_signal_config(config: "SignalConfig") -> None:
    """Persist a signal config (upsert by config_id)."""
    all_configs = load_all_signal_configs()
    all_configs[config.config_id] = config
    _signal_configs_path().write_text(
        json.dumps({cid: c.to_dict() for cid, c in all_configs.items()}, indent=2),
        encoding="utf-8",
    )


def get_signal_config(config_id: str) -> "SignalConfig | None":
    return load_all_signal_configs().get(config_id)


def delete_signal_config(config_id: str) -> bool:
    all_configs = load_all_signal_configs()
    if config_id not in all_configs:
        return False
    del all_configs[config_id]
    _signal_configs_path().write_text(
        json.dumps({cid: c.to_dict() for cid, c in all_configs.items()}, indent=2),
        encoding="utf-8",
    )
    return True


def append_signal_cycle_result(result: "SignalCycleResult") -> None:
    """Append a signal cycle result to per-config JSONL log (capped at 50)."""
    log_path = _signal_cycle_log_path(result.config_id)
    lines: list[str] = []
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").splitlines()
    lines.append(json.dumps(result.to_dict(), ensure_ascii=False, default=str))
    if len(lines) > _MAX_CYCLE_HISTORY:
        lines = lines[-_MAX_CYCLE_HISTORY:]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- Tracked open positions -------------------------------------------


def _tracked_positions_path(config_id: str) -> Path:
    d = _copy_trade_root() / "tracked_positions"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{config_id}.json"


def save_tracked_position(
    config_id: str,
    position_id: str,
    symbol: str,
    side: str,
    entry_time: str,
    entry_price: float,
    volume: int,
) -> None:
    """Record an open position so the engine can manage time/reversal exits."""
    path = _tracked_positions_path(config_id)
    positions: dict[str, Any] = {}
    if path.exists():
        try:
            positions = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    positions[str(position_id)] = {
        "position_id": str(position_id),
        "symbol": symbol,
        "side": side,
        "entry_time": entry_time,
        "entry_price": entry_price,
        "volume": volume,
        "config_id": config_id,
    }
    path.write_text(json.dumps(positions, indent=2, ensure_ascii=False), encoding="utf-8")


def load_tracked_positions(config_id: str) -> dict[str, dict[str, Any]]:
    """Return all tracked open positions for a config keyed by position_id."""
    path = _tracked_positions_path(config_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def remove_tracked_position(config_id: str, position_id: str) -> None:
    """Remove a position from tracking (called after close or SL/TP hit)."""
    path = _tracked_positions_path(config_id)
    if not path.exists():
        return
    try:
        positions = json.loads(path.read_text(encoding="utf-8"))
        positions.pop(str(position_id), None)
        path.write_text(json.dumps(positions, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def load_recent_signal_cycles(config_id: str, n: int = 10) -> list[dict[str, Any]]:
    """Return the last ``n`` signal cycle results for a config, newest first."""
    log_path = _signal_cycle_log_path(config_id)
    if not log_path.exists():
        return []
    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    tail = lines[-n:]
    results = []
    for line in reversed(tail):
        try:
            results.append(json.loads(line))
        except Exception:
            pass
    return results
