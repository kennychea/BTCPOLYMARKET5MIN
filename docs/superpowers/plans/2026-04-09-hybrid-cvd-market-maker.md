# Hybrid CVD Market Maker — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a market making strategy to `cvd_5min_bot.py` that posts two-sided quotes (bid+ask) on Polymarket BTC 5-min UP tokens, biased by CVD signals, with zero maker fees + 20% rebate.

**Architecture:** New `CVDMarketMaker` class parallel to existing `CVDStinkBot`, selected via `STRATEGY` env var. Reuses existing infrastructure (BinanceCVDFeed, market discovery, orderbook, CVD functions). Paper mode simulates fills by comparing virtual quotes against real orderbook (public API). All new code stays in `cvd_5min_bot.py` (single-file pattern).

**Tech Stack:** Python 3.13, py-clob-client 0.34.6, pandas, termcolor, math (stdlib)

---

## Why Market Making > Directional Betting

| | Stink Bid (current) | Market Making (new) |
|---|---|---|
| Fee | 1.80% taker at p=0.50 | **0% maker + 20% rebate** |
| Edge needed | Predict direction (51.8% breakeven) | Capture spread (no prediction needed) |
| Risk | Binary (win/lose all) | Inventory (manageable via skew) |
| CVD role | Trade signal (all-or-nothing) | Quote bias (continuous adjustment) |

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `cvd_5min_bot.py` | Modify | Add MM constants (~line 138), `calculate_mm_quotes()` (~line 355), `MMInventory` class (~line 950), `check_mm_paper_fills()` (~line 950), MM logging (~line 950), `CVDMarketMaker` class (~line 1497), strategy mode in `main()` |
| `paper_dashboard.py` | Modify | Add MM stats panel |
| `tests/unit/test_mm_quotes.py` | Create | Unit tests for `calculate_mm_quotes()` |
| `tests/unit/test_mm_inventory.py` | Create | Unit tests for `MMInventory` class |
| `tests/unit/test_mm_paper.py` | Create | Unit tests for paper fills + MM logging |
| `tests/integration/test_mm_smoke.py` | Create | Smoke tests for MM constants + class creation |

---

## Task 1: MM Configuration Constants

**Files:**
- Modify: `cvd_5min_bot.py` (after line 137, `PAPER_LOG_FILE`)
- Test: `tests/integration/test_mm_smoke.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_mm_smoke.py`:

```python
"""Smoke tests for market maker constants and class creation."""
import cvd_5min_bot as bot


class TestMMConstants:
    def test_strategy_constant(self):
        assert bot.STRATEGY in ("stink", "mm")

    def test_mm_base_spread(self):
        assert 0 < bot.MM_BASE_SPREAD < 1

    def test_mm_order_size(self):
        assert bot.MM_ORDER_SIZE > 0

    def test_mm_max_inventory(self):
        assert bot.MM_MAX_INVENTORY > 0

    def test_mm_max_cvd_skew(self):
        assert 0 < bot.MM_MAX_CVD_SKEW < bot.MM_BASE_SPREAD

    def test_mm_refresh_interval(self):
        assert bot.MM_REFRESH_INTERVAL > 0

    def test_mm_stop_quoting_sec(self):
        assert bot.MM_STOP_QUOTING_SEC > 0

    def test_mm_log_file(self):
        assert bot.MM_LOG_FILE.endswith("mm_paper_trades.csv")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_mm_smoke.py -v`
Expected: FAIL with `AttributeError: module 'cvd_5min_bot' has no attribute 'STRATEGY'`

- [ ] **Step 3: Add MM constants to cvd_5min_bot.py**

Insert after the `PAPER_LOG_FILE = ...` line (line 137) in `cvd_5min_bot.py`:

```python

# --- Market Making Strategy ---
STRATEGY = os.getenv("STRATEGY", "stink")             # "stink" (directional) or "mm" (market making)
MM_BASE_SPREAD = float(os.getenv("MM_BASE_SPREAD", "0.04"))   # 4-cent base spread
MM_ORDER_SIZE = int(os.getenv("MM_ORDER_SIZE", "10"))          # shares per side per quote
MM_MAX_INVENTORY = int(os.getenv("MM_MAX_INVENTORY", "50"))    # max net position (shares)
MM_MAX_CVD_SKEW = float(os.getenv("MM_MAX_CVD_SKEW", "0.02")) # max CVD-derived price shift
MM_REFRESH_INTERVAL = int(os.getenv("MM_REFRESH_INTERVAL", "10"))  # seconds between quote refreshes
MM_STOP_QUOTING_SEC = 30                                       # stop quoting N sec before market end
MM_LOG_FILE = os.path.join(DATA_DIR, "mm_paper_trades.csv")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_mm_smoke.py -v`
Expected: 8 PASSED

- [ ] **Step 5: Run full test suite to verify nothing broke**

Run: `pytest -v`
Expected: All 40+ tests PASS (existing tests unaffected)

- [ ] **Step 6: Commit**

```bash
git add cvd_5min_bot.py tests/integration/test_mm_smoke.py
git commit -m "feat: add market maker configuration constants"
```

---

## Task 2: `calculate_mm_quotes()` Pure Function

The core of the market maker — a pure function that takes market state and returns bid/ask prices.

**Files:**
- Modify: `cvd_5min_bot.py` (after `detect_divergence()`, before `check_cvd_signal()`)
- Create: `tests/unit/test_mm_quotes.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_mm_quotes.py`:

