"""Background scheduler for AI signal trading.

Runs as a daemon thread — starts automatically when the first scheduled signal
config is activated and survives the agent's conversational turns. Each tick
(every 60 s) it checks which configs are due and fires ``run_signal_cycle``.

"Due" means: the config has ``interval_minutes`` set AND the gap since its
last recorded cycle is ≥ interval_minutes. Last-run time is derived from the
most recent entry in the config's cycle JSONL log, so the scheduler naturally
resumes after a restart without a separate state file.

Thread safety: ``load_all_signal_configs`` / ``load_recent_signal_cycles`` are
read-only JSONL/JSON reads; ``run_signal_cycle`` writes only to its own per-
config JSONL. Concurrent writes from multiple tools are unlikely (the agent is
single-session), and JSONL appends are atomic at the OS level on Linux.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# How often the daemon wakes to check for due configs (seconds).
_CHECK_INTERVAL_S = 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _last_run_dt(config_id: str) -> datetime | None:
    """Return the UTC timestamp of the most recent cycle for *config_id*, or None."""
    from src.copy_trade.state import load_recent_signal_cycles

    cycles = load_recent_signal_cycles(config_id, n=1)
    if not cycles:
        return None
    ts_str = str(cycles[0].get("ts", ""))
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return None


def _is_due(config_id: str, interval_minutes: int) -> bool:
    """Return True if the config hasn't run within the last *interval_minutes*."""
    last = _last_run_dt(config_id)
    if last is None:
        return True  # never run — due immediately
    elapsed_s = (_utc_now() - last).total_seconds()
    return elapsed_s >= interval_minutes * 60


class _SignalSchedulerDaemon:
    """Singleton background thread that fires scheduled signal cycles."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop: threading.Event = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="signal-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("[signal_scheduler] started (check interval=%ds)", _CHECK_INTERVAL_S)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None
        logger.info("[signal_scheduler] stopped")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("[signal_scheduler] unexpected error in tick")
            self._stop.wait(_CHECK_INTERVAL_S)

    def _tick(self) -> None:
        from src.copy_trade.signal_engine import run_signal_cycle
        from src.copy_trade.state import load_all_signal_configs

        configs = load_all_signal_configs()
        now = _utc_now()

        # Skip everything if forex market is closed (weekend).
        from src.copy_trade.market_hours import is_market_open
        market_open, _ = is_market_open(now)
        if not market_open:
            return

        for config in configs.values():
            if not config.enabled:
                continue

            # --- Fast news check (every news_check_interval_minutes, default 5 min) ---
            if getattr(config, "news_filter_enabled", True):
                news_interval_s = getattr(config, "news_check_interval_minutes", 5) * 60
                last_check = _last_news_check.get(config.config_id)
                if last_check is None or (now - last_check).total_seconds() >= news_interval_s:
                    _last_news_check[config.config_id] = now
                    try:
                        _run_news_check(config)
                    except Exception as exc:
                        logger.error(
                            "[signal_scheduler] news check failed for %s: %s",
                            config.config_id, exc,
                        )

            # --- Full signal cycle (every interval_minutes, default 15 min) ---
            if not config.interval_minutes:
                continue
            if not _is_due(config.config_id, config.interval_minutes):
                continue

            label = config.label or config.config_id
            logger.info("[signal_scheduler] firing cycle for %s", label)
            try:
                result = run_signal_cycle(config)
                n_placed = len(result.orders_placed)
                n_skip = len(result.orders_skipped)
                n_err = len(result.errors)
                logger.info(
                    "[signal_scheduler] %s done — placed=%d skipped=%d errors=%d",
                    label, n_placed, n_skip, n_err,
                )
            except Exception as exc:
                logger.error("[signal_scheduler] cycle failed for %s: %s", label, exc)


# Per-config last-news-check timestamps (populated by the daemon).
_last_news_check: dict[str, datetime] = {}


def _run_news_check(config: Any) -> None:
    """Close all tracked positions immediately if a high-impact event is near."""
    from src.copy_trade.news_filter import is_news_blackout
    from src.copy_trade.state import load_tracked_positions, remove_tracked_position
    from src.trading.service import close_position

    blackout_minutes = getattr(config, "news_blackout_minutes", 30)
    in_blackout, reason = is_news_blackout(_utc_now(), blackout_minutes)
    if not in_blackout:
        return

    tracked = load_tracked_positions(config.config_id)
    if not tracked:
        return

    label = config.label or config.config_id
    logger.warning(
        "[signal_scheduler] NEWS BLACKOUT — closing %d open position(s) for %s: %s",
        len(tracked), label, reason,
    )
    for pid, pos_info in list(tracked.items()):
        try:
            close_position(
                int(pos_info["position_id"]),
                config.profile_id,
                volume=int(pos_info["volume"]),
            )
            remove_tracked_position(config.config_id, pid)
            logger.info(
                "[signal_scheduler] Closed %s %s (pos %s) — news",
                pos_info.get("side", "").upper(), pos_info.get("symbol", ""), pid,
            )
        except Exception as exc:
            logger.warning(
                "[signal_scheduler] Could not close position %s for news: %s", pid, exc,
            )


# Module-level singleton.
_daemon = _SignalSchedulerDaemon()


def scheduler_running() -> bool:
    return _daemon.running


def start_scheduler() -> None:
    _daemon.start()


def stop_scheduler() -> None:
    _daemon.stop()
