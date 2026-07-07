"""Built-in MetaTrader 5 connector profiles.

MT5 connects to a locally running MT5 terminal via the ``MetaTrader5`` Python
package (Windows only). Credentials (login/password/server) are stored in
``~/.vibe-trading/mt5.json``.

Demo accounts use the broker's demo server (e.g. ``Vantage-Demo``); live
accounts use the live server (e.g. ``Vantage-Real``). The ``profile`` field
is operator-declared — the connector records it but MT5 does not embed it in
responses.
"""

from __future__ import annotations

from src.trading.types import READ_CAPABILITIES, TradingProfile

MT5_PROFILES: tuple[TradingProfile, ...] = (
    TradingProfile(
        id="mt5-demo-sdk-readonly",
        connector="mt5",
        label="MetaTrader 5 Demo · Read-Only",
        environment="paper",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "demo"},
        notes=(
            "Reads a MetaTrader 5 demo account via the MetaTrader5 Python package. "
            "Requires MT5 terminal running locally on Windows. "
            "Configure credentials in ~/.vibe-trading/mt5.json."
        ),
    ),
    TradingProfile(
        id="mt5-demo-trade",
        connector="mt5",
        label="MetaTrader 5 Demo · Trade",
        environment="paper",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES + ("orders.place",),
        readonly=False,
        config={"profile": "demo"},
        notes=(
            "Reads and places orders on a MetaTrader 5 demo account. "
            "Requires MT5 terminal running locally on Windows. "
            "Configure credentials in ~/.vibe-trading/mt5.json."
        ),
    ),
    TradingProfile(
        id="mt5-live-sdk-readonly",
        connector="mt5",
        label="MetaTrader 5 Live · Read-Only",
        environment="live",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "live"},
        notes=(
            "Reads a MetaTrader 5 live account (e.g. Vantage-Real) via the "
            "MetaTrader5 Python package. Requires MT5 terminal running locally "
            "on Windows. Configure credentials in ~/.vibe-trading/mt5.json."
        ),
    ),
    TradingProfile(
        id="mt5-live-trade",
        connector="mt5",
        label="MetaTrader 5 Live · Trade",
        environment="live",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES + ("orders.place.requires_mandate",),
        readonly=False,
        config={"profile": "live"},
        notes=(
            "Reads and places orders on a MetaTrader 5 live account (e.g. Vantage). "
            "Requires MT5 terminal running locally on Windows. "
            "Live orders require an authorized mandate. "
            "Configure credentials in ~/.vibe-trading/mt5.json."
        ),
    ),
)
