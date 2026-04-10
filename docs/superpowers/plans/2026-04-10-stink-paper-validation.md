# Stink Paper Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `stink` paper-validation harness from `docs/superpowers/specs/2026-04-10-stink-paper-validation-design.md` — CVD signal flip, dual truth-source outcome resolver, and auto-stop statistical gate with sentinel-based halt.

**Architecture:** Three independent components added to the existing single-file bot:
1. `CVD_INVERT_SIGNAL` env flag flips direction inside `check_cvd_signal` (~5 LOC change).
2. New module `truth_sources.py` exposes `fetch_binance_spot_price` and `fetch_polymarket_resolution`, called by a rewritten `resolve_paper_outcome` that writes three parallel outcome columns.
3. New module `paper_gate.py` exposes `evaluate(df) -> GateStatus` and sentinel read/write; called at end of every cycle in main loop, halts bot on PASS/KILL/INCONCLUSIVE.

**Tech Stack:** Python 3.13, pandas 2.2, requests 2.32, pytest 8.3 (+ pytest-mock via monkeypatch). No new dependencies.

---

## File structure

**New files:**
- `truth_sources.py` (~60 LOC) — two HTTP fetch functions, zero state.
- `paper_gate.py` (~100 LOC) — `GateStatus` dataclass, `one_sided_binomial_p`, `compute_pnl_per_trade`, `evaluate`, `write_sentinel`, `read_sentinel`.
- `tests/unit/test_signal_flip.py` (~60 LOC)
- `tests/unit/test_truth_sources.py` (~120 LOC)
- `tests/unit/test_paper_gate.py` (~250 LOC)
- `tests/integration/test_paper_flow.py` (~60 LOC)
- `scripts/smoke_paper_gate.py` (~60 LOC)

**Modified files:**
- `cvd_5min_bot.py`:
  - L146 area: add `CVD_INVERT_SIGNAL` constant
  - L440-525 (`check_cvd_signal`): flip logic before final return
  - L740-809 (`get_market_info`): add `condition_id` to returned dict
  - L921-948 (`log_paper_signal`): new columns, new parameters
  - L951-992 (`resolve_paper_outcome`): complete rewrite calling truth_sources
  - L995-1032 (`print_paper_summary`): rename to read `signal_correct_polymarket`
  - L1667-1668 (call site): pass `filled=self.poly_filled`
  - L1769-1779 (call site): pass new params (condition_id, shares, direction_original, invert_flag)
  - L2182 area: add `CVD_INVERT_SIGNAL` line to config banner
  - L2262 area: sentinel startup check + end-of-cycle evaluate
- `tests/unit/test_paper_mode.py`: schema migration, FakeFeed unchanged
- `.env.example`: add `CVD_INVERT_SIGNAL=false` with comment

**Untouched:** `paper_dashboard.py`, `backtest.py`, `CVDMarketMaker`, `MMInventory`, all tests outside `test_paper_mode.py` / `test_signal_flip.py` / `test_truth_sources.py` / `test_paper_gate.py` / `test_paper_flow.py`.

---

## Task 1: Signal-flip constant and logic

**Files:**
- Modify: `cvd_5min_bot.py:146` (add constant) and `cvd_5min_bot.py:525` (flip logic)
- Create: `tests/unit/test_signal_flip.py`

- [ ] **Step 1: Write the failing test file**

Create `tests/unit/test_signal_flip.py`:

```python
"""Unit tests for CVD_INVERT_SIGNAL flip logic inside check_cvd_signal."""
import pytest

import cvd_5min_bot as bot


class FakeFeed:
    """Stub feed returning a fixed canned trade list so check_cvd_signal
    produces a deterministic signal. We only care about the flip wrapper,
    not the divergence math itself.
    """
    def __init__(self, trades):
        self._trades = trades

    def get_trades_since(self, seconds):
        return list(self._trades)

    def get_last_price(self):
        return 84000.0

    def get_trade_count(self):
        return len(self._trades)


def _stub_signal(monkeypatch, direction, signal_type="BULLISH_DIV", detail="stub"):
    """Monkeypatch check_cvd_signal's internals so it returns a fixed tuple.

    Easier than crafting real CVD-diverging trade data: we replace the
    function's core divergence detector to yield the desired (direction,
    signal_type, detail). The flip wrapper still runs on top.
    """
    # Replace detect_divergence to always return our forced signal.
    def fake_detect(price_change, cvd_value):
        return (signal_type, "stub", direction, 100)
    monkeypatch.setattr(bot, "detect_divergence", fake_detect)
    # Provide enough stub trades to pass the len<50 gate.
    fake_trades = [(1000.0 * i, 84000.0, 0.1, True, "buy") for i in range(100)]
    return FakeFeed(fake_trades)


class TestSignalFlip:
    def test_flip_disabled_returns_original_up(self, monkeypatch):
        monkeypatch.setattr(bot, "CVD_INVERT_SIGNAL", False)
        feed = _stub_signal(monkeypatch, "UP")
        direction, signal_type, _ = bot.check_cvd_signal(feed)
        assert direction == "UP"
        assert signal_type == "BULLISH_DIV"

    def test_flip_disabled_returns_original_down(self, monkeypatch):
        monkeypatch.setattr(bot, "CVD_INVERT_SIGNAL", False)
        feed = _stub_signal(monkeypatch, "DOWN", signal_type="BEARISH_DIV")
        direction, signal_type, _ = bot.check_cvd_signal(feed)
        assert direction == "DOWN"
        assert signal_type == "BEARISH_DIV"

    def test_flip_enabled_up_becomes_down(self, monkeypatch):
        monkeypatch.setattr(bot, "CVD_INVERT_SIGNAL", True)
        feed = _stub_signal(monkeypatch, "UP")
        direction, signal_type, _ = bot.check_cvd_signal(feed)
        assert direction == "DOWN"
        # Signal type label is NOT renamed — only the bet target flips.
        assert signal_type == "BULLISH_DIV"

    def test_flip_enabled_down_becomes_up(self, monkeypatch):
        monkeypatch.setattr(bot, "CVD_INVERT_SIGNAL", True)
        feed = _stub_signal(monkeypatch, "DOWN", signal_type="BEARISH_DIV")
        direction, signal_type, _ = bot.check_cvd_signal(feed)
        assert direction == "UP"
        assert signal_type == "BEARISH_DIV"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_signal_flip.py -v`
Expected: FAIL with `AttributeError: module 'cvd_5min_bot' has no attribute 'CVD_INVERT_SIGNAL'`

- [ ] **Step 3: Add the constant in `cvd_5min_bot.py`**

Edit `cvd_5min_bot.py` around line 146. After the existing `MM_NEUTRAL_ONLY` line, insert:

```python
CVD_INVERT_SIGNAL = os.getenv("CVD_INVERT_SIGNAL", "false").lower() == "true"  # flip direction returned by check_cvd_signal (stink paper validation harness)
```

- [ ] **Step 4: Add the flip logic in `check_cvd_signal`**

Edit `cvd_5min_bot.py` at the final return of `check_cvd_signal` (currently line 525: `return best[0], best[1], best[2]`). Replace the single-line return with:

```python
    # Stink paper validation harness — hypothesis test that CVD signal is
    # anti-correlated with Polymarket outcomes. When CVD_INVERT_SIGNAL=true,
    # flip only the bet target; leave signal_type label intact so audit logs
    # still show the original divergence classification.
    direction_out = best[0]
    if CVD_INVERT_SIGNAL and direction_out == "UP":
        direction_out = "DOWN"
    elif CVD_INVERT_SIGNAL and direction_out == "DOWN":
        direction_out = "UP"
    return direction_out, best[1], best[2]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_signal_flip.py -v`
Expected: PASS 4/4

- [ ] **Step 6: Run the full prior test suite to confirm no regressions**

Run: `pytest tests/ -q`
Expected: All previously-passing tests still pass + 4 new tests pass.

- [ ] **Step 7: Commit**

```bash
git add tests/unit/test_signal_flip.py cvd_5min_bot.py
git commit -m "feat: add CVD_INVERT_SIGNAL flip for stink paper validation"
```

---

## Task 2: truth_sources.py — Binance spot + Polymarket Gamma fetchers

**Files:**
- Create: `truth_sources.py`
- Create: `tests/unit/test_truth_sources.py`

- [ ] **Step 1: Write the failing test file**

Create `tests/unit/test_truth_sources.py`:

