"""MetaTrader 5 connector via the official ``MetaTrader5`` Python package.

Wraps the five standard read operations (account, positions, orders, quote,
history) and order placement/cancellation for MT5-based brokers such as
Vantage Markets, IC Markets, and any other MT5 broker.

**Windows-only:** The ``MetaTrader5`` package communicates with a locally
running MT5 terminal. This connector is unavailable on Linux/macOS.

**Credentials** are stored in ``~/.vibe-trading/mt5.json``:

.. code-block:: json

    {
        "login": 12345678,
        "password": "your_password",
        "server": "Vantage-Real",
        "profile": "live",
        "path": "C:/Program Files/Vantage MT5/terminal64.exe"
    }

``path`` is optional — omit it to let the package auto-discover the terminal.

**Paper vs live** is operator-declared via the ``profile`` field (``demo`` or
``live``) because MT5 account responses do not embed account type in a way
this connector can reliably verify across all brokers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from src.config.paths import get_runtime_root

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "mt5.json"

PROFILE_ENVIRONMENTS = {
    "demo": "paper",
    "live": "live",
}

# MT5 order type constants (mirrors MetaTrader5 package values)
_ORDER_TYPE_BUY = 0
_ORDER_TYPE_SELL = 1

# MT5 trade action
_TRADE_ACTION_DEAL = 1   # market order (immediate execution)
_TRADE_ACTION_PENDING = 5  # pending order


class MT5DependencyError(RuntimeError):
    """Raised when the ``MetaTrader5`` package is not installed."""


class MT5ConfigError(RuntimeError):
    """Raised when the connector configuration is missing or invalid."""


class MT5APIError(RuntimeError):
    """Raised when an MT5 terminal call fails."""


@dataclass(frozen=True)
class MT5Config:
    """MetaTrader 5 connector connection settings.

    Args:
        login: MT5 account number (integer).
        password: MT5 account password.
        server: Broker server name, e.g. ``Vantage-Real`` or ``Vantage-Demo``.
        profile: ``demo`` or ``live`` (operator-declared).
        path: Optional path to the MT5 terminal executable.
        timeout: Connection timeout in milliseconds (MT5 package unit).
        readonly: Whether order methods are exposed on this config.
    """

    login: int = 0
    password: str = ""
    server: str = ""
    profile: str = "demo"
    path: str = ""
    timeout: int = 60_000
    readonly: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None = None) -> "MT5Config":
        payload = dict(data or {})
        profile = str(payload.get("profile") or "demo").strip().lower()
        if profile not in PROFILE_ENVIRONMENTS:
            raise MT5ConfigError("profile must be 'demo' or 'live'")
        login_raw = payload.get("login", 0)
        try:
            login = int(login_raw) if login_raw else 0
        except (TypeError, ValueError):
            raise MT5ConfigError("login must be an integer account number")
        return cls(
            login=login,
            password=str(payload.get("password") or "").strip(),
            server=str(payload.get("server") or "").strip(),
            profile=profile,
            path=str(payload.get("path") or "").strip(),
            timeout=int(payload.get("timeout") or 60_000),
            readonly=bool(payload.get("readonly", True)),
        )

    def with_overrides(
        self,
        *,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        profile: str | None = None,
        path: str | None = None,
    ) -> "MT5Config":
        payload = asdict(self)
        if login is not None:
            payload["login"] = login
        if password is not None:
            payload["password"] = password
        if server is not None:
            payload["server"] = server
        if profile is not None:
            payload["profile"] = profile
        if path is not None:
            payload["path"] = path
        return MT5Config.from_mapping(payload)

    @property
    def environment(self) -> str:
        return PROFILE_ENVIRONMENTS.get(self.profile, "paper")

    @property
    def is_demo(self) -> bool:
        return self.profile == "demo"


_OVERRIDE_KEYS = ("login", "password", "server", "profile", "path")


def build_config(
    profile_config: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> MT5Config:
    """Resolve config: saved file ← profile defaults ← CLI overrides."""
    base = asdict(load_config())
    for key, value in dict(profile_config or {}).items():
        if value is not None:
            base[key] = value
    cfg = MT5Config.from_mapping(base)
    clean = {k: v for k, v in dict(overrides or {}).items() if k in _OVERRIDE_KEYS and v not in (None, "")}
    return cfg.with_overrides(**clean) if clean else cfg


def config_path() -> Path:
    return get_runtime_root() / CONFIG_FILENAME


def load_config() -> MT5Config:
    """Load MT5 settings from ``~/.vibe-trading/mt5.json``."""
    path = config_path()
    if not path.exists():
        return MT5Config()
    try:
        return MT5Config.from_mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MT5ConfigError(f"invalid MT5 config at {path}: {exc}") from exc


def save_config(config: MT5Config) -> Path:
    """Persist MT5 settings with owner-only permissions."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


