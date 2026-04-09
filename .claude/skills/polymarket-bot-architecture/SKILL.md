---
name: polymarket-bot-architecture
description: "Reference architecture for the Polymarket BTC 5-minute prediction bot (MoonDev/joicodev pattern). Use whenever building, modifying, or debugging any feed, strategy, engine, or execution component. Also use when someone asks 'how does the bot work', 'what APIs do we use', 'what's the trading flow', or references any part of the system architecture. Use this even for small changes to ensure consistency with the proven architecture."
---

# Polymarket Bot Architecture Reference

This bot replicates the proven MoonDev/joicodev architecture for trading Polymarket BTC 5-minute Up/Down prediction markets. Every component should follow this architecture exactly -- it's battle-tested and documented at https://mintlify.com/joicodev/polymarket-bot.

## Why this matters

This isn't a greenfield design. The architecture below comes from a working, profitable bot. Deviating from it means introducing untested patterns into a system that handles real money. When in doubt, match the reference implementation.

## System Overview

```
Chainlink WS (1s tick) ──┐
Vatic API (strike price) ─┼──> Engine (index) ──> EV Decision ──> CLOB Order
Gamma API (market IDs)   ─┤        │
CLOB API (market price)  ─┘        │
Hyperliquid (CVD ticks)  ──────────┘
```

## The 4 (+1) Data Sources

### 1. Chainlink WebSocket -- Live BTC/USD Price
- **Endpoint**: `wss://ws-live-data.polymarket.com`
- **Frequency**: 1 tick per second
- **Subscribe**: `{"action":"subscribe","subscriptions":[{"topic":"crypto_prices_chainlink","type":"*","filters":"{\"symbol\":\"btc/usd\"}"}]}`
- **Response**: `{"topic":"crypto_prices_chainlink","payload":{"symbol":"btc/usd","value":"64232.02","timestamp":1709571234567}}`
- **Guards**: NaN/zero rejection + 10% spike filter vs last valid price
- **Reconnect**: 3s delay, repeat while running

### 2. Vatic Trading API -- Strike Price
- **Endpoint**: `https://api.vatic.trading/api/v1/targets/timestamp?asset=btc&type=5min&timestamp={epoch}`
- **Epoch**: Aligned to 5-min boundary: `floor(now_sec / 300) * 300`
- **Response**: `{"target_price": 64355.45, "timestamp": 1709571300, "asset": "btc", "interval": "5min"}`
- **Field fallbacks**: Try `target_price`, then `target`, then `price`
- **Cache**: Per-epoch. Fetch once per 300s interval, not every second.
- **Timing**: Updates 10-30s before interval starts. Fetching too early returns previous interval's strike.

### 3. Polymarket Gamma API -- Market Discovery
- **Base URL**: `https://gamma-api.polymarket.com`
- **Endpoint**: `/events?slug={slug}`
- **Slug pattern**: `btc-updown-5m-{epochTimestamp}` (e.g., `btc-updown-5m-1709571300`)
- **Fallback slugs**: Try primary epoch, then ±300s offsets for clock skew
- **Rate limit**: 1 request per second (throttled fetch)
- **Extract**: `conditionId`, `upTokenId`, `downTokenId` from market response
  - Parse `clobTokenIds` and `outcomes` arrays
  - Map "Up" and "Down" to their respective token IDs

### 4. Polymarket CLOB API -- Market Price
- **Base URL**: `https://clob.polymarket.com`
- **Endpoint**: `/price?token_id={tokenId}&side=BUY`
- **Response**: `{"price": "0.1523", "token_id": "0x1234..."}`
- **`price` = implied probability of "Up" outcome**
- **Guard**: Reject if price <= 0 or price >= 1 or not finite
- **Polling**: Every 5 seconds (configurable)
- **Why /price not /book**: Order book can contain stale orders. /price returns best executable price.

### 5. Hyperliquid -- CVD (Cumulative Volume Delta)
- **Source**: Hyperliquid tick data (each individual BTC transaction)
- **CVD calculation**: For each tick, if trade at Ask → buy volume; if at Bid → sell volume. CVD = sum(buy) - sum(sell)
- **Signal**: Rising CVD = aggressive buyers (bullish). Falling CVD = aggressive sellers (bearish).
- **Why CVD**: Reveals order flow pressure that OHLC candles hide.

## EV Calculation

The core decision formula compares model probability `p` (from CVD/volatility model) against market probability `q` (from CLOB):

```
EV(YES) = p / q - 1
EV(NO)  = (1 - p) / (1 - q) - 1
Margin  = |p - q| / max(p, 1 - p)
Edge    = p - q
bestSide = YES if evYes > evNo else NO
```

**Guard**: If q <= 0 or q >= 1 or p <= 0 or p >= 1, return null (no trade).

**Extreme EV (> 5.0) warning signs**: stale market prices, low liquidity, oracle delays, model miscalibration. Always verify market conditions.

## 300-Second Trading Cycle

| Phase | Time | Action |
|-------|------|--------|
| Start | T=0s | Fetch strike price (cached per epoch) |
| Monitor | T=1-295s | BTC tick/sec, EWMA volatility, CVD, CLOB poll every 5s |
| Decision | T=296s | Calculate final EV. Trade if favorable + risk filters pass |
| Close | T=300s | Record outcome, update Platt calibration, update drawdown tracker, persist to history.json |

**Critical**: Near expiry (T=296-300s), disable momentum adjustments and use pure Black-Scholes only.

## File Structure (Reference)

```
src/
├── feeds/
│   ├── chainlink.py    # WebSocket feed + validation + reconnection
│   ├── vatic.py        # Strike price fetch + per-epoch cache
│   └── polymarket.py   # Gamma discovery + CLOB price + EV calculation
├── core/
│   ├── engine.py       # Main 300s loop coordinating all feeds
│   ├── models.py       # Data classes (Tick, Strike, Market, EV)
│   └── safety.py       # Kill switch, circuit breakers
└── history.json        # Persistent results tracking
```

## Quick Reference Card

| Item | Value |
|------|-------|
| Chainlink WS | `wss://ws-live-data.polymarket.com` |
| Vatic API | `https://api.vatic.trading/api/v1/targets/timestamp` |
| Gamma API | `https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{epoch}` |
| CLOB API | `https://clob.polymarket.com/price?token_id={id}&side=BUY` |
| Epoch alignment | `floor(now_sec / 300) * 300` |
| Spike threshold | 10% |
| Reconnect delay | 3 seconds |
| CLOB poll interval | 5 seconds |
| Gamma rate limit | 1 req/sec |
| Decision time | T=296s |
| EV formula | `p/q - 1` (YES), `(1-p)/(1-q) - 1` (NO) |