```python
"""Unit tests for truth_sources.fetch_binance_spot_price and fetch_polymarket_resolution."""
from unittest.mock import MagicMock, patch

import pytest

import truth_sources


# ── fetch_binance_spot_price ────────────────────────────────────────────

class TestFetchBinanceSpot:
    def test_success_returns_close_price(self):
        """Mock a valid Klines response and assert the close price is parsed."""
        # Binance Klines format: [[openTime, open, high, low, close, volume, ...], ...]
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = [
            [1712000000000, "84000.00", "84200.00", "83900.00", "84150.50", "12.5",
             1712000059999, "1052000.0", 100, "6.0", "505000.0", "0"]
        ]
        with patch("truth_sources.requests.get", return_value=fake_response):
            price = truth_sources.fetch_binance_spot_price(1712000000)
        assert price == 84150.50

    def test_timeout_returns_none(self):
        import requests
        with patch("truth_sources.requests.get", side_effect=requests.Timeout("slow")):
            price = truth_sources.fetch_binance_spot_price(1712000000)
        assert price is None

    def test_http_error_returns_none(self):
        fake_response = MagicMock()
        fake_response.status_code = 500
        with patch("truth_sources.requests.get", return_value=fake_response):
            price = truth_sources.fetch_binance_spot_price(1712000000)
        assert price is None

    def test_empty_response_returns_none(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = []
        with patch("truth_sources.requests.get", return_value=fake_response):
            price = truth_sources.fetch_binance_spot_price(1712000000)
        assert price is None

    def test_exception_returns_none(self):
        with patch("truth_sources.requests.get", side_effect=ValueError("corrupt json")):
            price = truth_sources.fetch_binance_spot_price(1712000000)
        assert price is None


# ── fetch_polymarket_resolution ──────────────────────────────────────────

class TestFetchPolymarketResolution:
    def test_resolved_up(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = [{"conditionId": "0xabc", "resolvedPrice": "1.0"}]
        with patch("truth_sources.requests.get", return_value=fake_response):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome == "UP"

    def test_resolved_down(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = [{"conditionId": "0xabc", "resolvedPrice": "0.0"}]
        with patch("truth_sources.requests.get", return_value=fake_response):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome == "DOWN"

    def test_pending_resolution(self):
        """Null resolvedPrice means market not yet settled."""
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = [{"conditionId": "0xabc", "resolvedPrice": None}]
        with patch("truth_sources.requests.get", return_value=fake_response):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome == "PENDING"

    def test_missing_resolvedprice_field_is_pending(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = [{"conditionId": "0xabc"}]
        with patch("truth_sources.requests.get", return_value=fake_response):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome == "PENDING"

    def test_http_error_returns_none(self):
        """HTTP 500 is a transient error distinct from PENDING — we return None."""
        fake_response = MagicMock()
        fake_response.status_code = 500
        with patch("truth_sources.requests.get", return_value=fake_response):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome is None

    def test_empty_list_returns_none(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = []
        with patch("truth_sources.requests.get", return_value=fake_response):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome is None

    def test_exception_returns_none(self):
        import requests
        with patch("truth_sources.requests.get", side_effect=requests.Timeout("slow")):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_truth_sources.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'truth_sources'`

- [ ] **Step 3: Create `truth_sources.py`**

Create `truth_sources.py` at project root:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_truth_sources.py -v`
Expected: PASS 12/12

- [ ] **Step 5: Run the full prior test suite to confirm no regressions**

Run: `pytest tests/ -q`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add truth_sources.py tests/unit/test_truth_sources.py
git commit -m "feat: add truth_sources module for Binance spot + Polymarket Gamma fetches"
```

---

## Task 3: paper_gate.py — statistical gate module

**Files:**
- Create: `paper_gate.py`
- Create: `tests/unit/test_paper_gate.py`

- [ ] **Step 1: Write the failing test file**

Create `tests/unit/test_paper_gate.py`:

```python
"""Unit tests for paper_gate.evaluate and helpers."""
import json
from math import comb

import pandas as pd
import pytest

import paper_gate


def _row(signal_correct="YES", shares=10, stink_price=0.40, filled=True):
    return {
        "signal_correct_polymarket": signal_correct,
        "shares": shares,
        "stink_price": stink_price,
        "filled": filled,
    }


def _df(rows):
    return pd.DataFrame(rows)


# ── one_sided_binomial_p ────────────────────────────────────────────────

class TestBinomialP:
    def test_fifty_fifty_yields_half(self):
        # P(X >= 1 | n=2, p=0.5) = 3/4 = 0.75
        assert paper_gate.one_sided_binomial_p(1, 2, 0.5) == pytest.approx(0.75)

    def test_all_wins_yields_half_raised_to_n(self):
        # P(X >= n | n, 0.5) = 0.5^n
        assert paper_gate.one_sided_binomial_p(5, 5, 0.5) == pytest.approx(0.5 ** 5)

    def test_reference_value_60_38(self):
        # Known reference: P(X >= 38 | n=60, p=0.5) computed via scipy:
        #   scipy.stats.binom.sf(37, 60, 0.5) ≈ 0.04386
        # We use sum of P(X=k) from k=38..60.
        expected = sum(comb(60, k) * (0.5 ** 60) for k in range(38, 61))
        assert paper_gate.one_sided_binomial_p(38, 60, 0.5) == pytest.approx(expected, abs=1e-12)
        assert expected < 0.05  # sanity: this is the PASS_WINRATE threshold at n=60


# ── compute_pnl_per_trade ────────────────────────────────────────────────

class TestComputePnl:
    def test_winning_trade_at_40c(self):
        """Buy 10 shares @ $0.40, win → payout = 10 × $1 = $10. Entry cost = $4.
        Gross = 10 × (1.0 - 0.40) = 6.00. Fees = 10 × 1.0 × 0.0315 = 0.315.
        Net = 5.685.
        """
        row = pd.Series(_row(signal_correct="YES", shares=10, stink_price=0.40))
        pnl = paper_gate.compute_pnl_per_trade(row)
        assert pnl == pytest.approx(5.685, abs=1e-9)

    def test_losing_trade_at_40c(self):
        """Buy 10 shares @ $0.40, lose → payout = 0. Gross = 10 × (0 - 0.40) = -4.0.
        Fees = 10 × 0 × 0.0315 = 0 (no fees on losers under payout-side model).
        Net = -4.0.
        """
        row = pd.Series(_row(signal_correct="NO", shares=10, stink_price=0.40))
        pnl = paper_gate.compute_pnl_per_trade(row)
        assert pnl == pytest.approx(-4.0, abs=1e-9)

    def test_winning_trade_at_55c_cap(self):
        row = pd.Series(_row(signal_correct="YES", shares=12, stink_price=0.55))
        # Gross = 12 × 0.45 = 5.40; fees = 12 × 1 × 0.0315 = 0.378; net = 5.022
        pnl = paper_gate.compute_pnl_per_trade(row)
        assert pnl == pytest.approx(5.022, abs=1e-9)


# ── evaluate ────────────────────────────────────────────────────────────

class TestEvaluate:
    def test_empty_df_continues(self):
        status = paper_gate.evaluate(_df([]))
        assert status.status == "CONTINUE"
        assert status.n == 0

    def test_only_unfilled_trades_continues(self):
        df = _df([_row(signal_correct="YES", filled=False) for _ in range(40)])
        status = paper_gate.evaluate(df)
        assert status.status == "CONTINUE"
        assert status.n == 0

    def test_only_pending_trades_continues(self):
        df = _df([_row(signal_correct="PENDING", filled=True) for _ in range(40)])
        status = paper_gate.evaluate(df)
        assert status.status == "CONTINUE"
        assert status.n == 0

    def test_below_kill_n_bad_winrate_continues(self):
        # n=29, winrate=0.00 (all losses) → CONTINUE (below KILL_N)
        df = _df([_row(signal_correct="NO") for _ in range(29)])
        status = paper_gate.evaluate(df)
        assert status.status == "CONTINUE"
        assert status.n == 29

    def test_kill_at_threshold(self):
        # n=30, wins=10, winrate=0.333 → KILL
        rows = [_row(signal_correct="YES") for _ in range(10)]
        rows += [_row(signal_correct="NO") for _ in range(20)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "KILL"
        assert status.n == 30
        assert status.winrate == pytest.approx(10 / 30, abs=1e-9)

    def test_above_kill_n_borderline_continues(self):
        # n=30, wins=15, winrate=0.50 → CONTINUE (≥0.45, no PASS at n<60)
        rows = [_row(signal_correct="YES") for _ in range(15)]
        rows += [_row(signal_correct="NO") for _ in range(15)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "CONTINUE"

    def test_below_min_n_high_winrate_continues(self):
        # n=50, wins=40, winrate=0.80 → CONTINUE (n<60)
        rows = [_row(signal_correct="YES") for _ in range(40)]
        rows += [_row(signal_correct="NO") for _ in range(10)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "CONTINUE"

    def test_pass_all_gates(self):
        # n=60, wins=40, winrate=0.667, p≈0.004, EV strongly positive
        rows = [_row(signal_correct="YES", stink_price=0.40) for _ in range(40)]
        rows += [_row(signal_correct="NO", stink_price=0.40) for _ in range(20)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "PASS"
        assert status.n == 60
        assert status.winrate == pytest.approx(40 / 60, abs=1e-9)
        assert status.p_value < 0.05
        assert status.ev_per_trade >= 0.20

    def test_inconclusive_ev_too_low(self):
        # n=60, wins=35, winrate=0.583, p≈0.10 → INCONCLUSIVE (p>0.05)
        rows = [_row(signal_correct="YES", stink_price=0.50) for _ in range(35)]
        rows += [_row(signal_correct="NO", stink_price=0.50) for _ in range(25)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "INCONCLUSIVE"
        assert status.n == 60

    def test_inconclusive_winrate_just_below(self):
        # n=60, wins=34, winrate=0.567 → INCONCLUSIVE (< PASS_WINRATE 0.58)
        rows = [_row(signal_correct="YES") for _ in range(34)]
        rows += [_row(signal_correct="NO") for _ in range(26)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "INCONCLUSIVE"


# ── sentinel read/write ────────────────────────────────────────────────

class TestSentinel:
    def test_write_and_read_roundtrip(self, tmp_path):
        path = str(tmp_path / "sentinel.json")
        status = paper_gate.GateStatus(
            status="PASS",
            reason="test",
            n=60,
            winrate=0.67,
            p_value=0.004,
            ev_per_trade=0.25,
            evaluated_at="2026-04-10T12:00:00+00:00",
        )
        paper_gate.write_sentinel(status, path)
        loaded = paper_gate.read_sentinel(path)
        assert loaded is not None
        assert loaded.status == "PASS"
        assert loaded.n == 60
        assert loaded.winrate == pytest.approx(0.67)

    def test_read_missing_file_returns_none(self, tmp_path):
        path = str(tmp_path / "does_not_exist.json")
        assert paper_gate.read_sentinel(path) is None

    def test_read_corrupt_file_returns_none(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("not json at all")
        assert paper_gate.read_sentinel(str(path)) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_paper_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'paper_gate'`

