"""Instrument classification for cTrader symbols."""

from __future__ import annotations

_FOREX_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF",
    "NOK", "SEK", "DKK", "SGD", "HKD", "MXN", "ZAR", "TRY",
    "PLN", "CZK", "HUF", "RUB",
}

_INDICES = {
    "US30", "US500", "US100", "NAS100", "SPX500", "DOW30",
    "UK100", "GER40", "GER30", "FRA40", "ESP35", "JPN225",
    "AUS200", "HKG50", "CHINA50", "FTSE100", "DAX40",
}

_COMMODITIES = {
    "XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD",
    "USOIL", "UKOIL", "NGAS", "COPPER",
    "COCOA", "COFFEE", "SUGAR", "WHEAT", "CORN", "SOYBEAN",
}

_CRYPTO_BASES = {
    "BTC", "ETH", "XRP", "LTC", "BCH", "ADA", "DOT", "SOL",
    "BNB", "DOGE", "MATIC", "LINK", "AVAX", "UNI", "AAVE",
}


def classify_symbol(raw_symbol: str) -> tuple[str, str]:
    """Return (instrument_type, asset_class) for a cTrader symbol string.

    Returns:
        Tuple of (instrument_type, asset_class) strings used by the trading
        service's order classifier. Falls back to ("equity", "us_equity")
        when the symbol cannot be positively identified.
    """
    symbol = raw_symbol.upper().strip()
    # Strip common broker suffixes (.ecn, .pro, +, m, etc.)
    for suffix in (".ECN", ".PRO", ".RAW", ".ZERO", ".STANDARD", ".", "+"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break

    if symbol in _COMMODITIES or symbol.startswith("XAU") or symbol.startswith("XAG"):
        return "commodity", "commodity"

    if symbol in _INDICES:
        return "index", "index"

    for base in _CRYPTO_BASES:
        if symbol.startswith(base) and len(symbol) > len(base):
            return "crypto", "crypto"

    if len(symbol) == 6:
        base, quote = symbol[:3], symbol[3:]
        if base in _FOREX_CURRENCIES and quote in _FOREX_CURRENCIES:
            return "forex", "forex"

    return "equity", "us_equity"