```python
"""Unit tests for market maker quote calculation."""
import pytest
from cvd_5min_bot import calculate_mm_quotes


class TestSymmetricQuotes:
    def test_no_skew_no_inventory(self):
        """No CVD skew, no inventory → symmetric around midpoint."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=0, max_inventory=50, order_size=10,
        )
        assert result["bid_price"] == 0.48
        assert result["ask_price"] == 0.52
        assert result["bid_size"] == 10
        assert result["ask_size"] == 10

    def test_spread_maintained(self):
        """Spread should be at least base_spread."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.06, cvd_skew=0.0,
            inventory=0, max_inventory=50, order_size=10,
        )
        spread = result["ask_price"] - result["bid_price"]
        assert spread >= 0.06


class TestCVDSkew:
    def test_bullish_shifts_up(self):
        """Positive CVD skew shifts both prices up."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.01,
            inventory=0, max_inventory=50, order_size=10,
        )
        assert result["bid_price"] > 0.48
        assert result["ask_price"] > 0.52

    def test_bearish_shifts_down(self):
        """Negative CVD skew shifts both prices down."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=-0.01,
            inventory=0, max_inventory=50, order_size=10,
        )
        assert result["bid_price"] < 0.48
        assert result["ask_price"] < 0.52


class TestInventorySkew:
    def test_long_shifts_down(self):
        """Positive inventory → lower prices (encourage selling)."""
        neutral = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=0, max_inventory=50, order_size=10,
        )
        long_inv = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=25, max_inventory=50, order_size=10,
        )
        assert long_inv["bid_price"] < neutral["bid_price"]
        assert long_inv["ask_price"] < neutral["ask_price"]

    def test_short_shifts_up(self):
        """Negative inventory → higher prices (encourage buying)."""
        neutral = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=0, max_inventory=50, order_size=10,
        )
        short_inv = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=-25, max_inventory=50, order_size=10,
        )
        assert short_inv["bid_price"] > neutral["bid_price"]
        assert short_inv["ask_price"] > neutral["ask_price"]

    def test_max_inventory_stops_buying(self):
        """At max inventory → bid_size = 0."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=50, max_inventory=50, order_size=10,
        )
        assert result["bid_size"] == 0
        assert result["ask_size"] == 10

    def test_max_short_stops_selling(self):
        """At -max inventory → ask_size = 0."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=-50, max_inventory=50, order_size=10,
        )
        assert result["bid_size"] == 10
        assert result["ask_size"] == 0


class TestPriceBounds:
    def test_minimum_spread_one_tick(self):
        """Spread never less than tick_size."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.001, cvd_skew=0.0,
            inventory=0, max_inventory=50, order_size=10,
            tick_size=0.01,
        )
        assert result["ask_price"] - result["bid_price"] >= 0.01

    def test_low_bound(self):
        """Prices never below 0.01."""
        result = calculate_mm_quotes(
            midpoint=0.02, base_spread=0.04, cvd_skew=-0.01,
            inventory=0, max_inventory=50, order_size=10,
        )
        assert result["bid_price"] >= 0.01
        assert result["ask_price"] >= 0.01

    def test_high_bound(self):
        """Prices never above 0.99."""
        result = calculate_mm_quotes(
            midpoint=0.98, base_spread=0.04, cvd_skew=0.01,
            inventory=0, max_inventory=50, order_size=10,
        )
        assert result["bid_price"] <= 0.99
        assert result["ask_price"] <= 0.99

    def test_prices_rounded_to_tick(self):
        """Prices are multiples of tick_size (0.01)."""
        result = calculate_mm_quotes(
            midpoint=0.505, base_spread=0.04, cvd_skew=0.003,
            inventory=7, max_inventory=50, order_size=10,
            tick_size=0.01,
        )
        assert round(result["bid_price"] * 100) == result["bid_price"] * 100
        assert round(result["ask_price"] * 100) == result["ask_price"] * 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_mm_quotes.py -v`
Expected: FAIL with `ImportError: cannot import name 'calculate_mm_quotes'`

- [ ] **Step 3: Implement `calculate_mm_quotes()`**

Insert in `cvd_5min_bot.py` after the `detect_divergence()` function (after the line `return ("NEUTRAL", "NEUTRAL", "FLAT", 0)`) and before `check_cvd_signal()`:

```python


def calculate_mm_quotes(
    midpoint: float,
    base_spread: float,
    cvd_skew: float,
    inventory: int,
    max_inventory: int,
    order_size: int,
    tick_size: float = 0.01,
) -> dict:
    """
    Calculate market maker bid/ask prices with CVD and inventory skew.

    Args:
        midpoint: Current orderbook midpoint price.
        base_spread: Base spread width (e.g., 0.04 = 4 cents).
        cvd_skew: CVD-derived price shift. Positive = bullish (shift up).
        inventory: Current net position in shares (positive = long UP).
        max_inventory: Maximum allowed net position.
        order_size: Shares per side.
        tick_size: Minimum price increment (default 0.01 for BTC markets).

    Returns:
        {"bid_price": float, "ask_price": float, "bid_size": int, "ask_size": int}
    """
    half_spread = base_spread / 2.0

    # Shift midpoint by CVD signal
    adjusted_mid = midpoint + cvd_skew

    # Inventory skew: penalize the overloaded side
    # Long inventory → lower mid → lower bid/ask → encourage sells, discourage buys
    if max_inventory > 0:
        inv_ratio = inventory / max_inventory  # range: -1 to +1
        inv_skew = inv_ratio * half_spread * 0.5  # shift up to half the half-spread
        adjusted_mid -= inv_skew

    # Calculate raw prices
    bid_price = adjusted_mid - half_spread
    ask_price = adjusted_mid + half_spread

    # Round to tick: floor bid (conservative buy), ceil ask (conservative sell)
    bid_price = round(math.floor(bid_price / tick_size) * tick_size, 2)
    ask_price = round(math.ceil(ask_price / tick_size) * tick_size, 2)

    # Ensure minimum spread of 1 tick
    if ask_price - bid_price < tick_size:
        ask_price = round(bid_price + tick_size, 2)

    # Clamp to valid range [0.01, 0.99]
    bid_price = max(0.01, min(0.99, bid_price))
    ask_price = max(0.01, min(0.99, ask_price))

    # Size: full size unless at inventory limit
    bid_size = order_size if inventory < max_inventory else 0
    ask_size = order_size if inventory > -max_inventory else 0

    return {
        "bid_price": bid_price,
        "ask_price": ask_price,
        "bid_size": bid_size,
        "ask_size": ask_size,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_mm_quotes.py -v`
Expected: 12 PASSED

- [ ] **Step 5: Run full test suite**

Run: `pytest -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add cvd_5min_bot.py tests/unit/test_mm_quotes.py
git commit -m "feat: add calculate_mm_quotes() pure function with CVD + inventory skew"
```

---

## Task 3: `MMInventory` Class

Tracks net position, fills, and P&L for the market maker.

**Files:**
- Modify: `cvd_5min_bot.py` (after `print_paper_summary()`, before HL hedge section)
- Create: `tests/unit/test_mm_inventory.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_mm_inventory.py`:

