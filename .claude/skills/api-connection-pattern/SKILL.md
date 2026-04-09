---
name: api-connection-pattern
description: "Patterns for connecting to Polymarket bot data sources with proper reconnection, caching, rate limiting, and error handling. Use when creating or modifying any feed module (chainlink.py, vatic.py, polymarket.py), when debugging connection issues, when seeing 'connection refused', 'timeout', 'rate limited', or when implementing WebSocket or REST API clients for this bot. Also use when writing tests for feed modules."
---

# API Connection Patterns

Each of the bot's 4 data sources has battle-tested connection patterns from the reference implementation. These patterns exist because naive implementations fail in production -- connections drop, APIs rate-limit you, caches go stale, and bad data slips through. Follow these patterns exactly.

## Pattern 1: Chainlink WebSocket -- Persistent Stream

The WebSocket feed is the bot's heartbeat. If it goes down, the bot is blind.

### Connection lifecycle
```
start() → _connect() → on('open') → subscribe → on('message') → validate → emit tick
                                                                     ↓
                                              on('close') → wait 3s → _connect() (loop)
```

### Key rules
- **Reconnect delay**: 3 seconds (configurable). Simple timeout, not exponential backoff -- the feed rarely stays down long.
- **Running flag**: `_running` boolean controls the reconnect loop. `stop()` sets it to False and cleans up.
- **Subscription on open**: Send the subscribe message every time the connection opens, not just the first time.
- **Validate every tick**: Two guards before accepting a price:
  1. **NaN/zero guard**: Reject if price is not finite or <= 0
  2. **Spike filter**: Reject if `abs(price / last_valid_price - 1) > 0.10` (10% threshold)
- **Track last valid price**: Only update after a tick passes both guards. This prevents a rejected spike from shifting the baseline.

### Python pattern
```python
import asyncio
import websockets
import json

class ChainlinkFeed:
    WS_URL = "wss://ws-live-data.polymarket.com"
    SUBSCRIBE_MSG = json.dumps({
        "action": "subscribe",
        "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*",
                           "filters": '{"symbol": "btc/usd"}'}]
    })
    RECONNECT_DELAY = 3.0
    SPIKE_THRESHOLD = 0.10

    def __init__(self, on_tick, on_status):
        self._on_tick = on_tick
        self._on_status = on_status
        self._running = False
        self._last_valid_price = None

    async def start(self):
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception:
                pass
            if self._running:
                self._on_status("reconnecting")
                await asyncio.sleep(self.RECONNECT_DELAY)

    async def _connect(self):
        async with websockets.connect(self.WS_URL) as ws:
            self._on_status("connected")
            await ws.send(self.SUBSCRIBE_MSG)
            async for msg in ws:
                data = json.loads(msg)
                if data.get("topic") != "crypto_prices_chainlink":
                    continue
                payload = data.get("payload", {})
                if payload.get("symbol") != "btc/usd":
                    continue
                price = float(payload["value"])
                if not self._validate(price):
                    continue
                self._on_tick({"timestamp": asyncio.get_event_loop().time(), "price": price})

    def _validate(self, price: float) -> bool:
        if not (price > 0 and price == price):  # catches NaN and <= 0
            return False
        if self._last_valid_price is not None:
            if abs(price / self._last_valid_price - 1) > self.SPIKE_THRESHOLD:
                return False
        self._last_valid_price = price
        return True

    def stop(self):
        self._running = False
```

## Pattern 2: Vatic API -- Per-Epoch Cache

Strike prices change once per 5-minute interval. Fetching every second wastes API calls and risks rate limits.

### Key rules
- **Epoch alignment**: `epoch = (now_sec // 300) * 300`
- **Cache key**: The epoch timestamp. Fetch only when epoch changes.
- **Field fallbacks**: Try `target_price`, then `target`, then `price` from JSON response.
- **Error handling**: If fetch fails, keep using the last cached value. Log a warning but don't crash.
- **Timing awareness**: Vatic updates 10-30s before interval start. Early fetches may return stale data.

### Python pattern
```python
import aiohttp

VATIC_BASE = "https://api.vatic.trading/api/v1/targets/timestamp"

def get_current_epoch() -> int:
    import time
    now_sec = int(time.time())
    return (now_sec // 300) * 300

async def fetch_strike_price(epoch: int) -> dict:
    url = f"{VATIC_BASE}?asset=btc&type=5min&timestamp={epoch}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
            strike = data.get("target_price") or data.get("target") or data.get("price")
            return {"strike_price": strike, "raw": data}

# Cache usage in engine loop:
# cached = {"epoch": None, "price": None}
# if current_epoch != cached["epoch"]:
#     result = await fetch_strike_price(current_epoch)
#     cached = {"epoch": current_epoch, "price": result["strike_price"]}
```

