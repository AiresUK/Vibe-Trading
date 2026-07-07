#!/usr/bin/env python3
"""One-time setup: fetch cTrader symbol IDs and save to disk.

Run this ONCE before starting the trading bot.  It uses a direct
one-shot TCP connection with a generous 5-minute timeout — long enough
even for slow Pepperstone demo servers.  After it completes, the main
bot loads symbol IDs from disk and never hits the API again.

Usage:
    cd ~/Vibe-Trading
    venv/bin/python agent/scripts/fetch_symbol_ids.py
"""
from __future__ import annotations

import json
import queue as _queue
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap path so we can import from the agent package
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "agent"))

from src.config.paths import get_runtime_root  # noqa: E402
from src.trading.connectors.ctrader.sdk import load_config  # noqa: E402

TIMEOUT_S = 300  # 5 minutes — more than enough even for a slow demo server
TARGET_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]


def fetch_symbol_ids() -> dict[str, int]:
    """Open a one-shot authenticated TCP connection and return {NAME: symbolId}."""
    try:
        import ctrader_open_api  # noqa: F401
    except ImportError:
        print("[ERROR] ctrader-open-api not installed. Run: pip install ctrader-open-api")
        sys.exit(1)

    from ctrader_open_api import Client, TcpProtocol, EndPoints
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAApplicationAuthReq,
        ProtoOAApplicationAuthRes,
        ProtoOAAccountAuthReq,
        ProtoOAAccountAuthRes,
        ProtoOASymbolsListReq,
        ProtoOASymbolsListRes,
        ProtoOAErrorRes,
    )
    from twisted.internet import reactor as _reactor

    config = load_config()
    result_q: _queue.Queue = _queue.Queue()
    phase = ["connecting"]

    def on_message(client, message):
        try:
            pt = message.payloadType

            # Error response
            if pt == ProtoOAErrorRes().payloadType:
                err = ProtoOAErrorRes()
                err.ParseFromString(message.payload)
                result_q.put(RuntimeError(f"cTrader error {err.errorCode}: {err.description}"))
                try: client.stopService()
                except Exception: pass
                return

            if phase[0] == "app_auth":
                if pt == ProtoOAApplicationAuthRes().payloadType:
                    phase[0] = "account_auth"
                    req = ProtoOAAccountAuthReq()
                    req.ctidTraderAccountId = config.account_id
                    req.accessToken = config.access_token
                    client.send(req)

            elif phase[0] == "account_auth":
                if pt == ProtoOAAccountAuthRes().payloadType:
                    phase[0] = "symbols"
                    print(f"[OK] Authenticated — fetching symbol list (may take up to {TIMEOUT_S}s)...")
                    req = ProtoOASymbolsListReq()
                    req.ctidTraderAccountId = config.account_id
                    client.send(req)

            elif phase[0] == "symbols":
                if pt == ProtoOASymbolsListRes().payloadType:
                    res = ProtoOASymbolsListRes()
                    res.ParseFromString(message.payload)
                    mapping = {s.symbolName.strip().upper(): s.symbolId for s in res.symbol}
                    result_q.put(mapping)
                    try: client.stopService()
                    except Exception: pass

        except Exception as exc:
            result_q.put(exc)

    def on_connected(client):
        phase[0] = "app_auth"
        req = ProtoOAApplicationAuthReq()
        req.clientId = config.client_id
        req.clientSecret = config.client_secret
        client.send(req)

    def on_disconnected(client, reason=None):
        if result_q.empty():
            result_q.put(RuntimeError("Disconnected before symbol list arrived"))

    host = (
        EndPoints.PROTOBUF_DEMO_HOST if config.environment == "demo"
        else EndPoints.PROTOBUF_LIVE_HOST
    )
    ct_client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
    ct_client.setConnectedCallback(on_connected)
    ct_client.setDisconnectedCallback(on_disconnected)
    ct_client.setMessageReceivedCallback(on_message)

    # Start reactor in background
    reactor_ready = threading.Event()
    def _run_reactor():
        _reactor.callLater(0, reactor_ready.set)
        _reactor.run(installSignalHandlers=False)
    t = threading.Thread(target=_run_reactor, daemon=True)
    t.start()
    reactor_ready.wait(timeout=10)

    _reactor.callFromThread(ct_client.startService)

    try:
        value = result_q.get(timeout=TIMEOUT_S)
    except _queue.Empty:
        print(f"[ERROR] Symbol list did not arrive within {TIMEOUT_S}s.")
        print("       The Pepperstone demo server may be overloaded — try again later.")
        sys.exit(1)

    if isinstance(value, Exception):
        print(f"[ERROR] {value}")
        sys.exit(1)

    return value


def main():
    print("=" * 60)
    print("cTrader Symbol ID Setup")
    print("=" * 60)

    mapping = fetch_symbol_ids()
    total = len(mapping)
    print(f"[OK] Received {total} symbols")

    # Save full cache to disk
    cache_path = get_runtime_root() / "ctrader_symbols.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    env_key = "demo"  # matches config.environment used by the bot
    cache = {env_key: mapping}
    cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    print(f"[OK] Saved to {cache_path}")

    # Print target symbols
    print()
    print("Symbol IDs for your watchlist:")
    missing = []
    for sym in TARGET_SYMBOLS:
        sid = mapping.get(sym)
        if sid:
            print(f"  {sym}: {sid}")
        else:
            print(f"  {sym}: NOT FOUND")
            missing.append(sym)

    if missing:
        print()
        print(f"[WARN] Missing: {missing}")
        print("       These symbols may not be available on this account.")
    else:
        print()
        print("[DONE] All target symbols found. The bot will use cached IDs from now on.")
        print("       Run venv/bin/vibe-trading to start trading.")


if __name__ == "__main__":
    main()