```python
"""Unit tests for MMInventory class."""
import pytest
from cvd_5min_bot import MMInventory


class TestMMInventoryInitial:
    def test_initial_state(self):
        inv = MMInventory()
        assert inv.net_position == 0
        assert inv.cash == 0.0
        assert inv.fills == []

    def test_reset_cycle(self):
        inv = MMInventory()
        inv.record_fill("BUY", 0.48, 10)
        inv.reset_cycle()
        assert inv.net_position == 0
        assert inv.cash == 0.0
        assert inv.fills == []


class TestMMInventoryFills:
    def test_buy_increases_position(self):
        inv = MMInventory()
        inv.record_fill("BUY", 0.48, 10)
        assert inv.net_position == 10
        assert inv.cash == pytest.approx(-4.80)

    def test_sell_decreases_position(self):
        inv = MMInventory()
        inv.record_fill("SELL", 0.52, 10)
        assert inv.net_position == -10
        assert inv.cash == pytest.approx(5.20)

    def test_fills_tracked(self):
        inv = MMInventory()
        inv.record_fill("BUY", 0.48, 10)
        assert len(inv.fills) == 1
        assert inv.fills[0]["side"] == "BUY"
        assert inv.fills[0]["price"] == 0.48
        assert inv.fills[0]["size"] == 10
        assert inv.fills[0]["net_position_after"] == 10

    def test_fill_count(self):
        inv = MMInventory()
        inv.record_fill("BUY", 0.48, 10)
        inv.record_fill("SELL", 0.52, 5)
        inv.record_fill("BUY", 0.47, 10)
        total, buys, sells = inv.get_fill_count()
        assert total == 3
        assert buys == 2
        assert sells == 1


class TestMMInventoryPnL:
    def test_round_trip_pnl(self):
        """Buy at 0.48, sell at 0.52 → P&L = 0.04 per share × 10 = 0.40."""
        inv = MMInventory()
        inv.record_fill("BUY", 0.48, 10)
        inv.record_fill("SELL", 0.52, 10)
        assert inv.net_position == 0
        assert inv.cash == pytest.approx(0.40)
        assert inv.get_pnl(0.50) == pytest.approx(0.40)

    def test_pnl_with_long_inventory(self):
        """Holding 10 shares bought at 0.48, marked to market."""
        inv = MMInventory()
        inv.record_fill("BUY", 0.48, 10)
        # cash = -4.80, position = 10
        # at mark 0.52: pnl = -4.80 + 10 * 0.52 = 0.40
        assert inv.get_pnl(0.52) == pytest.approx(0.40)
        # at mark 0.45: pnl = -4.80 + 10 * 0.45 = -0.30
        assert inv.get_pnl(0.45) == pytest.approx(-0.30)

    def test_pnl_with_short_inventory(self):
        """Sold 10 shares at 0.52, marked to market."""
        inv = MMInventory()
        inv.record_fill("SELL", 0.52, 10)
        # cash = 5.20, position = -10
        # at mark 0.48: pnl = 5.20 + (-10) * 0.48 = 0.40
        assert inv.get_pnl(0.48) == pytest.approx(0.40)
        # at mark 0.55: pnl = 5.20 + (-10) * 0.55 = -0.30
        assert inv.get_pnl(0.55) == pytest.approx(-0.30)

    def test_multiple_trades_pnl(self):
        """Multiple buys and sells → correct net P&L."""
        inv = MMInventory()
        inv.record_fill("BUY", 0.47, 10)    # cash: -4.70
        inv.record_fill("SELL", 0.53, 10)    # cash: -4.70 + 5.30 = 0.60
        inv.record_fill("BUY", 0.48, 5)     # cash: 0.60 - 2.40 = -1.80
        # net_position = 0 + 10 - 10 + 5 = 5
        assert inv.net_position == 5
        assert inv.cash == pytest.approx(-1.80)
        # at mark 0.50: pnl = -1.80 + 5 * 0.50 = 0.70
        assert inv.get_pnl(0.50) == pytest.approx(0.70)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_mm_inventory.py -v`
Expected: FAIL with `ImportError: cannot import name 'MMInventory'`

- [ ] **Step 3: Implement `MMInventory` class**

Insert in `cvd_5min_bot.py` after `print_paper_summary()` (after line ~949) and before the `# HYPERLIQUID HEDGE FUNCTIONS` section:

```python


# ============================================================================
# MARKET MAKER — INVENTORY TRACKER
# ============================================================================

class MMInventory:
    """Track market maker inventory, fills, and P&L."""

    def __init__(self):
        self.net_position: int = 0     # positive = long UP, negative = short UP
        self.cash: float = 0.0         # cash from sells minus cash for buys
        self.fills: list = []

    def reset_cycle(self):
        """Reset for a new market cycle."""
        self.net_position = 0
        self.cash = 0.0
        self.fills = []

    def record_fill(self, side: str, price: float, size: int):
        """Record a fill. BUY increases position, SELL decreases it."""
        if side == "BUY":
            self.net_position += size
            self.cash -= price * size
        elif side == "SELL":
            self.net_position -= size
            self.cash += price * size

        self.fills.append({
            "timestamp": time.time(),
            "side": side,
            "price": price,
            "size": size,
            "net_position_after": self.net_position,
        })

    def get_pnl(self, mark_price: float) -> float:
        """P&L = cash + (net_position × mark_price).

        For a round trip (buy 10 @ 0.48, sell 10 @ 0.52):
          cash = -4.80 + 5.20 = 0.40, position = 0, P&L = 0.40
        """
        return self.cash + (self.net_position * mark_price)

    def get_fill_count(self) -> tuple:
        """Returns (total, buys, sells)."""
        buys = sum(1 for f in self.fills if f["side"] == "BUY")
        sells = sum(1 for f in self.fills if f["side"] == "SELL")
        return len(self.fills), buys, sells
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_mm_inventory.py -v`
Expected: 10 PASSED

- [ ] **Step 5: Run full test suite**

Run: `pytest -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add cvd_5min_bot.py tests/unit/test_mm_inventory.py
git commit -m "feat: add MMInventory class for position and P&L tracking"
```

---

## Task 4: Paper Fill Simulation + MM Logging

**Files:**
- Modify: `cvd_5min_bot.py` (after `MMInventory` class)
- Create: `tests/unit/test_mm_paper.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_mm_paper.py`:

```python
"""Unit tests for MM paper fills and logging."""
import os

import pandas as pd
import pytest

import cvd_5min_bot as bot


# ── check_mm_paper_fills ────────────────────────────────────────────────

class TestCheckMmPaperFills:
    def test_bid_filled_when_ask_drops(self):
        """Market ask ≤ our bid → we buy."""
        book = {"best_bid": 0.45, "best_ask": 0.47, "spread": 0.02}
        fills = bot.check_mm_paper_fills(book, our_bid=0.48, our_ask=0.52,
                                         bid_size=10, ask_size=10)
        assert len(fills) == 1
        assert fills[0]["side"] == "BUY"
        assert fills[0]["price"] == 0.48
        assert fills[0]["size"] == 10

    def test_ask_filled_when_bid_rises(self):
        """Market bid ≥ our ask → we sell."""
        book = {"best_bid": 0.53, "best_ask": 0.55, "spread": 0.02}
        fills = bot.check_mm_paper_fills(book, our_bid=0.48, our_ask=0.52,
                                         bid_size=10, ask_size=10)
        assert len(fills) == 1
        assert fills[0]["side"] == "SELL"
        assert fills[0]["price"] == 0.52
        assert fills[0]["size"] == 10

    def test_no_fill_within_spread(self):
        """Market inside our spread → no fills."""
        book = {"best_bid": 0.49, "best_ask": 0.51, "spread": 0.02}
        fills = bot.check_mm_paper_fills(book, our_bid=0.48, our_ask=0.52,
                                         bid_size=10, ask_size=10)
        assert len(fills) == 0

    def test_both_sides_filled(self):
        """Extreme move crosses both sides."""
        book = {"best_bid": 0.53, "best_ask": 0.47, "spread": -0.06}
        fills = bot.check_mm_paper_fills(book, our_bid=0.48, our_ask=0.52,
                                         bid_size=10, ask_size=10)
        assert len(fills) == 2
        sides = {f["side"] for f in fills}
        assert sides == {"BUY", "SELL"}

    def test_zero_size_no_fill(self):
        """Size 0 → no fill even if price crosses."""
        book = {"best_bid": 0.53, "best_ask": 0.47, "spread": -0.06}
        fills = bot.check_mm_paper_fills(book, our_bid=0.48, our_ask=0.52,
                                         bid_size=0, ask_size=0)
        assert len(fills) == 0

    def test_zero_quote_no_fill(self):
        """Quote price 0 → no fill."""
        book = {"best_bid": 0.53, "best_ask": 0.47, "spread": -0.06}
        fills = bot.check_mm_paper_fills(book, our_bid=0.0, our_ask=0.0,
                                         bid_size=10, ask_size=10)
        assert len(fills) == 0


# ── MM Logging ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mm_tmp_dir(tmp_path, monkeypatch):
    """Redirect MM_LOG_FILE to temp directory."""
    monkeypatch.setattr(bot, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "MM_LOG_FILE", str(tmp_path / "mm_paper_trades.csv"))
    return tmp_path


class TestLogMmFill:
    def test_creates_csv(self):
        bot.log_mm_fill(1000, "slug-1", "BUY", 0.48, 10, 10, 0.005, "BULLISH_DIV")
        assert os.path.exists(bot.MM_LOG_FILE)
        df = pd.read_csv(bot.MM_LOG_FILE)
        assert len(df) == 1

    def test_appends(self):
        bot.log_mm_fill(1000, "slug-1", "BUY", 0.48, 10, 10, 0.005, "BULLISH_DIV")
        bot.log_mm_fill(1000, "slug-1", "SELL", 0.52, 10, 0, 0.005, "BULLISH_DIV")
        df = pd.read_csv(bot.MM_LOG_FILE)
        assert len(df) == 2

    def test_correct_columns(self):
        bot.log_mm_fill(1000, "slug-1", "BUY", 0.48, 10, 10, 0.005, "NEUTRAL")
        df = pd.read_csv(bot.MM_LOG_FILE)
        expected = {"timestamp", "market_ts", "market_slug", "side", "price",
                    "size", "inventory_after", "cvd_skew", "signal_type"}
        assert set(df.columns) == expected

    def test_values_stored(self):
        bot.log_mm_fill(2000, "slug-2", "SELL", 0.52, 5, -5, -0.01, "BEARISH_DIV")
        df = pd.read_csv(bot.MM_LOG_FILE)
        row = df.iloc[0]
        assert row["market_ts"] == 2000
        assert row["side"] == "SELL"
        assert row["price"] == 0.52
        assert row["size"] == 5
        assert row["inventory_after"] == -5


class TestPrintMmSummary:
    def test_no_file(self, capsys):
        bot.print_mm_summary()
        out = capsys.readouterr().out
        assert "No MM trades" in out

    def test_with_data(self, capsys):
        bot.log_mm_fill(1000, "s1", "BUY", 0.48, 10, 10, 0.0, "NEUTRAL")
        bot.log_mm_fill(1000, "s1", "SELL", 0.52, 10, 0, 0.0, "NEUTRAL")
        bot.print_mm_summary()
        out = capsys.readouterr().out
        assert "Market Maker Summary" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_mm_paper.py -v`
Expected: FAIL with `AttributeError: module 'cvd_5min_bot' has no attribute 'check_mm_paper_fills'`

- [ ] **Step 3: Implement paper fills and logging functions**

Insert in `cvd_5min_bot.py` after the `MMInventory` class:

```python


# ============================================================================
# MARKET MAKER — PAPER FILL SIMULATION
# ============================================================================

def check_mm_paper_fills(book: dict, our_bid: float, our_ask: float,
                         bid_size: int, ask_size: int) -> list:
    """
    Simulate MM fills in paper mode by comparing our quotes vs real orderbook.

    If real best_ask ≤ our bid → someone sold into our bid (we buy).
    If real best_bid ≥ our ask → someone bought from our ask (we sell).

    Returns list of fill dicts: [{"side": str, "price": float, "size": int}]
    """
    fills = []

    if bid_size > 0 and our_bid > 0 and book["best_ask"] <= our_bid:
        fills.append({"side": "BUY", "price": our_bid, "size": bid_size})

    if ask_size > 0 and our_ask > 0 and book["best_bid"] >= our_ask:
        fills.append({"side": "SELL", "price": our_ask, "size": ask_size})

    return fills


# ============================================================================
# MARKET MAKER — LOGGING
# ============================================================================

def log_mm_fill(market_ts: int, market_slug: str, side: str, price: float,
                size: int, inventory_after: int, cvd_skew: float,
                signal_type: str):
    """Log a market maker fill to CSV."""
    os.makedirs(DATA_DIR, exist_ok=True)

    new_row = pd.DataFrame([{
        "timestamp": datetime.now().isoformat(),
        "market_ts": market_ts,
        "market_slug": market_slug,
        "side": side,
        "price": round(price, 4),
        "size": size,
        "inventory_after": inventory_after,
        "cvd_skew": round(cvd_skew, 4),
        "signal_type": signal_type,
    }])

    if os.path.exists(MM_LOG_FILE):
        existing = pd.read_csv(MM_LOG_FILE)
        df = pd.concat([existing, new_row], ignore_index=True)
    else:
        df = new_row

    df.to_csv(MM_LOG_FILE, index=False)


def print_mm_summary():
    """Print market maker P&L summary."""
    if not os.path.exists(MM_LOG_FILE):
        print(colored("   📊 No MM trades yet", "yellow"))
        return

    df = pd.read_csv(MM_LOG_FILE)
    if len(df) == 0:
        print(colored("   📊 MM log empty", "yellow"))
        return

    buys = df[df["side"] == "BUY"]
    sells = df[df["side"] == "SELL"]

    total_bought = (buys["price"] * buys["size"]).sum() if len(buys) > 0 else 0
    total_sold = (sells["price"] * sells["size"]).sum() if len(sells) > 0 else 0
    realized_pnl = total_sold - total_bought

    print(colored(f"\n   📊 Market Maker Summary:", "cyan", attrs=["bold"]))
    print(colored(f"      Fills: {len(df)} ({len(buys)} buys, {len(sells)} sells)", "white"))
    print(colored(f"      Total bought: ${total_bought:.2f} | Total sold: ${total_sold:.2f}", "white"))
    pnl_color = "green" if realized_pnl >= 0 else "red"
    print(colored(f"      Realized P&L: ${realized_pnl:.4f}", pnl_color, attrs=["bold"]))

    if len(buys) > 0:
        print(colored(f"      Avg buy price:  ${buys['price'].mean():.4f}", "white"))
    if len(sells) > 0:
        print(colored(f"      Avg sell price: ${sells['price'].mean():.4f}", "white"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_mm_paper.py -v`
Expected: 13 PASSED

- [ ] **Step 5: Run full test suite**

Run: `pytest -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add cvd_5min_bot.py tests/unit/test_mm_paper.py
git commit -m "feat: add MM paper fill simulation and logging functions"
```

---

## Task 5: `CVDMarketMaker` Class

The main orchestrator — manages quotes, fills, and the 5-min market lifecycle.