# ---- MT5 terminal lifecycle --------------------------------------------


def _import_mt5():
    """Import the MetaTrader5 package or raise MT5DependencyError."""
    try:
        import MetaTrader5 as mt5
        return mt5
    except ImportError:
        raise MT5DependencyError(
            "The 'MetaTrader5' package is not installed or unavailable on this platform "
            "(Windows only). Install it with: pip install MetaTrader5"
        )


def _connect(config: MT5Config):
    """Initialise and log in to the MT5 terminal. Returns the mt5 module."""
    mt5 = _import_mt5()

    init_kwargs: dict[str, Any] = {"timeout": config.timeout}
    if config.path:
        init_kwargs["path"] = config.path
    if config.login:
        init_kwargs["login"] = config.login
    if config.password:
        init_kwargs["password"] = config.password
    if config.server:
        init_kwargs["server"] = config.server

    if not mt5.initialize(**init_kwargs):
        error = mt5.last_error()
        raise MT5APIError(f"MT5 terminal initialization failed: {error}")

    return mt5


def _shutdown(mt5) -> None:
    try:
        mt5.shutdown()
    except Exception:
        pass


# ---- Public interface --------------------------------------------------


def check_status(config: MT5Config | None = None) -> dict[str, Any]:
    """Check MT5 terminal connectivity and config completeness."""
    cfg = config or load_config()
    report: dict[str, Any] = {
        "status": "ok",
        "config": _public_config(cfg),
        "sdk": {"package": "MetaTrader5", "installed": False},
        "profile": cfg.profile,
    }

    try:
        _import_mt5()
        report["sdk"]["installed"] = True
    except MT5DependencyError as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        return report

    missing = _missing_fields(cfg)
    if missing:
        report["status"] = "error"
        report["error"] = f"MT5 connector not configured: missing {', '.join(missing)}."
        return report

    try:
        snapshot = get_account_snapshot(cfg)
        report["account"] = {
            "login": snapshot.get("login"),
            "server": snapshot.get("server"),
            "currency": snapshot.get("currency"),
            "balance": snapshot.get("balance"),
            "profile": cfg.profile,
        }
    except (MT5ConfigError, MT5APIError) as exc:
        report["status"] = "error"
        report["error"] = str(exc)
    except Exception as exc:
        report["status"] = "error"
        report["error"] = f"MT5 check failed: {exc}"

    return report


def get_account_snapshot(config: MT5Config | None = None) -> dict[str, Any]:
    """Fetch account info from the connected MT5 terminal."""
    cfg = config or load_config()
    mt5 = _connect(cfg)
    try:
        info = mt5.account_info()
        if info is None:
            raise MT5APIError(f"MT5 account_info() returned None: {mt5.last_error()}")
        d = info._asdict()
        return {
            "status": "ok",
            "profile": cfg.profile,
            "login": d.get("login"),
            "server": d.get("server"),
            "currency": d.get("currency"),
            "balance": d.get("balance"),
            "equity": d.get("equity"),
            "margin": d.get("margin"),
            "margin_free": d.get("margin_free"),
            "margin_level": d.get("margin_level"),
            "profit": d.get("profit"),
            "leverage": d.get("leverage"),
            "name": d.get("name"),
            "company": d.get("company"),
        }
    finally:
        _shutdown(mt5)