- [ ] **Step 3: Create `paper_gate.py`**

Create `paper_gate.py` at project root:

```python
"""Paper-trade statistical gate for the stink validation harness.

Pure logic + single sentinel file I/O. No network, no state. Called at the
end of every market cycle in cvd_5min_bot.main(); halts the main loop when
the gate reaches a terminal state (PASS, KILL, or INCONCLUSIVE).

Gate thresholds are frozen by the design spec
`docs/superpowers/specs/2026-04-10-stink-paper-validation-design.md`.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import comb

import pandas as pd

# ── Gate thresholds (frozen per spec) ──────────────────────────────────
MIN_N_FOR_DECISION = 60
KILL_N = 30
KILL_WINRATE = 0.45
PASS_WINRATE = 0.58
PASS_P_VALUE = 0.05
PASS_EV_PER_TRADE = 0.20
FEE_RATE = 0.0315  # Polymarket dynamic fees per CLAUDE.md §Domain Rules #4
SENTINEL_PATH = "data/paper_gate_status.json"


@dataclass
class GateStatus:
    status: str          # "PASS" | "KILL" | "INCONCLUSIVE" | "CONTINUE"
    reason: str
    n: int
    winrate: float
    p_value: float
    ev_per_trade: float
    evaluated_at: str    # ISO timestamp


def one_sided_binomial_p(wins: int, n: int, p: float = 0.5) -> float:
    """Return P(X >= wins | n, p) using math.comb. No scipy dependency."""
    if n <= 0 or wins < 0 or wins > n:
        return 1.0
    return sum(
        comb(n, k) * (p ** k) * ((1 - p) ** (n - k))
        for k in range(wins, n + 1)
    )


def compute_pnl_per_trade(row: pd.Series) -> float:
    """Net PnL for one filled+settled stink trade.

    Payout-side fee model: fees are charged as FEE_RATE of the winning
    payout notional. Losers pay no additional fee (you just lose the
    entry cost). This is a slight over-estimate of fees on winning trades
    versus entry-side models, making the PASS threshold slightly stricter.
    """
    payout = 1.0 if row["signal_correct_polymarket"] == "YES" else 0.0
    shares = float(row["shares"])
    entry = float(row["stink_price"])
    gross = shares * (payout - entry)
    fees = shares * payout * FEE_RATE
    return gross - fees


def evaluate(df: pd.DataFrame) -> GateStatus:
    """Evaluate the gate over all filled + settled trades in the DataFrame.

    Only rows where `filled == True` AND `signal_correct_polymarket` is
    "YES" or "NO" contribute to n. PENDING, UNKNOWN, or unfilled rows are
    excluded.
    """
    if len(df) == 0:
        return GateStatus("CONTINUE", "no trades logged yet",
                          0, 0.0, 1.0, 0.0, _now_iso())

    settled = df[
        df["filled"].astype(bool) &
        df["signal_correct_polymarket"].isin(["YES", "NO"])
    ]
    n = len(settled)
    if n == 0:
        return GateStatus("CONTINUE", "no settled trades yet",
                          0, 0.0, 1.0, 0.0, _now_iso())

    wins = int((settled["signal_correct_polymarket"] == "YES").sum())
    winrate = wins / n
    p_value = one_sided_binomial_p(wins, n, 0.5)
    ev = float(settled.apply(compute_pnl_per_trade, axis=1).mean())

    # KILL gate — fast exit on failure.
    if n >= KILL_N and winrate < KILL_WINRATE:
        return GateStatus(
            "KILL",
            f"n={n} ≥ {KILL_N} AND winrate={winrate:.1%} < {KILL_WINRATE:.0%}",
            n, winrate, p_value, ev, _now_iso(),
        )

    # PASS gate — all four conditions.
    if (n >= MIN_N_FOR_DECISION
            and winrate >= PASS_WINRATE
            and p_value <= PASS_P_VALUE
            and ev >= PASS_EV_PER_TRADE):
        return GateStatus(
            "PASS",
            f"all gates met: n={n}, winrate={winrate:.1%}, "
            f"p={p_value:.4f}, EV=${ev:.2f}",
            n, winrate, p_value, ev, _now_iso(),
        )

    # INCONCLUSIVE — sufficient n, but one or more PASS conditions unmet.
    if n >= MIN_N_FOR_DECISION:
        return GateStatus(
            "INCONCLUSIVE",
            f"n={n} sufficient but gates unmet: "
            f"winrate={winrate:.1%}, p={p_value:.4f}, EV=${ev:.2f}",
            n, winrate, p_value, ev, _now_iso(),
        )

    return GateStatus(
        "CONTINUE",
        f"accumulating: n={n}, winrate={winrate:.1%}",
        n, winrate, p_value, ev, _now_iso(),
    )


def write_sentinel(status: GateStatus, path: str = SENTINEL_PATH) -> None:
    """Write the gate status to a JSON sentinel file. Overwrites existing."""
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(asdict(status), f, indent=2)


def read_sentinel(path: str = SENTINEL_PATH) -> GateStatus | None:
    """Read the gate status from the sentinel file, or None if missing/corrupt."""
    try:
        with open(path) as f:
            data = json.load(f)
        return GateStatus(**data)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_paper_gate.py -v`
Expected: PASS all tests (should be ~19 tests).

- [ ] **Step 5: Run the full prior test suite to confirm no regressions**

Run: `pytest tests/ -q`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add paper_gate.py tests/unit/test_paper_gate.py
git commit -m "feat: add paper_gate module with statistical PASS/KILL/INCONCLUSIVE evaluation"
```

---

## Task 4: get_market_info returns condition_id

**Files:**
- Modify: `cvd_5min_bot.py:740-809` (`get_market_info` function)
- Test: unit test embedded in `tests/unit/test_paper_mode.py` (added in Task 5 schema migration; or add a small check here)

Since `get_market_info` makes a live HTTP call and existing tests mock it at the call-site level, we add the `condition_id` field as a pure dict-key addition with zero behavior change on existing callers.

- [ ] **Step 1: Inspect the current return dict in `cvd_5min_bot.py` line 797-804**

Current code:
```python
            return {
                "market_id": market_id,
                "up_token_id": tokens[up_idx],
                "down_token_id": tokens[down_idx],
                "question": question,
                "slug": market_slug,
                "neg_risk": neg_risk,
            }
```

- [ ] **Step 2: Add `condition_id` extraction and field**

Edit `cvd_5min_bot.py`. Right after line 793 (`question = event.get(...)`) and before the `print(colored(f"   ✅ Found market: ..."))` line, insert:

```python
            condition_id = market.get("conditionId", "")
