"""MT5 instrument classification helpers.

MT5 brokers expose diverse symbol naming conventions (EURUSD, XAUUSD, US30,
NAS100, BTCUSD, etc.). This module maps MT5 symbols to the mandate gate's
InstrumentType / AssetClass taxonomy on a best-effort basis.
"""

from __future__ import annotations


_FOREX_CURRENCIES = {
    "EUR", "GBP", "USD", "JPY", "CHF", "AUD", "NZD", "CAD",
    "NOK", "SEK", "DKK", "HKD", "SGD", "CNH", "MXN", "ZAR", "TRY",
}

_CRYPTO_BASES = {
    "BTC", "ETH", "LTC", "XRP", "SOL", "ADA", "DOT", "LINK",
    "AVAX", "MATIC", "DOGE", "BNB", "SHIB", "UNI",
}

_INDEX_SYMBOLS = {
    "US30", "US500", "NAS100", "UK100", "GER40", "GER30", "FRA40",
    "JPN225", "AUS200", "HK50", "ESP35", "STOXX50",
    "SP500", "DJ30", "NASDAQ",
}


def classify_symbol(symbol: str) -> tuple[str, str]:
    """Return (InstrumentType, AssetClass) strings for an MT5 symbol.

    Returns ``("equity", "us_equity")`` as the safe fallback when the symbol
    cannot be classified — this is fail-closed for the mandate gate.
    """
    s = str(symbol or "").strip().upper()

    # Strip broker suffixes (e.g. EURUSDm, EURUSD.pro, XAUUSD+)
    for suffix in (".PRO", ".ECN", ".RAW", "M", "+", "-", "."):
        if s.endswith(suffix):
            s = s[: -len(suffix)]

    # Crypto: base in known list + quote is USD/USDT/BTC/ETH
    for base in _CRYPTO_BASES:
        if s.startswith(base) and (
            s.endswith(("USD", "USDT", "BTC", "ETH")) or len(s) <= len(base) + 4
        ):
            return ("crypto", "crypto")

    # Indices
    if s in _INDEX_SYMBOLS or any(s.startswith(idx) for idx in ("US", "UK", "GER", "NAS", "FRA", "JPN", "AUS")):
        if len(s) <= 8:  # avoid matching equity tickers
            return ("equity", "us_equity")  # closest mandate class for indices

    # Forex: both halves are known currencies
    if len(s) == 6:
        base, quote = s[:3], s[3:]
        if base in _FOREX_CURRENCIES and quote in _FOREX_CURRENCIES:
            return ("forex", "forex")

    # Commodities: XAU, XAG, OIL, etc.
    if s.startswith(("XAU", "XAG", "XPT", "XPD", "OIL", "BRENT", "WTI", "NGAS")):
        return ("commodity", "commodity")

    # Default fallback
    return ("equity", "us_equity")