def get_positions(config: MT5Config | None = None) -> dict[str, Any]:
    """Fetch all open positions from the MT5 terminal."""
    cfg = config or load_config()
    mt5 = _connect(cfg)
    try:
        raw = mt5.positions_get()
        if raw is None:
            err = mt5.last_error()
            if err and err[0] != 1:  # 1 = ERR_SUCCESS (no positions)
                raise MT5APIError(f"MT5 positions_get() failed: {err}")
            raw = ()
        positions = [_position_to_dict(p) for p in raw]
        return {
            "status": "ok",
            "profile": cfg.profile,
            "positions": positions,
        }
    finally:
        _shutdown(mt5)


def get_open_orders(
    config: MT5Config | None = None,
    *,
    include_executions: bool = False,
) -> dict[str, Any]:
    """Fetch pending (open) orders from the MT5 terminal."""
    cfg = config or load_config()
    mt5 = _connect(cfg)
    try:
        raw = mt5.orders_get()
        if raw is None:
            err = mt5.last_error()
            if err and err[0] != 1:
                raise MT5APIError(f"MT5 orders_get() failed: {err}")
            raw = ()
        orders = [_order_to_dict(o) for o in raw]

        result: dict[str, Any] = {
            "status": "ok",
            "profile": cfg.profile,
            "open_orders": orders,
        }

        if include_executions:
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            from_date = now - timedelta(days=30)
            deals = mt5.history_deals_get(from_date, now)
            result["executions"] = [_deal_to_dict(d) for d in (deals or ())]

        return result
    finally:
        _shutdown(mt5)


def get_quote(symbol: str, *, config: MT5Config | None = None, **_: Any) -> dict[str, Any]:
    """Fetch the latest bid/ask tick for a symbol."""
    cfg = config or load_config()
    mt5 = _connect(cfg)
    try:
        clean_symbol = str(symbol or "").strip().upper()
        tick = mt5.symbol_info_tick(clean_symbol)
        if tick is None:
            raise MT5APIError(
                f"MT5: no tick data for '{clean_symbol}'. "
                "Check the symbol name matches your broker's instrument list."
            )
        d = tick._asdict()
        return {
            "status": "ok",
            "profile": cfg.profile,
            "symbol": clean_symbol,
            "bid": d.get("bid"),
            "ask": d.get("ask"),
            "last": d.get("last") or d.get("bid"),
            "volume": d.get("volume"),
            "time": d.get("time"),
        }
    finally:
        _shutdown(mt5)