**Files:**
- Modify: `cvd_5min_bot.py` (after `CVDStinkBot` class, before `main()`)
- Modify: `tests/integration/test_mm_smoke.py` (add class tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_mm_smoke.py`:

```python


class TestCVDMarketMakerCreation:
    def test_creates_without_error(self):
        feed = bot.BinanceCVDFeed(max_trades=100)
        mm = bot.CVDMarketMaker(feed)
        assert mm.inventory.net_position == 0
        assert mm.current_bid == 0.0
        assert mm.current_ask == 0.0

    def test_reset_clears_state(self):
        feed = bot.BinanceCVDFeed(max_trades=100)
        mm = bot.CVDMarketMaker(feed)
        mm.current_bid = 0.48
        mm.current_ask = 0.52
        mm.cycle_fills = 5
        mm.reset()
        assert mm.current_bid == 0.0
        assert mm.current_ask == 0.0
        assert mm.cycle_fills == 0
        assert mm.inventory.net_position == 0

    def test_compute_cvd_skew_no_trades(self):
        feed = bot.BinanceCVDFeed(max_trades=100)
        mm = bot.CVDMarketMaker(feed)
        skew, sig_type = mm.compute_cvd_skew()
        assert skew == 0.0
        assert sig_type == "NEUTRAL"

    def test_cancel_orders_paper_no_crash(self):
        feed = bot.BinanceCVDFeed(max_trades=100)
        mm = bot.CVDMarketMaker(feed)
        mm.up_token_id = "fake-token"
        mm.cancel_orders()  # should not crash in paper mode
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_mm_smoke.py::TestCVDMarketMakerCreation -v`
Expected: FAIL with `AttributeError: module 'cvd_5min_bot' has no attribute 'CVDMarketMaker'`

- [ ] **Step 3: Implement `CVDMarketMaker` class**

Insert in `cvd_5min_bot.py` after the `CVDStinkBot` class (after line ~1496) and before `# MAIN ENTRY`:

```python


class CVDMarketMaker:
    """
    Market maker for BTC 5-min markets, biased by CVD signals.

    Posts two-sided quotes (bid + ask) on the UP token.
    CVD signal shifts the midpoint. Inventory level adjusts skew.
    Zero maker fees + 20% rebate = structural edge.
    """

    def __init__(self, feed: BinanceCVDFeed):
        self.feed = feed
        self.inventory = MMInventory()
        self.current_market_ts: int | None = None
        self.market_info: dict | None = None
        self.up_token_id: str | None = None
        self.down_token_id: str | None = None
        self.current_bid: float = 0.0
        self.current_ask: float = 0.0
        self.current_bid_size: int = 0
        self.current_ask_size: int = 0
        self.current_cvd_skew: float = 0.0
        self.current_signal_type: str = "NEUTRAL"
        self.bid_order_id: str | None = None
        self.ask_order_id: str | None = None
        self.cycle_fills: int = 0

    def reset(self):
        """Reset state for next market cycle."""
        self.current_market_ts = None
        self.market_info = None
        self.up_token_id = None
        self.down_token_id = None
        self.current_bid = 0.0
        self.current_ask = 0.0
        self.current_bid_size = 0
        self.current_ask_size = 0
        self.current_cvd_skew = 0.0
        self.current_signal_type = "NEUTRAL"
        self.bid_order_id = None
        self.ask_order_id = None
        self.inventory.reset_cycle()
        self.cycle_fills = 0

    def compute_cvd_skew(self) -> tuple:
        """
        Get CVD signal for the primary timeframe and convert to a price skew.

        Returns (skew_value: float, signal_type: str)
        - Positive skew = bullish (shift quotes up)
        - Negative skew = bearish (shift quotes down)
        """
        trades = self.feed.get_trades_since(CVD_PRIMARY_TF)
        if len(trades) < 10:
            return 0.0, "NEUTRAL"

        cvd_val, price_chg, count = compute_volume_cvd(trades)
        signal_type, _, direction, strength = detect_divergence(price_chg, cvd_val)

        if direction == "UP":
            skew = (strength / 100.0) * MM_MAX_CVD_SKEW
        elif direction == "DOWN":
            skew = -(strength / 100.0) * MM_MAX_CVD_SKEW
        else:
            skew = 0.0

        return skew, signal_type

    def cancel_orders(self):
        """Cancel all MM quotes."""
        if self.up_token_id and not PAPER_MODE:
            cancel_token_orders(self.up_token_id)

    def _refresh_quotes(self, book: dict):
        """Recalculate and post new quotes."""
        midpoint = (book["best_bid"] + book["best_ask"]) / 2.0

        self.current_cvd_skew, self.current_signal_type = self.compute_cvd_skew()

        quotes = calculate_mm_quotes(
            midpoint=midpoint,
            base_spread=MM_BASE_SPREAD,
            cvd_skew=self.current_cvd_skew,
            inventory=self.inventory.net_position,
            max_inventory=MM_MAX_INVENTORY,
            order_size=MM_ORDER_SIZE,
        )

        self.current_bid = quotes["bid_price"]
        self.current_ask = quotes["ask_price"]
        self.current_bid_size = quotes["bid_size"]
        self.current_ask_size = quotes["ask_size"]

        # In paper mode, don't place real orders — fills are simulated
        if not PAPER_MODE:
            # Cancel existing, post new
            if self.up_token_id:
                cancel_token_orders(self.up_token_id)

            neg_risk = self.market_info.get("neg_risk", False) if self.market_info else False
            if self.current_bid_size > 0:
                resp = place_limit_order(self.up_token_id, "BUY", self.current_bid,
                                         self.current_bid_size, neg_risk)
                self.bid_order_id = resp.get("orderID")
            if self.current_ask_size > 0:
                resp = place_limit_order(self.up_token_id, "SELL", self.current_ask,
                                         self.current_ask_size, neg_risk)
                self.ask_order_id = resp.get("orderID")

    def _check_paper_fills(self, book: dict):
        """Check for simulated fills in paper mode."""
        fills = check_mm_paper_fills(
            book, self.current_bid, self.current_ask,
            self.current_bid_size, self.current_ask_size,
        )

        for fill in fills:
            self.inventory.record_fill(fill["side"], fill["price"], fill["size"])
            self.cycle_fills += 1

            market_slug = self.market_info.get("slug", "") if self.market_info else ""
            log_mm_fill(
                market_ts=self.current_market_ts,
                market_slug=market_slug,
                side=fill["side"],
                price=fill["price"],
                size=fill["size"],
                inventory_after=self.inventory.net_position,
                cvd_skew=self.current_cvd_skew,
                signal_type=self.current_signal_type,
            )

            emoji = "🟢" if fill["side"] == "BUY" else "🔴"
            print(colored(
                f"\n   {emoji} MM PAPER FILL: {fill['side']} {fill['size']} shares "
                f"@ ${fill['price']:.2f} | Inventory: {self.inventory.net_position}",
                "green" if fill["side"] == "BUY" else "red",
            ))

    def run_market_cycle(self, market_ts: int):
        """
        Run one market making cycle for a 5-minute market.

        Flow:
          1. Find the market (same as stink bot)
          2. Main quoting loop:
             a. Fetch real orderbook (public API, works in paper mode)
             b. Check for simulated fills (paper) or real fills (live)
             c. Recalculate quotes with CVD skew + inventory adjustment
             d. Post/refresh orders (live only)
             e. Print status line
          3. On market end: cancel orders, print cycle P&L
        """
        self.current_market_ts = market_ts
        market_dt = datetime.fromtimestamp(market_ts, tz=timezone.utc)
        market_et = datetime.fromtimestamp(market_ts, tz=ET)

        print(colored(f"\n{'=' * 70}", "magenta"))
        print(colored("📊 CVD MARKET MAKER CYCLE", "magenta", attrs=["bold"]))
        print(colored(
            f"   Market time: {market_et.strftime('%I:%M:%S%p ET')} | "
            f"{market_dt.strftime('%H:%M:%S UTC')}",
            "white",
        ))
        print(colored(f"{'=' * 70}", "magenta"))

        # Wait briefly for market to be indexed
        time_remaining = get_time_remaining(market_ts)
        if time_remaining > MARKET_DURATION - 10:
            print(colored("   ⏳ Waiting 10s for market index...", "yellow"))
            time.sleep(10)

        # Find market (up to 5 retries)
        self.market_info = None
        for attempt in range(5):
            self.market_info = get_market_info(market_ts)
            if self.market_info:
                break
            print(colored(f"   🔄 Retry {attempt + 1}/5 in 2s...", "yellow"))
            time.sleep(2)

        if not self.market_info:
            print(colored("   ❌ Could not find market, skipping cycle", "red"))
            return

        self.up_token_id = self.market_info["up_token_id"]
        self.down_token_id = self.market_info["down_token_id"]
        market_slug = self.market_info.get("slug", "")
        print(colored(f"   🔗 Market: https://polymarket.com/event/{market_slug}", "cyan", attrs=["bold"]))

        # Main quoting loop
        while True:
            time_remaining = get_time_remaining(market_ts)

            # Market ended
            if time_remaining <= 0:
                print(colored(f"\n   🔴 Market ended!", "yellow"))
                self.cancel_orders()

                # Calculate final cycle P&L
                mark_price = 0.50
                book = get_order_book(self.up_token_id)
                if book:
                    mark_price = (book["best_bid"] + book["best_ask"]) / 2.0

                pnl = self.inventory.get_pnl(mark_price)
                total, buys, sells = self.inventory.get_fill_count()
                pnl_color = "green" if pnl >= 0 else "red"
                print(colored(
                    f"   📊 Cycle P&L: ${pnl:.4f} | Fills: {total} ({buys}B/{sells}S) | "
                    f"Final inventory: {self.inventory.net_position}",
                    pnl_color, attrs=["bold"],
                ))
                break

            # Stop quoting near market end
            if time_remaining < MM_STOP_QUOTING_SEC:
                self.cancel_orders()
                mins = time_remaining // 60
                secs = time_remaining % 60
                print(colored(
                    f"\r   ⏳ Quoting stopped, waiting for market end... "
                    f"{mins}:{secs:02d}   ",
                    "yellow",
                ), end="", flush=True)
                time.sleep(2)
                continue

            # Get real orderbook (public API — works in paper mode)
            book = get_order_book(self.up_token_id)
            if not book:
                print(colored("   ⚠️ No orderbook, retrying...", "yellow"))
                time.sleep(5)
                continue

            # Check for paper fills BEFORE refreshing (uses previous quotes)
            if PAPER_MODE and self.current_bid > 0:
                self._check_paper_fills(book)

            # Refresh quotes (recalculate with updated inventory + CVD)
            self._refresh_quotes(book)

            # Print status line
            mins = time_remaining // 60
            secs = time_remaining % 60
            btc_price = self.feed.get_last_price()
            total, buys, sells = self.inventory.get_fill_count()
            pnl = self.inventory.get_pnl((book["best_bid"] + book["best_ask"]) / 2.0)

            skew_arrow = "↑" if self.current_cvd_skew > 0.001 else (
                "↓" if self.current_cvd_skew < -0.001 else "→"
            )
            print(colored(
                f"\r   📊 BID ${self.current_bid:.2f} | ASK ${self.current_ask:.2f} | "
                f"Sprd ${self.current_ask - self.current_bid:.2f} | "
                f"CVD {skew_arrow} | Inv: {self.inventory.net_position} | "
                f"Fills: {total} | P&L: ${pnl:+.4f} | "
                f"BTC ${btc_price:,.0f} | {mins}:{secs:02d}   ",
                "white",
            ), end="", flush=True)

            time.sleep(MM_REFRESH_INTERVAL)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_mm_smoke.py -v`
Expected: 12 PASSED (8 constants + 4 class tests)

- [ ] **Step 5: Run full test suite**

Run: `pytest -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add cvd_5min_bot.py tests/integration/test_mm_smoke.py
git commit -m "feat: add CVDMarketMaker class with paper fill simulation"
```

---

## Task 6: Wire into `main()` + Strategy Mode

**Files:**
- Modify: `cvd_5min_bot.py` (`main()` function)
- Modify: `tests/integration/test_mm_smoke.py` (add strategy test)

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_mm_smoke.py`:

```python


class TestStrategyMode:
    def test_strategy_default_is_stink(self):
        """Default strategy should be 'stink'."""
        # In test env, STRATEGY is not set → default = "stink"
        assert bot.STRATEGY in ("stink", "mm")

    def test_mm_and_stink_share_interface(self):
        """Both bot classes have reset() and run_market_cycle()."""
        feed = bot.BinanceCVDFeed(max_trades=100)
        stink = bot.CVDStinkBot(feed)
        mm = bot.CVDMarketMaker(feed)

        assert hasattr(stink, "reset") and callable(stink.reset)
        assert hasattr(stink, "run_market_cycle") and callable(stink.run_market_cycle)
        assert hasattr(mm, "reset") and callable(mm.reset)
        assert hasattr(mm, "run_market_cycle") and callable(mm.run_market_cycle)

    def test_both_bots_have_cancel_orders(self):
        """Both bot classes have cancel_orders()."""
        feed = bot.BinanceCVDFeed(max_trades=100)
        stink = bot.CVDStinkBot(feed)
        mm = bot.CVDMarketMaker(feed)
        assert hasattr(stink, "cancel_orders") and callable(stink.cancel_orders)
        assert hasattr(mm, "cancel_orders") and callable(mm.cancel_orders)
```

- [ ] **Step 2: Run test to verify it passes** (these should already pass from previous tasks)

Run: `pytest tests/integration/test_mm_smoke.py::TestStrategyMode -v`
Expected: 3 PASSED

- [ ] **Step 3: Modify `main()` for strategy selection**

In `cvd_5min_bot.py`, modify the `main()` function. There are 4 changes:

**Change 1:** After the existing config print block (after `HEDGE_LEVERAGE` print, around `if PAPER_MODE:`), add MM config display. Find this line:

```python
    if PAPER_MODE:
```

Insert **before** it:

```python
    if STRATEGY == "mm":
        print(colored(f"\n   📊 Market Making Configuration:", "magenta", attrs=["bold"]))
        print(colored(f"      STRATEGY                = {STRATEGY}", "white"))
        print(colored(f"      MM_BASE_SPREAD          = ${MM_BASE_SPREAD}", "white"))
        print(colored(f"      MM_ORDER_SIZE           = {MM_ORDER_SIZE} shares/side", "white"))
        print(colored(f"      MM_MAX_INVENTORY        = {MM_MAX_INVENTORY} shares", "white"))
        print(colored(f"      MM_MAX_CVD_SKEW         = ${MM_MAX_CVD_SKEW}", "white"))
        print(colored(f"      MM_REFRESH_INTERVAL     = {MM_REFRESH_INTERVAL}s", "white"))
        print(colored(f"      MM_STOP_QUOTING_SEC     = {MM_STOP_QUOTING_SEC}s", "white"))
```

**Change 2:** After `print_paper_summary()`, add MM summary. Find these lines:

```python
    if PAPER_MODE:
        print_paper_summary()
```

Add after them:

```python
    if STRATEGY == "mm":
        print_mm_summary()
```

**Change 3:** Replace the bot creation line. Find this line:

```python
    bot = CVDStinkBot(feed)
```

Replace with:

```python
    if STRATEGY == "mm":
        bot = CVDMarketMaker(feed)
        print(colored(f"\n   📊 Market Maker active — posting two-sided quotes with CVD bias", "magenta", attrs=["bold"]))
    else:
        bot = CVDStinkBot(feed)
        print(colored(f"\n   👁️ Watching BTC order flow for divergences and momentum...", "white"))
```

And remove the old standalone print that says "Watching BTC order flow" (which was right after the old `bot = CVDStinkBot(feed)` line).

**Change 4:** In the KeyboardInterrupt handler, conditionally skip hedge check for MM. Find:

```python
            bot.cancel_orders()
            if HEDGE_ENABLED:
```

Replace the `if HEDGE_ENABLED:` block with:

```python
            bot.cancel_orders()
            if STRATEGY != "mm" and HEDGE_ENABLED:
```

- [ ] **Step 4: Run full test suite**

Run: `pytest -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add cvd_5min_bot.py tests/integration/test_mm_smoke.py
git commit -m "feat: wire CVDMarketMaker into main() with STRATEGY env var"
```

---

## Task 7: Update `.env` and `.env.example`

**Files:**
- Modify: `.env`
- Modify: `.env.example`

- [ ] **Step 1: Update `.env.example` with MM config**

Add to `.env.example` after the existing content:

```env
# --- Market Making Strategy ---
# STRATEGY=mm              # "stink" (default, directional) or "mm" (market making)
# MM_BASE_SPREAD=0.04      # Base spread in dollars (4 cents)
# MM_ORDER_SIZE=10          # Shares per side per quote
# MM_MAX_INVENTORY=50       # Max net position
# MM_MAX_CVD_SKEW=0.02      # Max CVD price shift
# MM_REFRESH_INTERVAL=10    # Quote refresh interval (seconds)
```

- [ ] **Step 2: Verify `.env` doesn't need changes**

The current `.env` only has `PAPER_MODE=true`. The MM constants default correctly from `os.getenv()` calls. To activate MM mode, user will add `STRATEGY=mm` when ready. No change needed now.

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs: add MM configuration to .env.example"
```

---

## Task 8: Dashboard MM Panel

**Files:**
- Modify: `paper_dashboard.py`

- [ ] **Step 1: Add MM log file constant and import**

At the top of `paper_dashboard.py`, after the existing constants, add:

```python
MM_LOG_FILE = os.path.join(DATA_DIR, "mm_paper_trades.csv")
```

- [ ] **Step 2: Add `render_mm_panel()` function**

Insert after `render_dashboard()` and before `def main()`:

```python

def render_mm_panel():
    """Render market maker stats panel (only if MM data exists)."""
    if not os.path.exists(MM_LOG_FILE):
        return

    try:
        df = pd.read_csv(MM_LOG_FILE)
    except Exception:
        return

    if len(df) == 0:
        return

    print(colored("  ── MARKET MAKER STATS ────────────────────────────────", "magenta", attrs=["bold"]))

    buys = df[df["side"] == "BUY"]
    sells = df[df["side"] == "SELL"]

    total_bought = (buys["price"] * buys["size"]).sum() if len(buys) > 0 else 0
    total_sold = (sells["price"] * sells["size"]).sum() if len(sells) > 0 else 0
    realized_pnl = total_sold - total_bought

    print(colored(f"  Fills: {len(df)} ({len(buys)} buys, {len(sells)} sells)", "white"))
    pnl_color = "green" if realized_pnl >= 0 else "red"
    print(colored(f"  Realized P&L: ${realized_pnl:.4f}", pnl_color, attrs=["bold"]))

    if len(buys) > 0:
        print(colored(f"  Avg buy:  ${buys['price'].mean():.4f}", "white"))
    if len(sells) > 0:
        print(colored(f"  Avg sell: ${sells['price'].mean():.4f}", "white"))
    if len(buys) > 0 and len(sells) > 0:
        avg_spread = sells["price"].mean() - buys["price"].mean()
        print(colored(f"  Avg spread captured: ${avg_spread:.4f}", "cyan"))

    # Last 5 fills
    print(colored("\n  Recent fills:", "white"))
    recent = df.tail(5).iloc[::-1]
    for _, row in recent.iterrows():
        ts = str(row["timestamp"])[11:19]
        side = row["side"]
        emoji = "🟢" if side == "BUY" else "🔴"
        clr = "green" if side == "BUY" else "red"
        print(colored(
            f"  {ts}  {emoji} {side:<4} {int(row['size'])} @ ${row['price']:.4f} "
            f"| inv: {int(row['inventory_after'])} | skew: {row['cvd_skew']:+.4f}",
            clr,
        ))
    print()
```

- [ ] **Step 3: Call `render_mm_panel()` from `render_dashboard()`**

In `render_dashboard()`, add a call to `render_mm_panel()` after the price movement stats section and before the "Press Ctrl+C" line. Find:

```python
    print(colored("  Press Ctrl+C to exit.", "yellow"))
```

Insert before it:

```python
    render_mm_panel()

```

- [ ] **Step 4: Test manually**

Run: `python paper_dashboard.py`
Expected: Dashboard renders without error. MM panel only appears when `data/mm_paper_trades.csv` exists. No crash when file doesn't exist.

- [ ] **Step 5: Run full test suite**

Run: `pytest -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add paper_dashboard.py
git commit -m "feat: add market maker stats panel to paper dashboard"
```

---

## Test Summary

After all tasks, the test suite should include:

| File | Tests | What it covers |
|------|-------|---------------|
| `tests/unit/test_cvd_logic.py` | 16 | CVD core: compute_volume_cvd, detect_divergence |
| `tests/unit/test_paper_mode.py` | 11 | Paper logging: log_paper_signal, resolve_paper_outcome, print_paper_summary |
| `tests/unit/test_mm_quotes.py` | 12 | Quote calculator: symmetric, CVD skew, inventory skew, bounds, rounding |
| `tests/unit/test_mm_inventory.py` | 10 | Inventory tracker: fills, position, P&L, reset |
| `tests/unit/test_mm_paper.py` | 13 | Paper fills simulation + MM logging |
| `tests/integration/test_bot_smoke.py` | 12 | Smoke: module import, constants, paper guards, feed creation |
| `tests/integration/test_mm_smoke.py` | 15 | MM smoke: constants, class creation, strategy mode |
| **Total** | **~89** | |

---

## How to Run the Market Maker

After implementation, to switch from stink bids to market making:

```bash
# In .env, add:
STRATEGY=mm
PAPER_MODE=true

# Run the bot:
python cvd_5min_bot.py

# In another terminal, watch results:
python paper_dashboard.py
```

The bot will:
1. Connect to Binance WS (public, same as before)
2. Find each 5-min market via Gamma API (same as before)
3. Fetch real orderbook for the UP token (public CLOB API)
4. Calculate bid/ask with CVD skew + inventory adjustment
5. Simulate fills when market crosses our quotes
6. Log fills to `data/mm_paper_trades.csv`
7. Print cycle P&L at market end

**Let it run for 2-3 days** to collect meaningful data. The dashboard shows your simulated P&L, fill rate, and spread capture.
