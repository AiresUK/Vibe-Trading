"""cTrader connector profiles.

Works on any platform (Linux, macOS, Windows, ChromeOS) — uses the
cTrader Open API over TCP, no local terminal required.

Credentials are stored in ``~/.vibe-trading/ctrader.json``:

.. code-block:: json

    {
        "client_id": "your_app_client_id",
        "client_secret": "your_app_client_secret",
        "access_token": "your_account_access_token",
        "account_id": 12345678,
        "environment": "demo"
    }

How to get credentials
----------------------
1. Register a free application at https://connect.ctrader.com
   → you get ``client_id`` and ``client_secret``.
2. Run the OAuth2 flow (or copy the token from cTrader Web) to get
   ``access_token`` for your specific account.
3. Find ``account_id`` in cTrader → Settings → Account details.

Compatible brokers: any broker running cTrader (Pepperstone, IC Markets,
Vantage cTrader accounts, FTMO cTrader challenge accounts, etc.).
"""

from src.trading.types import READ_CAPABILITIES, TradingProfile

CTRADER_PROFILES: tuple[TradingProfile, ...] = (
    TradingProfile(
        id="ctrader-demo-sdk-readonly",
        label="cTrader Demo (read-only)",
        connector="ctrader",
        transport="broker_sdk",
        environment="paper",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={},
        notes="Read-only access to a cTrader demo account. Configure credentials in ~/.vibe-trading/ctrader.json.",
    ),
    TradingProfile(
        id="ctrader-demo-trade",
        label="cTrader Demo (trading)",
        connector="ctrader",
        transport="broker_sdk",
        environment="paper",
        capabilities=READ_CAPABILITIES + ("orders.place",),
        readonly=False,
        config={},
        notes="Read and place orders on a cTrader demo account. Configure credentials in ~/.vibe-trading/ctrader.json.",
    ),
    TradingProfile(
        id="ctrader-live-sdk-readonly",
        label="cTrader Live (read-only)",
        connector="ctrader",
        transport="broker_sdk",
        environment="live",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={},
        notes="Read-only access to a cTrader live account. Configure credentials in ~/.vibe-trading/ctrader.json.",
    ),
    TradingProfile(
        id="ctrader-live-trade",
        label="cTrader Live (trading — mandate required)",
        connector="ctrader",
        transport="broker_sdk",
        environment="live",
        capabilities=READ_CAPABILITIES + ("orders.place.requires_mandate",),
        readonly=False,
        config={},
        notes="Read and place orders on a cTrader live account. Live orders require an authorised mandate. Configure credentials in ~/.vibe-trading/ctrader.json.",
    ),
)