```

Then update the returned dict (lines 797-804) to include it:

```python
            return {
                "market_id": market_id,
                "condition_id": condition_id,
                "up_token_id": tokens[up_idx],
                "down_token_id": tokens[down_idx],
                "question": question,
                "slug": market_slug,
                "neg_risk": neg_risk,
            }
```

- [ ] **Step 3: Run the full prior test suite to confirm no regressions**

Run: `pytest tests/ -q`
Expected: All tests pass. Existing code paths that access `market_info["slug"]` etc. are unchanged.

- [ ] **Step 4: Commit**

```bash
git add cvd_5min_bot.py
git commit -m "feat: add condition_id to get_market_info return dict"
```

---

## Task 5: Extend log_paper_signal CSV schema

**Files:**
- Modify: `cvd_5min_bot.py:921-948` (`log_paper_signal`)
- Modify: `cvd_5min_bot.py:995-1032` (`print_paper_summary`)
- Modify: `cvd_5min_bot.py:1769-1779` (call site in `CVDStinkBot.run_market_cycle`)
- Modify: `tests/unit/test_paper_mode.py` — rewrite `TestLogPaperSignal` and `TestPrintPaperSummary` for new schema. `TestResolvePaperOutcome` is left as-is here and rewritten in Task 6.

- [ ] **Step 1: Rewrite `TestLogPaperSignal` tests for new schema**

Replace the body of the `TestLogPaperSignal` class in `tests/unit/test_paper_mode.py` (lines 30-79) with:

```python
class TestLogPaperSignal:
    def test_creates_csv_with_new_schema_columns(self):
        bot.log_paper_signal(
            market_ts=1000000,
            signal_type="BULLISH_DIV",
            direction="UP",
            detail="test signal",
            stink_price=0.45,
            btc_price_at_signal=84000.0,
            market_slug="btc-updown-5m-1000000",
            condition_id="0xabc",
            shares=11,
            direction_original="UP",
            invert_flag=False,
        )
        assert os.path.exists(bot.PAPER_LOG_FILE)
        df = pd.read_csv(bot.PAPER_LOG_FILE)
        assert len(df) == 1
        expected_cols = {
            "timestamp", "market_ts", "market_slug", "condition_id",
            "signal_type", "direction", "direction_original", "invert_flag",
            "detail", "stink_price", "shares",
            "btc_price_at_signal", "btc_price_at_close",
            "outcome_binance_perp", "outcome_binance_spot", "outcome_polymarket",
            "signal_correct_binance_perp", "signal_correct_binance_spot",
            "signal_correct_polymarket",
            "price_change_pct", "filled",
        }
        assert set(df.columns) == expected_cols

    def test_values_stored_correctly(self):
        bot.log_paper_signal(
            market_ts=1000000,
            signal_type="STRONG_BEAR",
            direction="UP",              # flipped from DOWN
            detail="strong selling",
            stink_price=0.52,
            btc_price_at_signal=85000.5,
            market_slug="btc-updown-5m-1000000",
            condition_id="0xdeadbeef",
            shares=9,
            direction_original="DOWN",
            invert_flag=True,
        )
        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["market_ts"] == 1000000
        assert row["signal_type"] == "STRONG_BEAR"
        assert row["direction"] == "UP"
        assert row["direction_original"] == "DOWN"
        assert bool(row["invert_flag"]) is True
        assert row["stink_price"] == 0.52
        assert row["shares"] == 9
        assert row["condition_id"] == "0xdeadbeef"
        assert row["btc_price_at_signal"] == 85000.5
        assert row["btc_price_at_close"] == 0.0
        assert bool(row["filled"]) is False
        # Outcome columns empty initially
        for col in ("outcome_binance_perp", "outcome_binance_spot", "outcome_polymarket",
                    "signal_correct_binance_perp", "signal_correct_binance_spot",
                    "signal_correct_polymarket"):
            val = row[col]
            assert pd.isna(val) or val == ""

    def test_appends_to_existing(self):
        bot.log_paper_signal(
            market_ts=1000, signal_type="BULLISH_DIV", direction="UP",
            detail="first", stink_price=0.4, btc_price_at_signal=84000.0,
            market_slug="slug-1", condition_id="0x1", shares=12,
            direction_original="UP", invert_flag=False,
        )
        bot.log_paper_signal(
            market_ts=2000, signal_type="BEARISH_DIV", direction="DOWN",
            detail="second", stink_price=0.5, btc_price_at_signal=84100.0,
            market_slug="slug-2", condition_id="0x2", shares=10,
            direction_original="DOWN", invert_flag=False,
        )
        df = pd.read_csv(bot.PAPER_LOG_FILE)
        assert len(df) == 2
        assert df.iloc[0]["signal_type"] == "BULLISH_DIV"
        assert df.iloc[1]["signal_type"] == "BEARISH_DIV"
```

- [ ] **Step 2: Rewrite `TestPrintPaperSummary` for new schema**

Replace the body of the `TestPrintPaperSummary` class (lines 161-176) with:

```python
class TestPrintPaperSummary:
    def test_no_file_no_crash(self, capsys):
        bot.print_paper_summary()
        out = capsys.readouterr().out
        assert "No paper trades" in out

    def test_with_resolved_data(self, capsys, monkeypatch):
        # Write two rows directly — one YES, one NO — with new-schema columns.
        df = pd.DataFrame([
            {
                "timestamp": "2026-04-10T12:00:00",
                "market_ts": 1000, "market_slug": "s1", "condition_id": "0x1",
                "signal_type": "BULLISH_DIV", "direction": "UP",
                "direction_original": "UP", "invert_flag": False,
                "detail": "t1", "stink_price": 0.4, "shares": 12,
                "btc_price_at_signal": 84000.0, "btc_price_at_close": 84100.0,
                "outcome_binance_perp": "UP", "outcome_binance_spot": "UP",
                "outcome_polymarket": "UP",
                "signal_correct_binance_perp": "YES",
                "signal_correct_binance_spot": "YES",
                "signal_correct_polymarket": "YES",
                "price_change_pct": 0.12, "filled": True,
            },
            {
                "timestamp": "2026-04-10T12:05:00",
                "market_ts": 2000, "market_slug": "s2", "condition_id": "0x2",
                "signal_type": "BEARISH_DIV", "direction": "DOWN",
                "direction_original": "DOWN", "invert_flag": False,
                "detail": "t2", "stink_price": 0.5, "shares": 10,
                "btc_price_at_signal": 84100.0, "btc_price_at_close": 84200.0,
                "outcome_binance_perp": "UP", "outcome_binance_spot": "UP",
                "outcome_polymarket": "UP",
                "signal_correct_binance_perp": "NO",
                "signal_correct_binance_spot": "NO",
                "signal_correct_polymarket": "NO",
                "price_change_pct": 0.12, "filled": True,
            },
        ])
        df.to_csv(bot.PAPER_LOG_FILE, index=False)

        bot.print_paper_summary()
        out = capsys.readouterr().out
        assert "Accuracy" in out
        assert "50.0%" in out
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/test_paper_mode.py::TestLogPaperSignal tests/unit/test_paper_mode.py::TestPrintPaperSummary -v`
Expected: FAIL — `log_paper_signal` doesn't accept new params, CSV schema mismatch, `print_paper_summary` still reads old columns.

- [ ] **Step 4: Rewrite `log_paper_signal` in `cvd_5min_bot.py`**

Replace the entire `log_paper_signal` function body (lines 921-948) with:

```python
def log_paper_signal(market_ts: int, signal_type: str, direction: str, detail: str,
                     stink_price: float, btc_price_at_signal: float, market_slug: str,
                     condition_id: str = "", shares: int = 0,
                     direction_original: str = "", invert_flag: bool = False):
    """Log paper trade signal for later outcome resolution.

    Writes one row with the new schema: three parallel outcome columns
    (perp / spot / polymarket), filled flag, audit fields for direction
    flip, and condition_id needed by fetch_polymarket_resolution.
    """
    os.makedirs(DATA_DIR, exist_ok=True)

    new_row = pd.DataFrame([{
        "timestamp": datetime.now().isoformat(),
        "market_ts": market_ts,
        "market_slug": market_slug,
        "condition_id": condition_id,
        "signal_type": signal_type,
        "direction": direction,
        "direction_original": direction_original or direction,
        "invert_flag": bool(invert_flag),
        "detail": detail,
        "stink_price": round(stink_price, 4),
        "shares": int(shares),
        "btc_price_at_signal": round(btc_price_at_signal, 2),
        "btc_price_at_close": 0.0,
        "outcome_binance_perp": "",
        "outcome_binance_spot": "",
        "outcome_polymarket": "",
        "signal_correct_binance_perp": "",
        "signal_correct_binance_spot": "",
        "signal_correct_polymarket": "",
        "price_change_pct": 0.0,
        "filled": False,
    }])

    if os.path.exists(PAPER_LOG_FILE):
        existing = pd.read_csv(PAPER_LOG_FILE)
        df = pd.concat([existing, new_row], ignore_index=True)
    else:
        df = new_row

    df.to_csv(PAPER_LOG_FILE, index=False)
    print(colored("   📝 Paper signal logged", "green"))
