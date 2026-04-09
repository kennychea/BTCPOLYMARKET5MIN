---
name: trading-cycle-implementation
description: "How to implement the 300-second trading cycle for the Polymarket BTC 5-min bot. Use when building the main engine loop, implementing the trading timeline, working on trade decision logic, or debugging why trades fire at the wrong time. Also use when you see issues like 'trade placed after interval close', 'stale strike price', 'missing market data', or when implementing history.json persistence. Triggers on any work touching engine.py or the main bot loop."
---

# 300-Second Trading Cycle

The bot's core is a loop that runs once per 5-minute (300-second) interval. Each cycle has 4 distinct phases with strict timing boundaries. Getting the timing wrong means placing trades on expired markets or using stale data -- both cost real money.

## The Cycle at a Glance

```
T=0s          T=1-295s              T=296s           T=300s
┌─────────┐   ┌──────────────────┐  ┌─────────────┐  ┌──────────────┐
│ STARTUP │   │    MONITORING    │  │  DECISION   │  │    CLOSE     │
│         │   │                  │  │             │  │              │
│ • Strike│   │ • BTC tick/sec   │  │ • Final EV  │  │ • Record     │
│ • Market│   │ • EWMA volatility│  │ • Risk check│  │ • Calibrate  │
│   IDs   │   │ • CVD accumulate │  │ • Trade or  │  │ • Drawdown   │
│         │   │ • CLOB poll /5s  │  │   abstain   │  │ • history.json│
└─────────┘   └──────────────────┘  └─────────────┘  └──────────────┘
```

## Phase 1: Startup (T=0s)

At the beginning of each 5-minute interval:

1. **Calculate epoch**: `epoch = (now_sec // 300) * 300`
2. **Fetch strike price** (cached -- only if epoch changed):
   - Call Vatic API with the epoch
   - Cache result: `{epoch, price, raw_data}`
   - If fetch fails, reuse last cache. Log warning.
3. **Discover market** (cached -- only if epoch changed):
   - Call Gamma API with slug `btc-updown-5m-{epoch}`
   - Try fallback offsets ±300s if primary fails
   - Extract `upTokenId` and `downTokenId`
   - If no market found, skip this interval entirely

**Why cache per epoch**: Strike price and market IDs don't change within an interval. Fetching them 300 times wastes API calls and risks rate limits.

## Phase 2: Monitoring (T=1s to T=295s)

The engine runs a tight loop, executing these tasks every second:

### Every 1 second:
- **Receive BTC price tick** from Chainlink WebSocket
- **Update EWMA volatility** (exponentially weighted moving average of price changes)
- **Accumulate CVD** from Hyperliquid tick data (buy volume - sell volume)

### Every 5 seconds:
- **Poll CLOB price** (`q` = market's implied probability of "Up")
- Cache with timestamp: only re-fetch if 5s have elapsed

### Calculate model probability `p`:
The model combines:
- Current BTC price vs strike price (directional signal)
- CVD trend (order flow pressure)
- EWMA volatility (confidence adjustment)

### Implementation pattern:
```python
async def monitoring_loop(engine):
    while engine.seconds_elapsed < 295:
        # BTC tick comes via WebSocket callback (async)
        # Every 5s, poll CLOB
        now_sec = int(time.time())
        if (now_sec - engine.last_clob_poll) >= engine.clob_poll_interval:
            price = await get_market_price(session, engine.market.up_token_id)
            if price is not None:
                engine.q_market = price
                engine.last_clob_poll = now_sec

        # CVD and volatility update on each tick (via callback)
        await asyncio.sleep(1)
```

## Phase 3: Decision (T=296s)

This is the critical moment. The bot has 4 seconds to decide and execute.

### Decision logic:
1. **Calculate final EV**:
   ```python
   def calculate_ev(p, q):
       if q <= 0 or q >= 1 or p <= 0 or p >= 1:
           return None
       ev_yes = p / q - 1
       ev_no = (1 - p) / (1 - q) - 1
       margin = abs(p - q) / max(p, 1 - p)
       best_side = "YES" if ev_yes > ev_no else "NO"
       best_ev = max(ev_yes, ev_no)
       return {"ev_yes": ev_yes, "ev_no": ev_no, "margin": margin,
               "best_side": best_side, "best_ev": best_ev, "edge": p - q}
   ```

2. **Check risk filters**:
   - Is EV above minimum threshold?
   - Is margin sufficient?
   - Is drawdown within limits?
   - Is there enough data (did we get enough ticks)?

3. **Disable momentum adjustments**: Near expiry, CVD momentum becomes unreliable. Use pure model probability (Black-Scholes style) only.

4. **Execute or abstain**:
   - If all filters pass: place order via CLOB API using the appropriate token ID
   - If any filter fails: log the abstention reason and skip

### Critical timing rule:
**Never trade after T=300s.** The interval is closed. An order placed at T=301s targets the NEXT interval with stale data from the current one. This is the single most expensive timing bug possible.

## Phase 4: Close (T=300s)

After the interval expires:

1. **Record outcome**: Did BTC finish Up or Down vs the strike?
2. **Update Platt calibration**: Adjust the model's probability estimates based on actual outcomes
3. **Update drawdown tracker**: Track cumulative P&L for risk management
4. **Persist to history.json**: Append the interval result with all metadata

### history.json entry structure:
```json
{
  "epoch": 1709571300,
  "strike_price": 64355.45,
  "final_btc_price": 64412.30,
  "outcome": "Up",
  "prediction": "YES",
  "ev": 2.35,
  "market_price_q": 0.52,
  "model_prob_p": 0.78,
  "traded": true,
  "pnl": 0.48
}
```

## Complete Engine Loop

```python
async def run_engine():
    while True:
        epoch = get_current_epoch()
        seconds_in_interval = int(time.time()) - epoch

        # Wait for next interval if we're mid-cycle
        if seconds_in_interval > 5:
            next_epoch = epoch + 300
            await asyncio.sleep(next_epoch - time.time())
            epoch = next_epoch

        # Phase 1: Startup
        strike = await fetch_strike_cached(epoch)
        market = await discover_market_cached(epoch)
        if not market["found"] or strike is None:
            await asyncio.sleep(300 - (int(time.time()) - epoch))
            continue

        # Phase 2: Monitoring (T=1 to T=295)
        await monitoring_loop(...)

        # Phase 3: Decision (T=296)
        ev = calculate_ev(model_p, market_q)
        if ev and passes_risk_filters(ev):
            await place_trade(market, ev["best_side"])

        # Phase 4: Close (T=300)
        await asyncio.sleep(300 - (int(time.time()) - epoch))
        outcome = determine_outcome(final_price, strike)
        persist_to_history(epoch, outcome, ev, ...)
```

## Common Mistakes

| Mistake | Impact | Prevention |
|---------|--------|------------|
| Trading after T=300s | Order hits wrong interval with stale data | Check `time.time() - epoch < 300` before every order |
| Not caching strike per epoch | 300 redundant Vatic API calls per interval | Cache key = epoch timestamp |
| Using CVD momentum near expiry | Unreliable signal in last 4 seconds | Disable at T=296s, use pure model only |
| No abstention on missing data | Trading blind (no strike, no market, no ticks) | Require all data sources present before T=296 decision |
| Blocking the event loop | Missed ticks, stale data, timing drift | Use async/await for ALL I/O, never block |
| Forgetting to persist history | Can't track performance or calibrate model | Always append to history.json at T=300 |
| Starting mid-interval | First trade uses partial data | Wait for next clean interval boundary on startup |