## Pattern 3: Gamma API -- Throttled Discovery with Fallbacks

Market slugs can be off by one interval due to clock skew. The Gamma API will rate-limit aggressive callers.

### Key rules
- **Rate limit**: Max 1 request per second. Enforce with a throttle function.
- **Slug pattern**: `btc-updown-5m-{epoch}` where epoch is the 5-minute boundary.
- **Fallback offsets**: If primary slug returns nothing, try epoch ± 300s.
- **Extract token IDs**: Parse `clobTokenIds` and `outcomes` JSON arrays. Map "Up"/"Down" to their token IDs.
- **Validation**: Require both upIndex and downIndex found, and at least 2 token IDs.

### Python pattern
```python
import asyncio
import aiohttp
import json

GAMMA_BASE = "https://gamma-api.polymarket.com"
SLUG_PREFIX = "btc-updown-5m-"
FALLBACK_OFFSETS = [-300, 300]
MIN_INTERVAL = 1.0  # seconds between requests

_last_request_time = 0.0

async def _throttled_get(session, url):
    global _last_request_time
    now = asyncio.get_event_loop().time()
    elapsed = now - _last_request_time
    if elapsed < MIN_INTERVAL:
        await asyncio.sleep(MIN_INTERVAL - elapsed)
    _last_request_time = asyncio.get_event_loop().time()
    return await session.get(url)

async def discover_market(epoch: int) -> dict:
    slugs = [SLUG_PREFIX + str(epoch)]
    slugs += [SLUG_PREFIX + str(epoch + off) for off in FALLBACK_OFFSETS]

    async with aiohttp.ClientSession() as session:
        for slug in slugs:
            resp = await _throttled_get(session, f"{GAMMA_BASE}/events?slug={slug}")
            if resp.status != 200:
                continue
            data = await resp.json()
            if not data:
                continue
            event = data[0]
            market = (event.get("markets") or [{}])[0]
            if not market:
                continue

            token_ids = json.loads(market.get("clobTokenIds", "[]"))
            outcomes = json.loads(market.get("outcomes", "[]"))
            up_idx = outcomes.index("Up") if "Up" in outcomes else -1
            down_idx = outcomes.index("Down") if "Down" in outcomes else -1

            if up_idx == -1 or down_idx == -1 or len(token_ids) < 2:
                continue

            return {
                "found": True,
                "condition_id": market.get("conditionId"),
                "up_token_id": token_ids[up_idx],
                "down_token_id": token_ids[down_idx],
                "slug": slug,
            }
    return {"found": False}
```

## Pattern 4: CLOB API -- Bounded Price Polling

The CLOB price is the market's implied probability. Bad values mean bad EV calculations.

### Key rules
- **Poll interval**: Every 5 seconds (configurable). Not every second -- reduces load and the price doesn't change that fast.
- **Price bounds**: Only accept `0 < price < 1`. Reject 0, 1, negative, NaN, Infinity.
- **Use /price not /book**: The order book can have stale orders. /price gives the best executable price.
- **Cache with timestamp**: Track `last_fetch_sec` to enforce the polling interval.
- **Null on failure**: Return None on error, don't crash. The engine decides whether to skip the trade.

### Python pattern
```python
async def get_market_price(session, token_id: str) -> float | None:
    try:
        url = f"https://clob.polymarket.com/price?token_id={token_id}&side=BUY"
        resp = await _throttled_get(session, url)
        if resp.status != 200:
            return None
        data = await resp.json()
        price = float(data["price"])
        if not (0 < price < 1):
            return None
        return price
    except Exception:
        return None
```

## Common Mistakes to Avoid

| Mistake | Why it breaks | Correct pattern |
|---------|--------------|----------------|
| No reconnection on WS | Bot goes blind silently | Always reconnect with delay loop |
| Fetching Vatic every second | 300 wasted calls per interval | Cache per epoch |
| No Gamma rate limit | API returns 429, bot can't find markets | 1 req/sec throttle |
| Accepting price = 0 or 1 from CLOB | Division by zero in EV formula | Strict bounds check |
| No spike filter on Chainlink | Oracle glitch → wrong trade | 10% threshold filter |
| Exponential backoff on Chainlink | Unnecessary complexity for a feed that recovers fast | Simple 3s fixed delay |
