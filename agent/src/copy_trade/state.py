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

logger = logging.getLogger(__name__)

_MAX_CYCLE_HISTORY = 50


def _copy_trade_root() -> Path:
    root = Path.home() / ".vibe-trading" / "copy_trade"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _configs_path() -> Path:
    return _copy_trade_root() / "configs.json"


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
