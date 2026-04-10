"""HTTP fetchers for paper-trade truth sources.

Called by cvd_5min_bot.resolve_paper_outcome after every market cycle to
determine the true outcome of a 5-min BTC Up/Down market. Polymarket Gamma
is the primary source (Chainlink-derived internally). Binance spot is an
independent cross-check. Both functions are pure, stateless, and safe to
call from any thread.
"""
from __future__ import annotations

import requests

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com/markets"


def fetch_binance_spot_price(timestamp: int, timeout_sec: float = 5.0) -> float | None:
    """Fetch BTC/USDT spot close price from the 1-minute candle containing `timestamp`.

    Uses the public Binance Klines REST endpoint (no auth, no rate limit
    issues under ~1 req/sec). Spot is chosen over perp because Polymarket
    settles via Chainlink BTC/USD, which tracks spot, not perp-futures.

    Args:
        timestamp: unix seconds
        timeout_sec: per-request timeout

    Returns:
        Close price as float, or None on any error (timeout, HTTP !=200,
        empty response, JSON parse failure, unexpected schema).
    """
    try:
        params = {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "startTime": int(timestamp) * 1000,
            "limit": 1,
        }
        resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=timeout_sec)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
        # Klines row: [openTime, open, high, low, close, volume, closeTime, ...]
        close_str = data[0][4]
        return float(close_str)
    except (requests.RequestException, ValueError, IndexError, TypeError):
        return None


def fetch_polymarket_resolution(condition_id: str, timeout_sec: float = 5.0) -> str | None:
    """Fetch the resolved outcome for a Polymarket binary market.

    Calls the Gamma API `/markets` endpoint filtered by conditionId. Reads
    the `resolvedPrice` field:
      - > 0.5 → "UP"   (the first outcome — YES / Up — won)
      - < 0.5 → "DOWN" (the second outcome — NO / Down — won)
      - null or missing → "PENDING" (market not yet settled)

    Polymarket settles binary markets via Chainlink internally, so this is
    the authoritative truth source without needing on-chain Chainlink access.

    Args:
        condition_id: the 0x-prefixed Polymarket conditionId

    Returns:
        "UP", "DOWN", "PENDING", or None on HTTP error / transport failure.
        None vs PENDING distinction: PENDING is retryable (market will
        resolve later); None indicates a fetch failure that should not be
        treated as market state.
    """
    try:
        params = {"condition_ids": condition_id}
        resp = requests.get(POLYMARKET_GAMMA_URL, params=params, timeout=timeout_sec)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
        market = data[0]
        resolved = market.get("resolvedPrice")
        if resolved is None:
            return "PENDING"
        resolved_float = float(resolved)
        return "UP" if resolved_float > 0.5 else "DOWN"
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