```

- [ ] **Step 5: Update `print_paper_summary` in `cvd_5min_bot.py`**

Replace the entire `print_paper_summary` function body (lines 995-1031) with:

```python
def print_paper_summary():
    """Print accuracy stats from paper trading log (reads Polymarket column)."""
    if not os.path.exists(PAPER_LOG_FILE):
        print(colored("   📝 No paper trades yet", "yellow"))
        return

    df = pd.read_csv(PAPER_LOG_FILE)
    # Defensive: tolerate a pre-schema-migration file
    if "signal_correct_polymarket" not in df.columns:
        print(colored("   📝 Paper log uses old schema, skipping summary", "yellow"))
        return

    resolved = df[df["signal_correct_polymarket"].isin(["YES", "NO"])]
    if len(resolved) == 0:
        print(colored(f"   📝 {len(df)} paper signals logged, none resolved yet", "yellow"))
        return

    total = len(resolved)
    correct = len(resolved[resolved["signal_correct_polymarket"] == "YES"])
    accuracy = (correct / total) * 100

    print(colored(f"\n   📝 Paper Trading Summary (Polymarket-resolved):", "cyan", attrs=["bold"]))
    print(colored(f"      Signals: {total} | Correct: {correct} | Wrong: {total - correct}", "white"))
    print(colored(f"      Accuracy: {accuracy:.1f}%", "green" if accuracy > 50 else "red", attrs=["bold"]))

    # Breakdown by signal type
    for sig_type in resolved["signal_type"].unique():
        subset = resolved[resolved["signal_type"] == sig_type]
        sub_correct = len(subset[subset["signal_correct_polymarket"] == "YES"])
        sub_total = len(subset)
        sub_acc = (sub_correct / sub_total) * 100 if sub_total > 0 else 0
        print(colored(f"      {sig_type:<14}: {sub_correct}/{sub_total} ({sub_acc:.0f}%)", "white"))

    # Average price change for correct vs wrong (perp-derived price_change_pct)
    correct_df = resolved[resolved["signal_correct_polymarket"] == "YES"]
    wrong_df = resolved[resolved["signal_correct_polymarket"] == "NO"]
    if len(correct_df) > 0:
        avg_correct = correct_df["price_change_pct"].abs().mean()
        print(colored(f"      Avg move (correct): {avg_correct:.3f}%", "white"))
    if len(wrong_df) > 0:
        avg_wrong = wrong_df["price_change_pct"].abs().mean()
        print(colored(f"      Avg move (wrong):   {avg_wrong:.3f}%", "white"))
```

- [ ] **Step 6: Update the call site in `CVDStinkBot.run_market_cycle`**

Edit `cvd_5min_bot.py` lines 1769-1779. Replace the existing `log_paper_signal(...)` call with:

```python
                    # Log paper signal for outcome tracking
                    if PAPER_MODE:
                        # direction_original = pre-flip direction from the raw signal.
                        # When CVD_INVERT_SIGNAL=true, self.signal_direction is already
                        # flipped — we infer the original by flipping it back for the
                        # audit field.
                        if CVD_INVERT_SIGNAL and self.signal_direction == "UP":
                            direction_original = "DOWN"
                        elif CVD_INVERT_SIGNAL and self.signal_direction == "DOWN":
                            direction_original = "UP"
                        else:
                            direction_original = self.signal_direction or ""

                        log_paper_signal(
                            market_ts=market_ts,
                            signal_type=self.signal_type,
                            direction=self.signal_direction,
                            detail=self.signal_detail,
                            stink_price=self.stink_bid_price,
                            btc_price_at_signal=self.feed.get_last_price(),
                            market_slug=self.market_info.get("slug", ""),
                            condition_id=self.market_info.get("condition_id", ""),
                            shares=self.stink_bid_shares,
                            direction_original=direction_original,
                            invert_flag=CVD_INVERT_SIGNAL,
                        )