def get_historical_bars(
    symbol: str,
    *,
    config: MT5Config | None = None,
    period: str = "1d",
    limit: int = 90,
    **_: Any,
) -> dict[str, Any]:
    """Fetch OHLCV bars for a symbol."""
    cfg = config or load_config()
    mt5 = _connect(cfg)
    try:
        clean_symbol = str(symbol or "").strip().upper()
        timeframe = _period_to_mt5_timeframe(mt5, period)
        rates = mt5.copy_rates_from_pos(clean_symbol, timeframe, 0, int(limit))
        if rates is None:
            raise MT5APIError(f"MT5: no bar data for '{clean_symbol}' @ {period}")
        bars = [
            {
                "time": int(r["time"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["tick_volume"]),
            }
            for r in rates
        ]
        return {
            "status": "ok",
            "profile": cfg.profile,
            "symbol": clean_symbol,
            "period": period,
            "bars": bars,
        }
    finally:
        _shutdown(mt5)


def place_order(
    config: MT5Config | None = None,
    *,
    symbol: str,
    side: str,
    quantity: float | None = None,
    notional: float | None = None,
    order_type: str = "market",
    limit_price: float | None = None,
    time_in_force: str = "day",
) -> dict[str, Any]:
    """Place a market or limit order via the MT5 terminal.

    Args:
        symbol: Broker instrument name (e.g. ``EURUSD``, ``US30``, ``XAUUSD``).
        side: ``"buy"`` or ``"sell"``.
        quantity: Volume in lots (MT5 unit). Required unless ``notional`` is set.
        notional: Approximate USD notional — converted to lots using the current ask.
        order_type: ``"market"`` (default) or ``"limit"``.
        limit_price: Required when ``order_type == "limit"``.
        time_in_force: Ignored for MT5 market orders (always GTC for pending orders).
    """
    cfg = config or load_config()
    mt5 = _connect(cfg)
    try:
        clean_symbol = str(symbol or "").strip().upper()
        clean_side = str(side or "").strip().lower()
        if clean_side not in ("buy", "sell"):
            raise MT5ConfigError(f"side must be 'buy' or 'sell', got '{clean_side}'")

        # Resolve volume.
        volume = _resolve_volume(mt5, clean_symbol, quantity, notional, clean_side)

        action = _TRADE_ACTION_DEAL if order_type == "market" else _TRADE_ACTION_PENDING
        order_type_const = _ORDER_TYPE_BUY if clean_side == "buy" else _ORDER_TYPE_SELL

        # For market orders use the current bid/ask as price (required by some brokers).
        price = 0.0
        if order_type == "market":
            tick = mt5.symbol_info_tick(clean_symbol)
            if tick:
                price = tick.ask if clean_side == "buy" else tick.bid
        elif limit_price is not None:
            price = float(limit_price)

        request: dict[str, Any] = {
            "action": action,
            "symbol": clean_symbol,
            "volume": float(volume),
            "type": order_type_const,
            "price": price,
            "deviation": 20,  # max price deviation in points
            "magic": 234000,  # EA magic number (identifies vibe-trading orders)
            "comment": "vibe-trading copy-trade",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            raise MT5APIError(f"MT5 order_send returned None: {mt5.last_error()}")

        d = result._asdict()
        retcode = d.get("retcode", -1)
        if retcode != mt5.TRADE_RETCODE_DONE:
            raise MT5APIError(
                f"MT5 order rejected (retcode={retcode}): {d.get('comment', 'unknown error')}"
            )

        return {
            "status": "ok",
            "profile": cfg.profile,
            "symbol": clean_symbol,
            "side": clean_side,
            "volume": volume,
            "order_type": order_type,
            "price": d.get("price"),
            "order_id": d.get("order"),
            "deal_id": d.get("deal"),
            "retcode": retcode,
            "comment": d.get("comment"),
        }
    finally:
        _shutdown(mt5)


def cancel_order(
    config: MT5Config | None = None,
    order_id: str = "",
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Cancel a pending MT5 order by ticket id."""
    cfg = config or load_config()
    mt5 = _connect(cfg)
    try:
        ticket = int(order_id)
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket,
        }
        result = mt5.order_send(request)
        if result is None:
            raise MT5APIError(f"MT5 cancel order_send returned None: {mt5.last_error()}")
        d = result._asdict()
        retcode = d.get("retcode", -1)
        if retcode != mt5.TRADE_RETCODE_DONE:
            raise MT5APIError(
                f"MT5 cancel rejected (retcode={retcode}): {d.get('comment', 'unknown')}"
            )
        return {
            "status": "ok",
            "profile": cfg.profile,
            "order_id": order_id,
            "retcode": retcode,
            "comment": d.get("comment"),
        }
    except (TypeError, ValueError) as exc:
        raise MT5ConfigError(f"order_id must be a valid integer ticket: {exc}") from exc
    finally:
        _shutdown(mt5)


# ---- Helpers -----------------------------------------------------------


def _resolve_volume(
    mt5: Any,
    symbol: str,
    quantity: float | None,
    notional: float | None,
    side: str,
) -> float:
    """Return lot volume, computing from notional if quantity is not given."""
    if quantity is not None:
        return round(float(quantity), 2)
    if notional is not None:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MT5APIError(f"Cannot fetch tick for '{symbol}' to compute lot size from notional.")
        price = tick.ask if side == "buy" else tick.bid
        info = mt5.symbol_info(symbol)
        if info is None or price <= 0:
            raise MT5APIError(f"Cannot fetch symbol info for '{symbol}'.")
        contract_size = getattr(info, "trade_contract_size", 100_000) or 100_000
        raw_lots = float(notional) / (price * contract_size)
        # Round to broker's lot step.
        lot_step = getattr(info, "volume_step", 0.01) or 0.01
        lots = round(raw_lots / lot_step) * lot_step
        return round(max(lots, getattr(info, "volume_min", lot_step)), 2)
    raise MT5ConfigError("Either 'quantity' (lots) or 'notional' (USD) must be provided.")


def _period_to_mt5_timeframe(mt5: Any, period: str) -> int:
    """Map generic period strings to MT5 timeframe constants."""
    mapping = {
        "1m": mt5.TIMEFRAME_M1,
        "5m": mt5.TIMEFRAME_M5,
        "15m": mt5.TIMEFRAME_M15,
        "30m": mt5.TIMEFRAME_M30,
        "1h": mt5.TIMEFRAME_H1,
        "4h": mt5.TIMEFRAME_H4,
        "1d": mt5.TIMEFRAME_D1,
        "1w": mt5.TIMEFRAME_W1,
        "1mo": mt5.TIMEFRAME_MN1,
    }
    tf = mapping.get(str(period).lower())
    if tf is None:
        logger.warning("Unknown period '%s', defaulting to D1", period)
        return mt5.TIMEFRAME_D1
    return tf


def _missing_fields(config: MT5Config) -> list[str]:
    missing = []
    if not config.login:
        missing.append("login")
    if not config.password:
        missing.append("password")
    if not config.server:
        missing.append("server")
    return missing


def _public_config(config: MT5Config) -> dict[str, Any]:
    data = asdict(config)
    if data.get("password"):
        data["password"] = "***redacted***"
    return data


def _position_to_dict(pos: Any) -> dict[str, Any]:
    d = pos._asdict() if hasattr(pos, "_asdict") else {}
    return {
        "symbol": d.get("symbol"),
        "ticket": d.get("ticket"),
        "side": "buy" if d.get("type") == _ORDER_TYPE_BUY else "sell",
        "quantity": d.get("volume"),
        "open_price": d.get("price_open"),
        "current_price": d.get("price_current"),
        "sl": d.get("sl"),
        "tp": d.get("tp"),
        "profit": d.get("profit"),
        "swap": d.get("swap"),
        "time": d.get("time"),
        "magic": d.get("magic"),
        "comment": d.get("comment"),
    }


def _order_to_dict(order: Any) -> dict[str, Any]:
    d = order._asdict() if hasattr(order, "_asdict") else {}
    return {
        "order_id": str(d.get("ticket", "")),
        "symbol": d.get("symbol"),
        "side": "buy" if d.get("type") == _ORDER_TYPE_BUY else "sell",
        "quantity": d.get("volume_current"),
        "volume_initial": d.get("volume_initial"),
        "price": d.get("price_open"),
        "sl": d.get("sl"),
        "tp": d.get("tp"),
        "time_setup": d.get("time_setup"),
        "magic": d.get("magic"),
        "comment": d.get("comment"),
    }


def _deal_to_dict(deal: Any) -> dict[str, Any]:
    d = deal._asdict() if hasattr(deal, "_asdict") else {}
    return {
        "deal_id": str(d.get("ticket", "")),
        "order_id": str(d.get("order", "")),
        "symbol": d.get("symbol"),
        "side": "buy" if d.get("type") == 0 else "sell",
        "volume": d.get("volume"),
        "price": d.get("price"),
        "profit": d.get("profit"),
        "commission": d.get("commission"),
        "swap": d.get("swap"),
        "time": d.get("time"),
        "magic": d.get("magic"),
        "comment": d.get("comment"),
    }