```

- [ ] **Step 7: Run the new schema tests to verify they pass**

Run: `pytest tests/unit/test_paper_mode.py::TestLogPaperSignal tests/unit/test_paper_mode.py::TestPrintPaperSummary -v`
Expected: PASS.

Note: `TestResolvePaperOutcome` will FAIL at this step because `resolve_paper_outcome` still uses the old schema. That is rewritten in Task 6; we tolerate red on those tests between commits.

- [ ] **Step 8: Commit**

```bash
git add cvd_5min_bot.py tests/unit/test_paper_mode.py
git commit -m "refactor: expand paper_trades CSV schema for dual truth sources"
```

---

## Task 6: Rewrite resolve_paper_outcome with dual truth sources

**Files:**
- Modify: `cvd_5min_bot.py:951-992` (`resolve_paper_outcome`)
- Modify: `cvd_5min_bot.py:1667-1668` (call site)
- Modify: `tests/unit/test_paper_mode.py` — rewrite `TestResolvePaperOutcome` class

- [ ] **Step 1: Rewrite `TestResolvePaperOutcome` for new behavior**

Replace the body of the `TestResolvePaperOutcome` class in `tests/unit/test_paper_mode.py` (was lines 84-156) with:

```python
class TestResolvePaperOutcome:
    def _seed(self, market_ts, direction, btc_price, condition_id="0xabc", shares=10,
              stink_price=0.45):
        bot.log_paper_signal(
            market_ts=market_ts,
            signal_type="BULLISH_DIV",
            direction=direction,
            detail="test",
            stink_price=stink_price,
            btc_price_at_signal=btc_price,
            market_slug="slug",
            condition_id=condition_id,
            shares=shares,
            direction_original=direction,
            invert_flag=False,
        )

    def test_resolves_all_three_outcome_columns_up(self, monkeypatch):
        """BTC moves up, Polymarket returns UP, spot returns 84100 — all three agree."""
        self._seed(1000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 84105.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "UP",
        )

        bot.resolve_paper_outcome(1000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["outcome_binance_perp"] == "UP"
        assert row["signal_correct_binance_perp"] == "YES"
        assert row["outcome_binance_spot"] == "UP"
        assert row["signal_correct_binance_spot"] == "YES"
        assert row["outcome_polymarket"] == "UP"
        assert row["signal_correct_polymarket"] == "YES"
        assert bool(row["filled"]) is True
        assert row["btc_price_at_close"] == 84100.0

    def test_polymarket_pending_keeps_row_reevaluable(self, monkeypatch):
        """If Polymarket returns PENDING, the row is logged and re-runnable."""
        self._seed(2000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 84110.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "PENDING",
        )

        bot.resolve_paper_outcome(2000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["outcome_polymarket"] == "PENDING"
        assert row["signal_correct_polymarket"] == "PENDING"
        assert row["outcome_binance_perp"] == "UP"  # other sources still filled in
        assert row["outcome_binance_spot"] == "UP"

    def test_polymarket_none_writes_unknown(self, monkeypatch):
        """Transient HTTP failure → UNKNOWN, no retry."""
        self._seed(3000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 84110.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: None,
        )

        bot.resolve_paper_outcome(3000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["outcome_polymarket"] == "UNKNOWN"
        assert row["signal_correct_polymarket"] == "UNKNOWN"

    def test_binance_spot_none_writes_unknown_for_spot_only(self, monkeypatch):
        """Spot fetch fails but Polymarket still works — Polymarket drives gate."""
        self._seed(4000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: None,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "UP",
        )

        bot.resolve_paper_outcome(4000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["outcome_binance_spot"] == "UNKNOWN"
        assert row["signal_correct_binance_spot"] == "UNKNOWN"
        assert row["outcome_polymarket"] == "UP"
        assert row["signal_correct_polymarket"] == "YES"

    def test_wrong_direction_marked_no(self, monkeypatch):
        """Predicted UP, Polymarket says DOWN → signal_correct_polymarket = NO."""
        self._seed(5000, "UP", 84000.0)
        feed = FakeFeed(83900.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 83890.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "DOWN",
        )

        bot.resolve_paper_outcome(5000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["signal_correct_polymarket"] == "NO"

    def test_filled_flag_propagates(self, monkeypatch):
        """When the bot passes filled=False, the row marks filled=False."""
        self._seed(6000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 84105.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "UP",
        )

        bot.resolve_paper_outcome(6000, feed, filled=False)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert bool(row["filled"]) is False

    def test_pending_retries_next_cycle(self, monkeypatch):
        """A row with PENDING signal_correct_polymarket is re-resolved on next call."""
        self._seed(7000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        # First call: Polymarket returns PENDING
        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 84105.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "PENDING",
        )
        bot.resolve_paper_outcome(7000, feed, filled=True)

        # Second call (next cycle): Polymarket now resolved
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "UP",
        )
        bot.resolve_paper_outcome(7000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["outcome_polymarket"] == "UP"
        assert row["signal_correct_polymarket"] == "YES"

    def test_skips_already_resolved_non_pending(self, monkeypatch):
        """A row with signal_correct_polymarket in {YES,NO} is not re-touched."""
        self._seed(8000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 84105.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "UP",
        )
        bot.resolve_paper_outcome(8000, feed, filled=True)

        # A second call — even with a different Polymarket answer — must NOT overwrite
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "DOWN",
        )
        bot.resolve_paper_outcome(8000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        assert df.iloc[0]["outcome_polymarket"] == "UP"
        assert df.iloc[0]["signal_correct_polymarket"] == "YES"

    def test_no_file_is_noop(self):
        feed = FakeFeed(84000.0)
        bot.resolve_paper_outcome(999, feed, filled=False)  # no crash

    def test_zero_feed_price_skips(self, monkeypatch):
        self._seed(9000, "UP", 84000.0)
        feed = FakeFeed(0.0)
        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: None,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: None,
        )
        bot.resolve_paper_outcome(9000, feed, filled=False)
        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        # Nothing resolvable — all three outcome columns should be UNKNOWN or blank
        assert row["outcome_binance_perp"] in ("", "UNKNOWN") or pd.isna(row["outcome_binance_perp"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_paper_mode.py::TestResolvePaperOutcome -v`
Expected: FAIL — current `resolve_paper_outcome` doesn't accept `filled` kwarg and doesn't call truth_sources.

- [ ] **Step 3: Rewrite `resolve_paper_outcome` in `cvd_5min_bot.py`**

Replace the entire `resolve_paper_outcome` function body (lines 951-992) with:

```python
def resolve_paper_outcome(market_ts: int, feed: BinanceCVDFeed, filled: bool = False):
    """After a market cycle ends, write all three outcome columns.

    Sources:
      1. Binance perp (feed.get_last_price()) — continuity with old schema
      2. Binance spot (truth_sources.fetch_binance_spot_price)
      3. Polymarket Gamma resolvedPrice (truth_sources.fetch_polymarket_resolution)

    Only rows where signal_correct_polymarket is "" or "PENDING" are
    touched — already-resolved YES/NO rows are immutable. Passing
    filled=True marks the row as a filled stink bid (contributes to the
    gate); filled=False keeps the row in the log but excludes it from
    the gate via paper_gate.evaluate().
    """
    import truth_sources

    if not os.path.exists(PAPER_LOG_FILE):
        return

    df = pd.read_csv(PAPER_LOG_FILE)
    # Ensure all new-schema columns exist and are string-typed where needed
    for col in ("outcome_binance_perp", "outcome_binance_spot", "outcome_polymarket",
                "signal_correct_binance_perp", "signal_correct_binance_spot",
                "signal_correct_polymarket"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)
    if "filled" not in df.columns:
        df["filled"] = False

    # Re-resolve any row matching this market_ts that is still unresolved
    # OR whose Polymarket state is PENDING.
    mask = (df["market_ts"] == market_ts) & (
        (df["signal_correct_polymarket"].isin(["", "PENDING"]))
    )
    if not mask.any():
        return

    for idx in df[mask].index:
        predicted = df.loc[idx, "direction"]
        btc_at_signal = df.loc[idx, "btc_price_at_signal"]

        # Mark filled status from caller
        df.loc[idx, "filled"] = bool(filled)

        # ── Source 1: Binance perp via feed ──
        btc_now = feed.get_last_price() if feed else 0.0
        if btc_now > 0 and btc_at_signal > 0:
            actual_perp = "UP" if btc_now > btc_at_signal else "DOWN"
            price_change = ((btc_now - btc_at_signal) / btc_at_signal) * 100
            df.loc[idx, "btc_price_at_close"] = round(btc_now, 2)
            df.loc[idx, "outcome_binance_perp"] = actual_perp
            df.loc[idx, "signal_correct_binance_perp"] = "YES" if predicted == actual_perp else "NO"
            df.loc[idx, "price_change_pct"] = round(price_change, 4)
        else:
            df.loc[idx, "outcome_binance_perp"] = "UNKNOWN"
            df.loc[idx, "signal_correct_binance_perp"] = "UNKNOWN"

        # ── Source 2: Binance spot via Klines REST ──
        spot_close = truth_sources.fetch_binance_spot_price(int(market_ts) + 300)
        if spot_close is not None and btc_at_signal > 0:
            actual_spot = "UP" if spot_close > btc_at_signal else "DOWN"
            df.loc[idx, "outcome_binance_spot"] = actual_spot
            df.loc[idx, "signal_correct_binance_spot"] = "YES" if predicted == actual_spot else "NO"
        else:
            df.loc[idx, "outcome_binance_spot"] = "UNKNOWN"
            df.loc[idx, "signal_correct_binance_spot"] = "UNKNOWN"

        # ── Source 3: Polymarket Gamma (primary, drives gate) ──
        condition_id = str(df.loc[idx, "condition_id"] or "")
        if condition_id:
            poly_outcome = truth_sources.fetch_polymarket_resolution(condition_id)
        else:
            poly_outcome = None

        if poly_outcome == "PENDING":
            df.loc[idx, "outcome_polymarket"] = "PENDING"
            df.loc[idx, "signal_correct_polymarket"] = "PENDING"
        elif poly_outcome in ("UP", "DOWN"):
            df.loc[idx, "outcome_polymarket"] = poly_outcome
            df.loc[idx, "signal_correct_polymarket"] = "YES" if predicted == poly_outcome else "NO"
        else:
            df.loc[idx, "outcome_polymarket"] = "UNKNOWN"
            df.loc[idx, "signal_correct_polymarket"] = "UNKNOWN"

        # Terminal console print
        poly_state = df.loc[idx, "signal_correct_polymarket"]
        if poly_state == "YES":
            emoji, color = "✅", "green"
        elif poly_state == "NO":
            emoji, color = "❌", "red"
        elif poly_state == "PENDING":
            emoji, color = "⏳", "yellow"
        else:
            emoji, color = "❓", "white"

        print(colored(
            f"   {emoji} PAPER RESULT: Predicted {predicted} | "
            f"Polymarket={df.loc[idx, 'outcome_polymarket']} "
            f"Spot={df.loc[idx, 'outcome_binance_spot']} "
            f"Perp={df.loc[idx, 'outcome_binance_perp']}",
            color,
        ))

    df.to_csv(PAPER_LOG_FILE, index=False)
```

- [ ] **Step 4: Update the call site in `CVDStinkBot.run_market_cycle`**

Edit `cvd_5min_bot.py` line 1667-1668. Change:

```python
                # Resolve paper outcome
                if PAPER_MODE and self.signal_fired:
                    resolve_paper_outcome(market_ts, self.feed)
```

to:

```python
                # Resolve paper outcome — pass filled state so the gate
                # excludes unfilled stink bids.
                if PAPER_MODE and self.signal_fired:
                    resolve_paper_outcome(market_ts, self.feed, filled=self.poly_filled)
```

- [ ] **Step 5: Run all paper-mode tests to verify they pass**

Run: `pytest tests/unit/test_paper_mode.py -v`
Expected: PASS (all classes).

- [ ] **Step 6: Run the full test suite to confirm no regressions**

Run: `pytest tests/ -q`
Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add cvd_5min_bot.py tests/unit/test_paper_mode.py
git commit -m "feat: resolve_paper_outcome writes three parallel outcome columns via truth_sources"
```

---

## Task 7: Main-loop sentinel integration

**Files:**
- Modify: `cvd_5min_bot.py` — main() near line 2207 (startup check) and line 2262 (end-of-cycle evaluate)
- Modify: `cvd_5min_bot.py` — main() config banner near line 2182

- [ ] **Step 1: Add startup banner line for CVD_INVERT_SIGNAL**

Edit `cvd_5min_bot.py` after the existing `MM_STOP_QUOTING_SEC` banner line at line 2205 and before the `if PAPER_MODE:` block at line 2207. Insert:

```python
    # Stink paper validation harness state
    if STRATEGY == "stink":
        flip_state = "ACTIVE — signals will be flipped" if CVD_INVERT_SIGNAL else "inactive"
        print(colored(f"\n   🔁 CVD_INVERT_SIGNAL       = {CVD_INVERT_SIGNAL} ({flip_state})",
                      "magenta" if CVD_INVERT_SIGNAL else "white"), flush=True)
```

- [ ] **Step 2: Add the startup sentinel check**

Edit `cvd_5min_bot.py` inside `main()`. Find the section right after `clob_client = init_clob_client()` (around line 2218) and before the Hyperliquid HL position block. Insert:

```python
    # Paper validation gate — refuse to start if prior run left a KILL sentinel
    if STRATEGY == "stink" and PAPER_MODE:
        import paper_gate
        prior = paper_gate.read_sentinel()
        if prior is not None and prior.status == "KILL":
            print(colored(
                f"\n   🛑 REFUSING TO START — previous paper_gate status = KILL\n"
                f"      Reason: {prior.reason}\n"
                f"      n={prior.n}, winrate={prior.winrate:.1%}, evaluated_at={prior.evaluated_at}\n"
                f"      Delete {paper_gate.SENTINEL_PATH} to override, or archive the strategy.",
                "red", attrs=["bold"],
            ), flush=True)
            return
        if prior is not None and prior.status in ("PASS", "INCONCLUSIVE"):
            print(colored(
                f"\n   ℹ️ Previous paper_gate status = {prior.status} "
                f"(n={prior.n}, winrate={prior.winrate:.1%}). "
                f"Starting a new accumulation session.",
                "cyan",
            ), flush=True)
```

- [ ] **Step 3: Add end-of-cycle gate evaluation in the main loop**

Edit `cvd_5min_bot.py` inside `main()`'s `while True:` loop (around line 2262). After the existing `bot.run_market_cycle(market_ts)` call and before the `except KeyboardInterrupt` clause, insert the gate evaluation. The updated try block should look like:

```python
        try:
            market_ts = get_current_market_timestamp()
            time_remaining = get_time_remaining(market_ts)

            # If too little time left, wait for next market
            if time_remaining < MIN_TIME_LEFT + 30:
                next_ts = market_ts + MARKET_DURATION
                wait_time = next_ts - int(time.time())
                if wait_time > 0:
                    next_dt = datetime.fromtimestamp(next_ts, tz=ET)
                    print(colored(
                        f"\n   🌙 Waiting {wait_time}s for next market "
                        f"({next_dt.strftime('%I:%M:%S%p ET')})...",
                        "yellow",
                    ))
                    time.sleep(wait_time + 1)
                market_ts = get_current_market_timestamp()

            bot.reset()
            bot.run_market_cycle(market_ts)

            # Paper validation gate — evaluate after every cycle
            if STRATEGY == "stink" and PAPER_MODE:
                import paper_gate
                if os.path.exists(PAPER_LOG_FILE):
                    gate_df = pd.read_csv(PAPER_LOG_FILE)
                    status = paper_gate.evaluate(gate_df)
                    paper_gate.write_sentinel(status)
                    if status.status in ("PASS", "KILL", "INCONCLUSIVE"):
                        banner_color = {"PASS": "green", "KILL": "red",
                                        "INCONCLUSIVE": "yellow"}[status.status]
                        print(colored(
                            f"\n{'=' * 70}\n"
                            f"   🎯 PAPER GATE TERMINAL: {status.status}\n"
                            f"   Reason: {status.reason}\n"
                            f"   n={status.n} | winrate={status.winrate:.1%} | "
                            f"p={status.p_value:.4f} | EV/trade=${status.ev_per_trade:.2f}\n"
                            f"   Sentinel written to {paper_gate.SENTINEL_PATH}\n"
                            f"{'=' * 70}\n",
                            banner_color, attrs=["bold"],
                        ), flush=True)
                        bot.cancel_orders()
                        feed.stop()
                        return
        except KeyboardInterrupt:
```

(The `except KeyboardInterrupt:` block is unchanged.)

- [ ] **Step 4: Smoke-test the startup banner path by importing main in a subshell**

Run: `PAPER_MODE=true STRATEGY=stink CVD_INVERT_SIGNAL=true python -c "import cvd_5min_bot; print('import ok'); print('CVD_INVERT_SIGNAL=', cvd_5min_bot.CVD_INVERT_SIGNAL)"`

Expected output:
```
import ok
CVD_INVERT_SIGNAL= True
```

- [ ] **Step 5: Run the full test suite**

Run: `pytest tests/ -q`
Expected: All tests pass (no integration test exercises main() yet; Task 8 adds that).

- [ ] **Step 6: Commit**

```bash
git add cvd_5min_bot.py
git commit -m "feat: wire paper_gate sentinel into cvd_5min_bot main loop"
```

---

## Task 8: Integration test — end-to-end paper flow

**Files:**
- Create: `tests/integration/test_paper_flow.py`

- [ ] **Step 1: Write the integration test**

Create `tests/integration/test_paper_flow.py`:

```python
"""Integration test: synthetic paper_trades.csv → paper_gate.evaluate → sentinel."""
import json
from datetime import datetime

import pandas as pd
import pytest

import paper_gate


def _fake_row(signal_correct_polymarket, market_ts, shares=10, stink_price=0.40):
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "market_ts": market_ts,
        "market_slug": f"btc-updown-5m-{market_ts}",
        "condition_id": f"0x{market_ts:08x}",
        "signal_type": "BULLISH_DIV",
        "direction": "UP",
        "direction_original": "DOWN",
        "invert_flag": True,
        "detail": "synth",
        "stink_price": stink_price,
        "shares": shares,
        "btc_price_at_signal": 84000.0,
        "btc_price_at_close": 84100.0,
        "outcome_binance_perp": "UP",
        "outcome_binance_spot": "UP",
        "outcome_polymarket": "UP" if signal_correct_polymarket == "YES" else "DOWN",
        "signal_correct_binance_perp": signal_correct_polymarket,
        "signal_correct_binance_spot": signal_correct_polymarket,
        "signal_correct_polymarket": signal_correct_polymarket,
        "price_change_pct": 0.12,
        "filled": True,
    }


def test_synthetic_pass_flow(tmp_path):
    """70 trades, 50 wins (71.4% winrate) at stink_price 0.40 → PASS.

    Expected metrics:
      wins=50, n=70, winrate=0.714
      EV per win = 10 * (1 - 0.40) - 10 * 1 * 0.0315 = 6.0 - 0.315 = 5.685
      EV per loss = 10 * (0 - 0.40) - 0 = -4.0
      Mean EV = (50*5.685 + 20*(-4.0)) / 70 = (284.25 - 80) / 70 ≈ 2.918
      p-value ≈ 0.00018 (very significant)
    All four gates met → PASS.
    """
    rows = [_fake_row("YES", market_ts=1_000_000 + i * 300) for i in range(50)]
    rows += [_fake_row("NO", market_ts=1_020_000 + i * 300) for i in range(20)]
    df = pd.DataFrame(rows)

    sentinel_path = str(tmp_path / "paper_gate_status.json")
    status = paper_gate.evaluate(df)
    paper_gate.write_sentinel(status, sentinel_path)

    assert status.status == "PASS"
    assert status.n == 70
    assert status.winrate == pytest.approx(50 / 70, abs=1e-9)
    assert status.p_value < 0.05
    assert status.ev_per_trade > 0.20

    # Sentinel round-trip
    with open(sentinel_path) as f:
        data = json.load(f)
    assert data["status"] == "PASS"
    assert data["n"] == 70


def test_synthetic_kill_flow(tmp_path):
    """40 trades, 8 wins (20% winrate) → KILL."""
    rows = [_fake_row("YES", market_ts=2_000_000 + i * 300) for i in range(8)]
    rows += [_fake_row("NO", market_ts=2_020_000 + i * 300) for i in range(32)]
    df = pd.DataFrame(rows)

    status = paper_gate.evaluate(df)
    assert status.status == "KILL"
    assert status.n == 40
    assert status.winrate == pytest.approx(8 / 40, abs=1e-9)


def test_synthetic_inconclusive_flow(tmp_path):
    """n=65, winrate=0.55 (below PASS 0.58) → INCONCLUSIVE."""
    rows = [_fake_row("YES", market_ts=3_000_000 + i * 300) for i in range(36)]
    rows += [_fake_row("NO", market_ts=3_030_000 + i * 300) for i in range(29)]
    df = pd.DataFrame(rows)

    status = paper_gate.evaluate(df)
    assert status.status == "INCONCLUSIVE"
    assert status.n == 65
```

- [ ] **Step 2: Run the integration test**

Run: `pytest tests/integration/test_paper_flow.py -v`
Expected: PASS 3/3.

- [ ] **Step 3: Run the full test suite**

Run: `pytest tests/ -q`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_paper_flow.py
git commit -m "test: integration test for paper_gate synthetic PASS/KILL/INCONCLUSIVE flows"
```

---

## Task 9: Smoke script — validate gate on MM baseline CSV

**Files:**
- Create: `scripts/smoke_paper_gate.py`

This is a pre-flight operator tool, not a pytest test. It reconstructs per-cycle alignment from the archived MM baseline CSV and runs it through `paper_gate.evaluate` to verify the gate would have correctly KILLED the MM strategy. It's a sanity check that the thresholds are realistic.

- [ ] **Step 1: Verify the baseline CSV exists**

Run: `ls -la data/mm_paper_trades_baseline_2026-04-10.csv`
Expected: file exists, ~820 rows.

- [ ] **Step 2: Create the smoke script**

Create `scripts/smoke_paper_gate.py`:

```python
"""Pre-flight smoke test: run paper_gate on the archived MM baseline CSV.

The MM baseline has 64 settled cycles with 17 GOOD alignment (26.6%, p<10⁻⁴).
When we reshape each cycle as a fake stink trade, we expect paper_gate to
output KILL well before n=64. If it doesn't, the thresholds are wrong and
the spec needs adjustment before running real paper trades.

Usage:
    python scripts/smoke_paper_gate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

# Make project root importable when running from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtest  # noqa: E402 — existing harness reused for cycle iteration
import paper_gate  # noqa: E402

BASELINE = "data/mm_paper_trades_baseline_2026-04-10.csv"


def reshape_mm_to_stink_trades(mm_csv: str) -> pd.DataFrame:
    """Convert MM baseline CSV rows into paper_gate-shaped rows.

    Each MM cycle becomes one fake stink trade:
      - shares = 10, stink_price = 0.40 (fixed, representative)
      - signal_correct_polymarket = "YES" if cycle's inventory sign matched
        the cycle outcome (via backtest._classify_alignment), "NO" if mismatched,
        excluded if flat.
      - filled = True for all included cycles.

    This is a lossy approximation: MM cycles don't have a per-trade stink
    bid, so PnL math is fake. The point is to test that the gate detects
    the KNOWN bad winrate (26.6%) and outputs KILL.
    """
    df = pd.read_csv(mm_csv)
    rows = []
    for market_ts, cycle_rows in backtest.iter_cycles(df):
        inv = backtest.pre_settle_inventory(cycle_rows)
        outcome = backtest.cycle_outcome(cycle_rows)
        tag = backtest._classify_alignment(inv, outcome)
        if tag == "NA":
            continue
        rows.append({
            "timestamp": str(market_ts),
            "market_ts": market_ts,
            "market_slug": f"btc-updown-5m-{market_ts}",
            "condition_id": "0xsynth",
            "signal_type": "BULLISH_DIV",
            "direction": "UP",
            "direction_original": "UP",
            "invert_flag": False,
            "detail": "reshaped from MM baseline",
            "stink_price": 0.40,
            "shares": 10,
            "btc_price_at_signal": 0.0,
            "btc_price_at_close": 0.0,
            "outcome_binance_perp": "",
            "outcome_binance_spot": "",
            "outcome_polymarket": outcome,
            "signal_correct_binance_perp": "",
            "signal_correct_binance_spot": "",
            "signal_correct_polymarket": "YES" if tag == "GOOD" else "NO",
            "price_change_pct": 0.0,
            "filled": True,
        })
    return pd.DataFrame(rows)


def main() -> int:
    if not Path(BASELINE).exists():
        print(f"ERROR: {BASELINE} not found", flush=True)
        return 1

    df = reshape_mm_to_stink_trades(BASELINE)
    print(f"Reshaped {len(df)} synthetic stink-trade rows from MM baseline", flush=True)

    status = paper_gate.evaluate(df)
    print(
        f"\nPAPER_GATE RESULT on MM baseline:\n"
        f"  status      = {status.status}\n"
        f"  reason      = {status.reason}\n"
        f"  n           = {status.n}\n"
        f"  winrate     = {status.winrate:.1%}\n"
        f"  p_value     = {status.p_value:.6f}\n"
        f"  ev_per_trade= ${status.ev_per_trade:.4f}",
        flush=True,
    )

    if status.status == "KILL":
        print("\n✅ Expected: MM baseline KILLED as intended. Gate thresholds are sane.", flush=True)
        return 0
    else:
        print(
            f"\n❌ Expected KILL, got {status.status}. "
            "Gate thresholds may be miscalibrated — review spec before running real paper.",
            flush=True,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run the smoke script**

Run: `python scripts/smoke_paper_gate.py`
Expected output ends with:
```
PAPER_GATE RESULT on MM baseline:
  status      = KILL
  ...
  winrate     = 26.6% (or similar)
  ...
✅ Expected: MM baseline KILLED as intended. Gate thresholds are sane.
```

If the exit code is non-zero, stop the task and investigate the thresholds.

- [ ] **Step 4: Commit**

```bash
git add scripts/smoke_paper_gate.py
git commit -m "test: smoke script validating paper_gate KILLS the MM baseline"
```

---

## Task 10: .env.example update + final verification

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Check current `.env.example`**

Run: `cat .env.example | grep -i cvd`
Observe which CVD-related lines already exist.

- [ ] **Step 2: Add `CVD_INVERT_SIGNAL` line**

Append to `.env.example` (or insert in the CVD config section if one exists):

```
# Stink paper validation harness — flip direction returned by check_cvd_signal.
# Hypothesis: CVD signal is anti-correlated with Polymarket 5-min BTC outcomes.
# Set to true ONLY when running the paper_gate harness. Default false.
CVD_INVERT_SIGNAL=false
```

- [ ] **Step 3: Run the full test suite one final time**

Run: `pytest tests/ -q`
Expected: All tests pass, including the new tests from Tasks 1–8.

- [ ] **Step 4: Verify the smoke script still works**

Run: `python scripts/smoke_paper_gate.py`
Expected: exit code 0, "MM baseline KILLED as intended."

- [ ] **Step 5: Commit**

```bash
git add .env.example
git commit -m "docs: document CVD_INVERT_SIGNAL env var in .env.example"
```

---

## Post-implementation operator runbook

After all 10 tasks are complete and merged, the operator runs the harness:

1. `git pull` the completed implementation.
2. Run pre-flight smoke: `python scripts/smoke_paper_gate.py` — must print KILL.
3. Ensure `.env` has `PAPER_MODE=true` and `STRATEGY=stink`.
4. Set `CVD_INVERT_SIGNAL=true` in `.env`.
5. Backup or delete `data/paper_trades.csv` if you want a clean accumulation session (the gate reads the whole file, so stale rows will pollute).
6. Delete `data/paper_gate_status.json` if it exists with `KILL` status.
7. `python cvd_5min_bot.py` — bot runs until the gate halts it (PASS, KILL, or INCONCLUSIVE).
8. Read `data/paper_gate_status.json` for the terminal summary and `data/paper_trades.csv` for per-trade detail.
9. Cross-check the three outcome columns (`outcome_polymarket`, `outcome_binance_spot`, `outcome_binance_perp`) — any drift between them indicates a data-source issue worth investigating before interpreting the gate result.
10. On PASS → brainstorm live deployment in a separate spec. On KILL → brainstorm new signal source. On INCONCLUSIVE → brainstorm session-extension criteria.

## Self-review notes

**Spec coverage:**
- Signal flip (Component 1) → Task 1 ✓
- Dual-source outcome resolver (Component 2) → Tasks 4, 5, 6 ✓
- Auto-stop stats gate (Component 3) → Task 3 ✓
- Main loop sentinel integration → Task 7 ✓
- Session-resume (CSV accumulates across runs) → implicit in `log_paper_signal` append + sentinel PASS/INCONCLUSIVE allows re-start ✓
- Operator runbook → end of plan ✓
- Smoke test on MM baseline → Task 9 ✓
- `.env.example` documentation → Task 10 ✓
- All new tests follow TDD (test first, verify red, implement, verify green) ✓

**Placeholder scan:** No TBDs, no "implement later", no "similar to Task N" — every task has full code.

**Type consistency:**
- `GateStatus` fields used identically in Task 3 definition, Task 7 main-loop integration, Task 8 integration test, and Task 9 smoke script.
- CSV schema column names (especially `signal_correct_polymarket`, `filled`, `condition_id`, `direction_original`, `invert_flag`) consistent across Tasks 5, 6, 7, 8, 9.
- `log_paper_signal` kwargs (`condition_id`, `shares`, `direction_original`, `invert_flag`) consistent between Task 5 definition and Task 5 call-site update.
- `resolve_paper_outcome` signature `(market_ts, feed, filled=False)` consistent between Task 6 rewrite and Task 6 call site.
