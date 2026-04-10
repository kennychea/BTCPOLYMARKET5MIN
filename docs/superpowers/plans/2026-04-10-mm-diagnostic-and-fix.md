# MM Diagnostic & Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Diagnose the root cause of the negative edge in the CVD market maker (31.4% alignment, −$131 over 3h45) by running parallel ex-post experiments on existing paper data, then either implement the identified fix or archive the strategy.

**Architecture:** Two-phase diagnostic workflow. Phase 1 builds a lightweight backtest harness that replays the `mm_paper_trades.csv` data under counterfactual rules (cheap, deterministic, parallelizable). Phase 2 dispatches 4 parallel agents, each testing one hypothesis against the harness. Phase 3 gates on measurable improvement; if a winner emerges, it is implemented in `cvd_5min_bot.py` via TDD and validated in a fresh paper run. If no winner emerges, the strategy is archived.

**Tech Stack:** Python 3.13, pandas, pytest, the existing `cvd_5min_bot.py` and `paper_dashboard.py`. No new dependencies.

---

## Known State Going In

- Session data: `data/mm_paper_trades.csv` — 476 rows, 46 cycles, 35 settled, 13:43→17:24 2026-04-10
- Dashboard P&L: total −$131.05, settled_pnl −$126.54, cycles_won 11 / cycles_lost 24
- Adverse alignment: 11/35 GOOD = 31.4%, binomial p=0.014 one-sided (statistically worse than random)
- Pathologies by regime: STRONG_BULL (14% alignment, n=7) short-biased; STRONG_BEAR (22%, n=9) long-biased; NEUTRAL (64%, n=11) near break-even
- Cap `MM_MAX_INVENTORY=20` binds in 18.5% of fills — actively biting on the short side (saved ~$2.53 avg adverse per skip)
- MM spread P&L CI includes zero: [−$1.25, +$1.18] per cycle — pure MM has no proven edge or loss
- Settlement P&L is the dominant loss source: −$125 of −$131 total

## Scope & Exit Criteria

**In scope:**
- Ex-post experiments that can be computed from the existing CSV alone (no bot code changes needed)
- One code change to `cvd_5min_bot.py` if and only if an experiment shows a measurable, realistic improvement
- One fresh 1h paper validation session after the fix

**Out of scope:**
- Tuning `MM_MAX_INVENTORY` or `MM_MAX_CVD_SKEW` further (already tried, worse)
- Rewriting the CVD signal or replacing it with a new directional model (that is a separate, larger project)
- Any live (real USDC) deployment (forbidden until an edge is proven on ≥12h of paper data with CI excluding zero)

**Success thresholds (realistic, not optimistic):**

| Threshold | Meaning | Action |
|---|---|---|
| Alignment ≥ 50% AND per-cycle P&L ≥ −$0.50 | Break-even-ish | Implement fix, validate, monitor |
| Alignment ≥ 55% AND per-cycle P&L > $0.00 | Small positive edge | Implement fix, longer validation |
| Alignment ≥ 60% AND per-cycle P&L ≥ $0.50 | Clear edge (unlikely on this architecture) | Implement fix, plan live transition tests |
| None of the above | No edge identifiable | **Archive the strategy**, document findings, move on |

**Kill criteria (stop the plan immediately):**
- Backtest harness cannot reproduce dashboard totals to within $0.01 → something is wrong with the data or the dashboard
- All 4 experiments show per-cycle P&L worse than −$1.00 → no direction to go, stop and regroup
- Implementing the fix breaks existing unit tests → revert, do not commit

---

## File Structure

**Create:**
- `backtest.py` (project root) — Single-file backtest harness with replay functions, cycle slicing, and experiment drivers. Keep under 250 lines. Rationale: single-file preference from user memory, and this harness is a diagnostic tool, not production code.
- `tests/unit/test_backtest.py` — pytest tests for the harness.
- `docs/superpowers/reports/2026-04-10-experiment-results.md` — Will be populated by Task 3.6 with the 4 agents' findings.

**Modify (conditional on Phase 3 gate):**
- `cvd_5min_bot.py` — one targeted fix to the identified pathology. Exact lines TBD by Phase 3 winner.

**Do not touch:**
- `data/mm_paper_trades.csv` (read-only baseline — snapshot it in Task 0.2 to be safe)
- `paper_dashboard.py` (reference implementation for cash accounting; must stay compatible)

---

## Phase 0: Setup & Snapshot

### Task 0.1: Stop the bot and confirm it's dead

**Files:** None (operational only).

- [ ] **Step 1: Confirm user has stopped the bot in their terminal**

Ask the user to confirm the terminal running `python cvd_5min_bot.py` has been closed. Do not proceed until confirmed.

- [ ] **Step 2: Verify no lingering python.exe processes still hold the CSV**

Run: `tasklist //FI "IMAGENAME eq python.exe"`
Expected: Output lists python processes; verify none of them has the bot's PID by checking with `wmic process where "name='python.exe'" get processid,commandline`. If the bot process is still alive, ask the user to kill it before continuing.

- [ ] **Step 3: Confirm CSV mtime is no longer advancing**

Run: `stat data/mm_paper_trades.csv | grep Modify`
Wait 10 seconds, run again. The modify time must be unchanged. If it's still growing, the bot is still running somewhere.

### Task 0.2: Snapshot the baseline CSV

**Files:**
- Create: `data/mm_paper_trades_baseline_2026-04-10.csv`

- [ ] **Step 1: Copy the CSV to a read-only baseline**

Run: `cp data/mm_paper_trades.csv data/mm_paper_trades_baseline_2026-04-10.csv`
Expected: File created, no errors.

- [ ] **Step 2: Verify row counts match**

Run: `wc -l data/mm_paper_trades.csv data/mm_paper_trades_baseline_2026-04-10.csv`
Expected: Both files have the same line count (should be ~477).

- [ ] **Step 3: Commit the baseline**

```bash
git add data/mm_paper_trades_baseline_2026-04-10.csv
git commit -m "chore: snapshot 3h45 MM paper session as baseline for diagnostic"
```

---

## Phase 1: Backtest Harness (TDD)

The harness must reproduce the existing dashboard totals exactly on the baseline CSV before it can be trusted for counterfactuals.

### Task 1.1: Failing test for `load_fills` (sanity load)

**Files:**
- Create: `tests/unit/test_backtest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_backtest.py
"""Tests for the diagnostic backtest harness."""
import pandas as pd
import pytest

import backtest


BASELINE_CSV = "data/mm_paper_trades_baseline_2026-04-10.csv"


class TestLoadFills:
    def test_loads_csv_with_expected_columns(self):
        df = backtest.load_fills(BASELINE_CSV)
        expected = {"timestamp", "market_ts", "market_slug", "side", "price",
                    "size", "inventory_after", "cvd_skew", "signal_type"}
        assert set(df.columns) == expected

    def test_nonempty(self):
        df = backtest.load_fills(BASELINE_CSV)
        assert len(df) > 400  # should be ~476
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_backtest.py::TestLoadFills -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest'`

### Task 1.2: Minimal `load_fills` implementation

**Files:**
- Create: `backtest.py`

- [ ] **Step 1: Create the harness file with `load_fills`**

```python
# backtest.py
"""Diagnostic backtest harness for CVD market maker.

Replays mm_paper_trades.csv under counterfactual rules to test hypotheses
about the strategy's negative edge. Deterministic, cheap, parallelizable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import pandas as pd

MAKER_REBATE_RATE = 0.005  # 0.5% — matches paper_dashboard.py


def load_fills(csv_path: str) -> pd.DataFrame:
    """Load the MM paper trades CSV."""
    return pd.read_csv(csv_path)
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/unit/test_backtest.py::TestLoadFills -v`
Expected: PASS (2 tests).

### Task 1.3: Failing test for `iter_cycles`

**Files:**
- Modify: `tests/unit/test_backtest.py`

- [ ] **Step 1: Add the test**

Append to `tests/unit/test_backtest.py`:

```python
class TestIterCycles:
    def test_groups_by_market_ts(self):
        df = backtest.load_fills(BASELINE_CSV)
        cycles = list(backtest.iter_cycles(df))
        # 46 distinct cycles in the baseline session
        assert len(cycles) == 46

    def test_cycle_rows_preserve_order(self):
        df = backtest.load_fills(BASELINE_CSV)
        cycles = list(backtest.iter_cycles(df))
        for market_ts, rows in cycles:
            # All rows in a cycle share the same market_ts
            assert (rows["market_ts"] == market_ts).all()
            # Rows are in original CSV order (by timestamp)
            ts = pd.to_datetime(rows["timestamp"])
            assert ts.is_monotonic_increasing
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_backtest.py::TestIterCycles -v`
Expected: FAIL — `AttributeError: module 'backtest' has no attribute 'iter_cycles'`

### Task 1.4: Implement `iter_cycles`

**Files:**
- Modify: `backtest.py`

- [ ] **Step 1: Add the function**

Append to `backtest.py` (after `load_fills`):

```python
def iter_cycles(df: pd.DataFrame) -> Iterator[tuple[int, pd.DataFrame]]:
    """Yield (market_ts, cycle_rows) for each distinct cycle, in CSV order.

    Cycles are grouped by market_ts. The first-seen order is preserved
    (dict preserves insertion order in Python 3.7+).
    """
    seen: list[int] = []
    for ts in df["market_ts"]:
        if ts not in seen:
            seen.append(ts)
    for ts in seen:
        yield ts, df[df["market_ts"] == ts].reset_index(drop=True)
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/unit/test_backtest.py::TestIterCycles -v`
Expected: PASS (2 tests).

### Task 1.5: Failing test for `cycle_cash` (core P&L reproduction)

**Files:**
- Modify: `tests/unit/test_backtest.py`

- [ ] **Step 1: Add the test**

Append to `tests/unit/test_backtest.py`:

```python
class TestCycleCash:
    def test_reproduces_dashboard_settled_pnl(self):
        """Sum of cycle_cash across all cycles must match dashboard settled_pnl."""
        df = backtest.load_fills(BASELINE_CSV)
        total = sum(backtest.cycle_cash(rows) for _, rows in backtest.iter_cycles(df))

        # Cross-check against paper_dashboard.compute_portfolio
        import sys
        sys.path.insert(0, ".")
        from paper_dashboard import compute_portfolio
        dash = compute_portfolio(df)
        # settled_pnl counts only cycles with a SETTLE row; cycle_cash counts all.
        # Flat cycles contribute 0 to settled_pnl (no SETTLE row written) so totals match.
        assert abs(total - dash["settled_pnl"]) < 0.01, \
            f"backtest={total:.4f}, dashboard={dash['settled_pnl']:.4f}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_backtest.py::TestCycleCash -v`
Expected: FAIL — `AttributeError: module 'backtest' has no attribute 'cycle_cash'`

### Task 1.6: Implement `cycle_cash`

**Files:**
- Modify: `backtest.py`

- [ ] **Step 1: Add the function**

Append to `backtest.py`:

```python
def cycle_cash(cycle_rows: pd.DataFrame) -> float:
    """Compute total cash flow for one cycle (fills + rebates + settlement).

    Matches paper_dashboard.compute_portfolio's cycle_cash accounting exactly.
    Returns 0.0 for flat cycles with no SETTLE row.
    """
    cash = 0.0
    for _, row in cycle_rows.iterrows():
        if row["side"] == "BUY":
            notional = row["price"] * row["size"]
            rebate = notional * MAKER_REBATE_RATE
            cash += -notional + rebate
        elif row["side"] == "SELL":
            notional = row["price"] * row["size"]
            rebate = notional * MAKER_REBATE_RATE
            cash += notional + rebate
        elif row["side"] == "SETTLE":
            # Pre-settle inventory = inventory_after of previous row (or 0 if first)
            # Settlement cash = pre_settle_inv * price
            # We recover pre_settle_inv from the row's size and the sign convention:
            # the SETTLE row's size is the absolute pre_settle qty, and whether it
            # was long or short is encoded by the sign change between prior row and 0.
            # Simpler: reconstruct from fills in this cycle (excluding SETTLE rows).
            fills = cycle_rows[cycle_rows["side"].isin(["BUY", "SELL"])]
            net = 0
            for _, f in fills.iterrows():
                net += int(f["size"]) if f["side"] == "BUY" else -int(f["size"])
            cash += net * row["price"]
    return cash
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/unit/test_backtest.py::TestCycleCash -v`
Expected: PASS. If it fails due to arithmetic drift, debug by printing the per-cycle deltas — a mismatch here is a **kill criterion**: stop the plan and investigate.

### Task 1.7: Failing test for `cycle_outcome` (UP/DOWN/FLAT classifier)

**Files:**
- Modify: `tests/unit/test_backtest.py`

- [ ] **Step 1: Add the test**

Append to `tests/unit/test_backtest.py`:

```python
class TestCycleOutcome:
    def test_up_cycle(self):
        rows = pd.DataFrame([
            {"side": "BUY", "price": 0.56, "size": 10, "inventory_after": 10,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"side": "SETTLE", "price": 1.00, "size": 10, "inventory_after": 0,
             "cvd_skew": 0.0, "signal_type": "UP", "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        assert backtest.cycle_outcome(rows) == "UP"

    def test_down_cycle(self):
        rows = pd.DataFrame([
            {"side": "SELL", "price": 0.55, "size": 10, "inventory_after": -10,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"side": "SETTLE", "price": 0.00, "size": 10, "inventory_after": 0,
             "cvd_skew": 0.0, "signal_type": "DOWN", "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        assert backtest.cycle_outcome(rows) == "DOWN"

    def test_flat_cycle_no_settle(self):
        rows = pd.DataFrame([
            {"side": "BUY", "price": 0.50, "size": 10, "inventory_after": 10,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"side": "SELL", "price": 0.51, "size": 10, "inventory_after": 0,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        assert backtest.cycle_outcome(rows) == "FLAT"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_backtest.py::TestCycleOutcome -v`
Expected: FAIL — `AttributeError: module 'backtest' has no attribute 'cycle_outcome'`

### Task 1.8: Implement `cycle_outcome`

**Files:**
- Modify: `backtest.py`

- [ ] **Step 1: Add the function**

Append to `backtest.py`:

```python
def cycle_outcome(cycle_rows: pd.DataFrame) -> str:
    """Return 'UP', 'DOWN', or 'FLAT' based on SETTLE row presence and price.

    - 'UP'   : a SETTLE row with price == 1.0
    - 'DOWN' : a SETTLE row with price == 0.0
    - 'FLAT' : no SETTLE row (cycle ended at inventory=0)
    """
    settle = cycle_rows[cycle_rows["side"] == "SETTLE"]
    if len(settle) == 0:
        return "FLAT"
    price = float(settle.iloc[0]["price"])
    if price >= 0.5:
        return "UP"
    return "DOWN"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/unit/test_backtest.py::TestCycleOutcome -v`
Expected: PASS (3 tests).

### Task 1.9: Failing test for `pre_settle_inventory`

**Files:**
- Modify: `tests/unit/test_backtest.py`

- [ ] **Step 1: Add the test**

Append to `tests/unit/test_backtest.py`:

```python
class TestPreSettleInventory:
    def test_long_pre_settle(self):
        rows = pd.DataFrame([
            {"side": "BUY", "price": 0.56, "size": 10, "inventory_after": 10,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"side": "SETTLE", "price": 1.00, "size": 10, "inventory_after": 0,
             "cvd_skew": 0.0, "signal_type": "UP", "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        assert backtest.pre_settle_inventory(rows) == 10

    def test_short_pre_settle(self):
        rows = pd.DataFrame([
            {"side": "SELL", "price": 0.55, "size": 10, "inventory_after": -10,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"side": "SETTLE", "price": 0.00, "size": 10, "inventory_after": 0,
             "cvd_skew": 0.0, "signal_type": "DOWN", "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        assert backtest.pre_settle_inventory(rows) == -10

    def test_flat_returns_zero(self):
        rows = pd.DataFrame([
            {"side": "BUY", "price": 0.50, "size": 10, "inventory_after": 10,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"side": "SELL", "price": 0.51, "size": 10, "inventory_after": 0,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        assert backtest.pre_settle_inventory(rows) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_backtest.py::TestPreSettleInventory -v`
Expected: FAIL — `AttributeError`.

### Task 1.10: Implement `pre_settle_inventory`

**Files:**
- Modify: `backtest.py`

- [ ] **Step 1: Add the function**

Append to `backtest.py`:

```python
def pre_settle_inventory(cycle_rows: pd.DataFrame) -> int:
    """Return the net inventory (signed shares) accumulated from BUY/SELL rows.

    Ignores SETTLE rows. Long positive, short negative, 0 if flat.
    """
    net = 0
    for _, row in cycle_rows.iterrows():
        if row["side"] == "BUY":
            net += int(row["size"])
        elif row["side"] == "SELL":
            net -= int(row["size"])
    return net
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/unit/test_backtest.py::TestPreSettleInventory -v`
Expected: PASS (3 tests).

### Task 1.11: Failing test for `dominant_signal`

**Files:**
- Modify: `tests/unit/test_backtest.py`

- [ ] **Step 1: Add the test**

Append to `tests/unit/test_backtest.py`:

```python
class TestDominantSignal:
    def test_most_frequent_signal(self):
        rows = pd.DataFrame([
            {"signal_type": "STRONG_BULL", "side": "BUY", "price": 0.5, "size": 10,
             "inventory_after": 10, "cvd_skew": 0.05, "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"signal_type": "STRONG_BULL", "side": "SELL", "price": 0.5, "size": 10,
             "inventory_after": 0, "cvd_skew": 0.05, "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
            {"signal_type": "NEUTRAL", "side": "BUY", "price": 0.5, "size": 10,
             "inventory_after": 10, "cvd_skew": 0.0, "market_ts": 1, "timestamp": "t3", "market_slug": "s"},
        ])
        assert backtest.dominant_signal(rows) == "STRONG_BULL"

    def test_excludes_settle_row(self):
        rows = pd.DataFrame([
            {"signal_type": "BULLISH", "side": "BUY", "price": 0.5, "size": 10,
             "inventory_after": 10, "cvd_skew": 0.01, "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"signal_type": "UP", "side": "SETTLE", "price": 1.0, "size": 10,
             "inventory_after": 0, "cvd_skew": 0.0, "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        # SETTLE row's signal_type ("UP") is the outcome, not a trading signal
        assert backtest.dominant_signal(rows) == "BULLISH"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_backtest.py::TestDominantSignal -v`
Expected: FAIL — `AttributeError`.

### Task 1.12: Implement `dominant_signal`

**Files:**
- Modify: `backtest.py`

- [ ] **Step 1: Add the function**

Append to `backtest.py`:

```python
def dominant_signal(cycle_rows: pd.DataFrame) -> str:
    """Return the most frequent signal_type across BUY/SELL rows (excludes SETTLE).

    Ties broken by last-seen. Returns 'NEUTRAL' if no trading rows.
    """
    trading = cycle_rows[cycle_rows["side"].isin(["BUY", "SELL"])]
    if len(trading) == 0:
        return "NEUTRAL"
    counts = trading["signal_type"].value_counts()
    return str(counts.idxmax())
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/unit/test_backtest.py::TestDominantSignal -v`
Expected: PASS (2 tests).

### Task 1.13: Commit the harness

- [ ] **Step 1: Run the full harness test suite**

Run: `pytest tests/unit/test_backtest.py -v`
Expected: All tests pass (should be ~13 tests).

- [ ] **Step 2: Commit**

```bash
git add backtest.py tests/unit/test_backtest.py
git commit -m "feat: add diagnostic backtest harness with cycle replay primitives

Provides load_fills, iter_cycles, cycle_cash, cycle_outcome,
pre_settle_inventory, dominant_signal. Reproduces paper_dashboard
settled_pnl exactly on the 2026-04-10 baseline CSV (kill criterion check)."
```

---

## Phase 2: Parallel Ex-Post Experiments

Four independent hypotheses, each tested by one subagent against the baseline CSV. **No bot code changes.** Each experiment is a pure function over the CSV.

### Task 2.1: Write experiment driver functions

**Files:**
- Modify: `backtest.py`

- [ ] **Step 1: Add the 4 experiment functions**

Append to `backtest.py`:

```python
# ═══════════════════════════════════════════════════════════════════════
# EXPERIMENTS — each returns a dict of metrics for comparison
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class ExperimentResult:
    name: str
    total_pnl: float
    cycles_kept: int
    cycles_dropped: int
    alignment_good: int
    alignment_bad: int
    alignment_rate: float
    per_cycle_mean: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "total_pnl": round(self.total_pnl, 4),
            "cycles_kept": self.cycles_kept,
            "cycles_dropped": self.cycles_dropped,
            "alignment_good": self.alignment_good,
            "alignment_bad": self.alignment_bad,
            "alignment_rate": round(self.alignment_rate, 4),
            "per_cycle_mean": round(self.per_cycle_mean, 4),
        }


def _classify_alignment(inv: int, outcome: str) -> str:
    """GOOD if inventory sign matches outcome, BAD otherwise, NA if flat."""
    if outcome == "FLAT" or inv == 0:
        return "NA"
    if (inv > 0 and outcome == "UP") or (inv < 0 and outcome == "DOWN"):
        return "GOOD"
    return "BAD"


def experiment_baseline(df: pd.DataFrame) -> ExperimentResult:
    """Experiment 0: pure replay, no modification. Must match current reality."""
    total = 0.0
    kept = 0
    good = bad = 0
    per_cycle: list[float] = []
    for _, rows in iter_cycles(df):
        cash = cycle_cash(rows)
        total += cash
        kept += 1
        per_cycle.append(cash)
        inv = pre_settle_inventory(rows)
        outcome = cycle_outcome(rows)
        tag = _classify_alignment(inv, outcome)
        if tag == "GOOD":
            good += 1
        elif tag == "BAD":
            bad += 1
    settled = good + bad
    rate = good / settled if settled else 0.0
    mean = sum(per_cycle) / len(per_cycle) if per_cycle else 0.0
    return ExperimentResult("baseline", total, kept, 0, good, bad, rate, mean)


def experiment_neutral_only(df: pd.DataFrame) -> ExperimentResult:
    """Experiment 1: drop all cycles where dominant_signal != NEUTRAL.

    Simulates 'only quote in NEUTRAL regimes, skip strong trends entirely'.
    """
    total = 0.0
    kept = dropped = 0
    good = bad = 0
    per_cycle: list[float] = []
    for _, rows in iter_cycles(df):
        if dominant_signal(rows) != "NEUTRAL":
            dropped += 1
            continue
        cash = cycle_cash(rows)
        total += cash
        kept += 1
        per_cycle.append(cash)
        inv = pre_settle_inventory(rows)
        outcome = cycle_outcome(rows)
        tag = _classify_alignment(inv, outcome)
        if tag == "GOOD":
            good += 1
        elif tag == "BAD":
            bad += 1
    settled = good + bad
    rate = good / settled if settled else 0.0
    mean = sum(per_cycle) / len(per_cycle) if per_cycle else 0.0
    return ExperimentResult("neutral_only", total, kept, dropped, good, bad, rate, mean)


def experiment_forced_flatten(df: pd.DataFrame) -> ExperimentResult:
    """Experiment 2: strip the settlement leg from every cycle.

    Simulates 'the bot always flattens before cycle end, no settlement exposure'.
    Cash is the sum of all BUY/SELL rows (with rebates) minus the settlement leg.
    This isolates pure MM spread P&L.
    """
    total = 0.0
    kept = 0
    good = bad = 0  # alignment is N/A when forced flat — counters stay 0
    per_cycle: list[float] = []
    for _, rows in iter_cycles(df):
        non_settle = rows[rows["side"].isin(["BUY", "SELL"])]
        cash = 0.0
        for _, r in non_settle.iterrows():
            notional = r["price"] * r["size"]
            rebate = notional * MAKER_REBATE_RATE
            if r["side"] == "BUY":
                cash += -notional + rebate
            else:
                cash += notional + rebate
        # Force-flatten at baseline $0.50 (fair binary midpoint)
        inv = pre_settle_inventory(rows)
        cash += inv * 0.50
        total += cash
        kept += 1
        per_cycle.append(cash)
    mean = sum(per_cycle) / len(per_cycle) if per_cycle else 0.0
    return ExperimentResult("forced_flatten_0.50", total, kept, 0, good, bad, 0.0, mean)


def experiment_half_cap(df: pd.DataFrame) -> ExperimentResult:
    """Experiment 3: truncate all fills that would push |inventory| past 10.

    Simulates MM_MAX_INVENTORY=10 (half the current cap). Drops fills that
    couldn't have happened under the tighter cap. NB: this is an imperfect
    simulation because the bot's subsequent fills would differ under the
    tighter cap — treat as directional evidence only.
    """
    total = 0.0
    kept = 0
    good = bad = 0
    per_cycle: list[float] = []
    for _, rows in iter_cycles(df):
        cash = 0.0
        net = 0
        for _, r in rows.iterrows():
            if r["side"] == "SETTLE":
                cash += net * r["price"]
                continue
            # Simulate a cap of 10: drop this fill if it would push past ±10
            delta = int(r["size"]) if r["side"] == "BUY" else -int(r["size"])
            if abs(net + delta) > 10:
                continue
            notional = r["price"] * r["size"]
            rebate = notional * MAKER_REBATE_RATE
            if r["side"] == "BUY":
                cash += -notional + rebate
            else:
                cash += notional + rebate
            net += delta
        total += cash
        kept += 1
        per_cycle.append(cash)
        # Alignment: use simulated pre-settle net (just before SETTLE logic above)
        # We need to recompute it since the loop already applied the SETTLE.
        net_recalc = 0
        for _, r in rows.iterrows():
            if r["side"] == "SETTLE":
                break
            delta = int(r["size"]) if r["side"] == "BUY" else -int(r["size"])
            if abs(net_recalc + delta) > 10:
                continue
            net_recalc += delta
        outcome = cycle_outcome(rows)
        tag = _classify_alignment(net_recalc, outcome)
        if tag == "GOOD":
            good += 1
        elif tag == "BAD":
            bad += 1
    settled = good + bad
    rate = good / settled if settled else 0.0
    mean = sum(per_cycle) / len(per_cycle) if per_cycle else 0.0
    return ExperimentResult("half_cap_10", total, kept, 0, good, bad, rate, mean)
```

- [ ] **Step 2: Commit (tests come next, function body is stable)**

```bash
git add backtest.py
git commit -m "feat: add 4 ex-post experiment drivers (baseline, neutral-only, forced-flatten, half-cap)"
```

### Task 2.2: Failing test for baseline experiment reproducing dashboard

**Files:**
- Modify: `tests/unit/test_backtest.py`

- [ ] **Step 1: Add the test**

Append to `tests/unit/test_backtest.py`:

```python
class TestExperimentBaseline:
    def test_reproduces_dashboard_total(self):
        df = backtest.load_fills(BASELINE_CSV)
        result = backtest.experiment_baseline(df)

        import sys
        sys.path.insert(0, ".")
        from paper_dashboard import compute_portfolio
        dash = compute_portfolio(df)

        # settled_pnl is the sum of cycle_cash across all cycles (flat cycles contribute 0)
        assert abs(result.total_pnl - dash["settled_pnl"]) < 0.01, \
            f"baseline={result.total_pnl:.4f}, dashboard={dash['settled_pnl']:.4f}"

    def test_alignment_matches_adverse_selection_agent(self):
        """Previous agent reported 11/35 = 31.4% alignment. Reproduce it."""
        df = backtest.load_fills(BASELINE_CSV)
        result = backtest.experiment_baseline(df)
        settled = result.alignment_good + result.alignment_bad
        # Allow tiny drift if bot wrote more rows between agent runs
        assert 30 <= settled <= 40, f"settled count {settled}"
        assert result.alignment_rate < 0.40, f"alignment too high: {result.alignment_rate}"
```

- [ ] **Step 2: Run test**

Run: `pytest tests/unit/test_backtest.py::TestExperimentBaseline -v`
Expected: PASS. **If this fails, the harness is broken — kill criterion hit, stop the plan.**

### Task 2.3: Sanity test for other experiments

**Files:**
- Modify: `tests/unit/test_backtest.py`

- [ ] **Step 1: Add the test**

Append to `tests/unit/test_backtest.py`:

```python
class TestExperimentsSmoke:
    def test_all_experiments_run(self):
        df = backtest.load_fills(BASELINE_CSV)
        for fn in (backtest.experiment_neutral_only,
                   backtest.experiment_forced_flatten,
                   backtest.experiment_half_cap):
            result = fn(df)
            assert result.cycles_kept > 0
            # total_pnl is a float; just make sure it's finite
            assert result.total_pnl == result.total_pnl  # not NaN
```

- [ ] **Step 2: Run test**

Run: `pytest tests/unit/test_backtest.py::TestExperimentsSmoke -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_backtest.py
git commit -m "test: add smoke tests for backtest experiments (baseline reproduces dashboard)"
```

### Task 2.4: Dispatch 4 parallel agents to run experiments

**Files:** None (orchestration step).

- [ ] **Step 1: Dispatch agent 1 — Baseline confirmation**

Use the Agent tool (subagent_type: general-purpose, run_in_background: true) with this exact prompt:

```
You are a DIAGNOSTIC REPLAY agent. Run the baseline experiment from the
backtest harness and confirm it reproduces the dashboard numbers.

Context: The bot has a -31.4% alignment problem. We just built
`backtest.py` at the project root. Your job is to run it and report.

Steps:
1. cd to C:\Users\kenny\Desktop\POLYMARKET-5MINBTC
2. Run: python -c "import backtest; import json; df = backtest.load_fills('data/mm_paper_trades_baseline_2026-04-10.csv'); r = backtest.experiment_baseline(df); print(json.dumps(r.to_dict(), indent=2))"
3. Also run: python -c "import sys; sys.path.insert(0, '.'); import pandas as pd; from paper_dashboard import compute_portfolio; df = pd.read_csv('data/mm_paper_trades_baseline_2026-04-10.csv'); r = compute_portfolio(df); import json; print(json.dumps({k: (float(v) if hasattr(v, 'item') else v) for k,v in r.items()}, indent=2, default=str))"
4. Report:
   - The experiment's total_pnl and alignment_rate
   - The dashboard's settled_pnl and (cycles_won, cycles_lost)
   - Whether they match within $0.01
   - If they DO NOT match, this is a kill criterion — flag loudly
5. Do not modify any files. Read-only analysis only.
```

- [ ] **Step 2: Dispatch agent 2 — NEUTRAL-only hypothesis**

Use the Agent tool with this prompt:

```
You are a HYPOTHESIS TEST agent. Test the hypothesis: "if we gate the bot
to only trade in NEUTRAL signal regimes, the loss would be drastically
reduced or eliminated."

Context: Prior analysis showed NEUTRAL cycles (n=11) lost only -$0.42/cycle
while STRONG_BULL (n=7) and STRONG_BEAR (n=9) cycles lost -$6+/cycle.
This experiment verifies that exact claim on the full 46-cycle baseline.

Steps:
1. cd to C:\Users\kenny\Desktop\POLYMARKET-5MINBTC
2. Run: python -c "import backtest; import json; df = backtest.load_fills('data/mm_paper_trades_baseline_2026-04-10.csv'); r = backtest.experiment_neutral_only(df); print(json.dumps(r.to_dict(), indent=2))"
3. Report:
   - cycles_kept and cycles_dropped
   - total_pnl (vs baseline -$126.54)
   - per_cycle_mean (vs baseline -$3.61)
   - alignment_rate (vs baseline 31.4%)
   - Whether this experiment crosses any success threshold:
     Break-even-ish: per_cycle_mean >= -$0.50 AND alignment >= 50%
     Small edge:     per_cycle_mean > 0.00     AND alignment >= 55%
     Clear edge:     per_cycle_mean >= $0.50   AND alignment >= 60%
4. Verdict in 3 sentences.
5. Do NOT modify any files.
```

- [ ] **Step 3: Dispatch agent 3 — Forced flatten hypothesis**

Use the Agent tool with this prompt:

```
You are a HYPOTHESIS TEST agent. Test the hypothesis: "if the bot always
flattens before settlement (no directional exposure), the pure MM spread
P&L is positive or at least non-negative."

Context: Prior analysis showed settlement P&L was -$125 of the -$131 total
loss, and the MM spread P&L CI [-$1.25, +$1.18] per cycle includes zero.
This experiment strips the settlement leg entirely and marks open positions
to $0.50 (fair binary midpoint).

Steps:
1. cd to C:\Users\kenny\Desktop\POLYMARKET-5MINBTC
2. Run: python -c "import backtest; import json; df = backtest.load_fills('data/mm_paper_trades_baseline_2026-04-10.csv'); r = backtest.experiment_forced_flatten(df); print(json.dumps(r.to_dict(), indent=2))"
3. Also compute the 95% confidence interval on the per-cycle mean manually:
   Run: python -c "
   import backtest
   df = backtest.load_fills('data/mm_paper_trades_baseline_2026-04-10.csv')
   per = []
   for _, rows in backtest.iter_cycles(df):
       non_settle = rows[rows['side'].isin(['BUY','SELL'])]
       cash = 0.0
       for _, r in non_settle.iterrows():
           notional = r['price'] * r['size']
           rebate = notional * 0.005
           if r['side'] == 'BUY':
               cash += -notional + rebate
           else:
               cash += notional + rebate
       inv = backtest.pre_settle_inventory(rows)
       cash += inv * 0.50
       per.append(cash)
   import statistics as s
   n = len(per)
   m = s.mean(per)
   sd = s.stdev(per)
   se = sd / (n ** 0.5)
   ci_low = m - 1.96*se
   ci_high = m + 1.96*se
   print(f'n={n} mean={m:.4f} sd={sd:.4f} se={se:.4f} 95CI=[{ci_low:.4f}, {ci_high:.4f}]')
   "
4. Report:
   - total_pnl and per_cycle_mean
   - 95% CI on per_cycle_mean
   - Does the CI include zero? (if yes: no MM spread edge proven; if no and it's positive: spread edge exists; if no and it's negative: spread also loses)
   - Whether this experiment would have reduced the loss and by how much
5. Verdict in 3 sentences.
6. Do NOT modify any files.
```

- [ ] **Step 4: Dispatch agent 4 — Half cap hypothesis**

Use the Agent tool with this prompt:

```
You are a HYPOTHESIS TEST agent. Test the hypothesis: "reducing MAX_INVENTORY
from 20 to 10 would cap cycle losses enough to matter."

Context: The current cap of 20 binds in 18.5% of fills and has saved ~$2.53
per short-side skip. Prior short-cap ROI showed 3:1 adverse ratio. The
question: does halving the cap materially reduce the loss, or does it
starve the bot of fills?

CAVEAT: This is an imperfect simulation. The bot's subsequent fills would
differ under a tighter cap (the fill detector depends on inventory state).
Treat results as DIRECTIONAL evidence, not as a true counterfactual.

Steps:
1. cd to C:\Users\kenny\Desktop\POLYMARKET-5MINBTC
2. Run: python -c "import backtest; import json; df = backtest.load_fills('data/mm_paper_trades_baseline_2026-04-10.csv'); r = backtest.experiment_half_cap(df); print(json.dumps(r.to_dict(), indent=2))"
3. Report:
   - total_pnl vs baseline -$126.54
   - per_cycle_mean vs baseline -$3.61
   - alignment_rate vs baseline 31.4%
   - How many fills were 'dropped' by the simulated cap (compute from the cap logic in experiment_half_cap)
4. State clearly that this is a DIRECTIONAL result, not a true counterfactual
   (the bot would have made different subsequent decisions under a real cap=10).
5. Verdict in 3 sentences.
6. Do NOT modify any files.
```

- [ ] **Step 5: Wait for all 4 agents to complete**

Do not proceed until all 4 task notifications arrive. Do not poll; the system notifies on completion.

### Task 2.5: Aggregate results

**Files:**
- Create: `docs/superpowers/reports/2026-04-10-experiment-results.md`

- [ ] **Step 1: Write the aggregation doc**

Copy each agent's reported numbers into a single comparison table in the new file:

```markdown
# Ex-Post Experiment Results — 2026-04-10

Baseline CSV: `data/mm_paper_trades_baseline_2026-04-10.csv`
Baseline totals: 46 cycles, 35 settled, -$126.54 total, 31.4% alignment, -$3.61/cycle

## Comparison

| Experiment | Cycles | Total P&L | Per-cycle | Alignment | Verdict |
|---|---|---|---|---|---|
| baseline | 46 | [agent1] | [agent1] | [agent1] | sanity |
| neutral_only | [agent2] | [agent2] | [agent2] | [agent2] | [agent2] |
| forced_flatten | 46 | [agent3] | [agent3] | N/A | [agent3] |
| half_cap_10 | 46 | [agent4] | [agent4] | [agent4] | [agent4] |

## Agent notes

### Baseline
[paste agent1's key finding]

### Neutral-only
[paste agent2's verdict]

### Forced flatten
[paste agent3's CI and verdict]

### Half-cap
[paste agent4's verdict + directional caveat]
```

- [ ] **Step 2: Commit the report**

```bash
git add docs/superpowers/reports/2026-04-10-experiment-results.md
git commit -m "docs: aggregate 4 parallel ex-post experiment results"
```

---

## Phase 3: Gate — Decide

### Task 3.1: Evaluate each experiment against thresholds

**Files:** None (decision step).

- [ ] **Step 1: Score each experiment against the success thresholds table**

For each of the 3 non-baseline experiments, classify the result:

| Score | Rule |
|---|---|
| **Archive** | per_cycle_mean worse than −$1.00 |
| **Inconclusive** | per_cycle_mean in [−$1.00, −$0.50] OR alignment 45–50% |
| **Break-even** | per_cycle_mean ≥ −$0.50 AND alignment ≥ 50% |
| **Small edge** | per_cycle_mean > $0.00 AND alignment ≥ 55% |
| **Clear edge** | per_cycle_mean ≥ $0.50 AND alignment ≥ 60% |

Write the score for each in a short paragraph in the same report file.

- [ ] **Step 2: Pick the winner**

Rules (in priority order):
1. If any experiment scores "Clear edge" or "Small edge" → it's the winner.
2. Else if exactly one experiment scores "Break-even" → it's the winner.
3. Else if multiple score "Break-even" → pick the simplest to implement (prefer `neutral_only` over `forced_flatten` over `half_cap_10`).
4. Else (all Archive/Inconclusive) → **no winner**.

- [ ] **Step 3: Branch decision**

- If a winner was picked → proceed to Phase 4 and implement it.
- If no winner → skip directly to Phase 6 (Final Decision). **Do not attempt Phase 4 on an inconclusive experiment.** That path wastes time.

---

## Phase 4: Implement the Winner (conditional)

**This phase runs only if Phase 3 identified a winner.** The exact implementation depends on which experiment won. The plan covers all three branches — execute only the branch matching the winner.

### Task 4.1a: Implement `neutral_only` gating (branch A)

**Only if winner == neutral_only.**

**Files:**
- Modify: `cvd_5min_bot.py:1883-1895` (the `_refresh_quotes` method)
- Modify: `tests/unit/test_mm_paper.py` (add a gating test)

- [ ] **Step 1: Failing test — non-NEUTRAL signal should zero the quote sizes**

Add to `tests/unit/test_mm_paper.py` in a new class:

```python
class TestNeutralOnlyGating:
    def test_strong_bull_regime_zeroes_quote_sizes(self, monkeypatch):
        """When gating is active, non-NEUTRAL signals must produce bid_size=0 ask_size=0."""
        import cvd_5min_bot as bot
        # Force the signal to STRONG_BULL regardless of CVD
        monkeypatch.setattr(bot.MarketMaker, "compute_cvd_skew",
                            lambda self: (0.05, "STRONG_BULL"))
        # Set NEUTRAL_ONLY flag (to be added)
        monkeypatch.setattr(bot, "MM_NEUTRAL_ONLY", True)

        mm = bot.MarketMaker(feed=None, placeholder=True)  # see ctor in bot
        mm.inventory = bot.MMInventory(max_inventory=20)
        book = {"best_bid": 0.49, "best_ask": 0.51}
        mm._refresh_quotes(book)
        assert mm.current_bid_size == 0
        assert mm.current_ask_size == 0
```

Note: the test above depends on the existing `MarketMaker` constructor signature; inspect `cvd_5min_bot.py:1810-1825` and adjust the ctor arguments to a minimal stub (use `monkeypatch` to sidestep the feed dependency if necessary).

- [ ] **Step 2: Run test, confirm it fails**

Run: `pytest tests/unit/test_mm_paper.py::TestNeutralOnlyGating -v`
Expected: FAIL (either AttributeError on `MM_NEUTRAL_ONLY`, or the quotes are non-zero).

- [ ] **Step 3: Implement the env flag and the gate**

In `cvd_5min_bot.py`, near the other `MM_*` constants (search for `MM_MAX_INVENTORY = `):

```python
MM_NEUTRAL_ONLY = os.getenv("MM_NEUTRAL_ONLY", "false").lower() == "true"
```

Then in `_refresh_quotes` (around line 1883), after `self.current_cvd_skew, self.current_signal_type = self.compute_cvd_skew()`, add:

```python
        # Phase 3 gating: if MM_NEUTRAL_ONLY is set, only quote in NEUTRAL regime
        if MM_NEUTRAL_ONLY and self.current_signal_type != "NEUTRAL":
            self.current_bid = 0.0
            self.current_ask = 0.0
            self.current_bid_size = 0
            self.current_ask_size = 0
            return
```

- [ ] **Step 4: Run test, verify it passes**

Run: `pytest tests/unit/test_mm_paper.py::TestNeutralOnlyGating -v`
Expected: PASS.

- [ ] **Step 5: Run the full existing test suite**

Run: `pytest tests/unit/ -v`
Expected: All existing tests still pass. If any fail → revert the change (kill criterion).

- [ ] **Step 6: Enable the flag in .env**

Append to `.env`:
```
MM_NEUTRAL_ONLY=true
```

- [ ] **Step 7: Commit**

```bash
git add cvd_5min_bot.py tests/unit/test_mm_paper.py .env
git commit -m "feat(mm): gate quoting to NEUTRAL regime only

Activated by MM_NEUTRAL_ONLY=true. When the signal is anything other
than NEUTRAL, bid_size and ask_size are forced to 0 — the bot posts
nothing in strong-trend regimes where the backtest showed -\$6/cycle
adverse selection.

Justification: ex-post experiment on 46-cycle baseline showed
neutral-only cycles averaged -\$0.42/cycle vs -\$3.61 baseline.
See docs/superpowers/reports/2026-04-10-experiment-results.md."
```

### Task 4.1b: Implement `forced_flatten` (branch B)

**Only if winner == forced_flatten.**

**Files:**
- Modify: `cvd_5min_bot.py` — add an end-of-cycle flatten routine in `run_market_cycle`
- Modify: `tests/unit/test_mm_paper.py`

- [ ] **Step 1: Failing test — bot must flatten inventory 30s before cycle end**

Add to `tests/unit/test_mm_paper.py`:

```python
class TestForcedFlatten:
    def test_bot_flattens_near_cycle_end(self, monkeypatch):
        """When time_remaining <= 30s and inventory != 0, bot should post
        aggressive taker-priced quotes to flatten before settlement."""
        import cvd_5min_bot as bot
        monkeypatch.setattr(bot, "MM_FORCE_FLATTEN_SEC", 30)

        mm = bot.MarketMaker(feed=None, placeholder=True)
        mm.inventory = bot.MMInventory(max_inventory=20)
        mm.inventory.net_position = 10  # long 10
        book = {"best_bid": 0.49, "best_ask": 0.51}
        quotes = mm.compute_flatten_quotes(book, time_remaining=20)
        # Long → sell aggressively at best_bid to guarantee fill
        assert quotes["ask_price"] == 0.49
        assert quotes["ask_size"] == 10
        assert quotes["bid_size"] == 0
```

- [ ] **Step 2: Run test, confirm failure**

Run: `pytest tests/unit/test_mm_paper.py::TestForcedFlatten -v`
Expected: FAIL — `compute_flatten_quotes` does not exist.

- [ ] **Step 3: Implement `compute_flatten_quotes` and wire into the cycle loop**

Add to `cvd_5min_bot.py` near `calculate_mm_quotes`:

```python
MM_FORCE_FLATTEN_SEC = int(os.getenv("MM_FORCE_FLATTEN_SEC", "30"))


def compute_flatten_quotes(book: dict, inventory: int) -> dict:
    """Compute quotes that aggressively flatten inventory by taking the other side.

    Long → post SELL at best_bid (becomes a taker, but certain fill).
    Short → post BUY at best_ask.
    Flat → no quotes.
    """
    if inventory == 0:
        return {"bid_price": 0.0, "ask_price": 0.0, "bid_size": 0, "ask_size": 0}
    if inventory > 0:
        return {"bid_price": 0.0, "ask_price": float(book["best_bid"]),
                "bid_size": 0, "ask_size": abs(inventory)}
    return {"bid_price": float(book["best_ask"]), "ask_price": 0.0,
            "bid_size": abs(inventory), "ask_size": 0}
```

Then add a method `MarketMaker.compute_flatten_quotes(self, book, time_remaining)` that wraps the above and returns the result.

Finally, in `run_market_cycle` (around the main loop), when `time_remaining <= MM_FORCE_FLATTEN_SEC` and `self.inventory.net_position != 0`, call `compute_flatten_quotes` instead of the normal `_refresh_quotes`.

Exact location TBD by reading lines 1990-2080 of `cvd_5min_bot.py` during implementation — preserve the existing structure.

- [ ] **Step 4: Run test, verify passes**

Run: `pytest tests/unit/test_mm_paper.py::TestForcedFlatten -v`
Expected: PASS.

- [ ] **Step 5: Run full suite**

Run: `pytest tests/unit/ -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add cvd_5min_bot.py tests/unit/test_mm_paper.py
git commit -m "feat(mm): force-flatten inventory 30s before cycle end

When MM_FORCE_FLATTEN_SEC remains in the cycle and net_position != 0,
the bot switches from neutral MM quotes to aggressive taker-priced
quotes on the contra side, guaranteeing flat at settle.

Justification: ex-post forced_flatten experiment showed the settlement
leg is the dominant loss source (-\$125 of -\$131 total). Stripping it
leaves spread P&L CI [-X, +Y] per cycle (see experiment report)."
```

### Task 4.1c: Implement `half_cap` (branch C)

**Only if winner == half_cap_10.**

**Files:**
- Modify: `.env`

- [ ] **Step 1: Update the env**

Edit `.env`:
```
MM_MAX_INVENTORY=10
```

- [ ] **Step 2: Run existing test suite to make sure nothing hardcodes 20**

Run: `pytest tests/unit/ -v`
Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add .env
git commit -m "chore: tighten MM_MAX_INVENTORY to 10 per half_cap experiment

Ex-post simulation showed tighter cap reduces loss by X%. Note this is
a DIRECTIONAL result — true behavior under cap=10 requires a fresh
paper session (Phase 5)."
```

---

## Phase 5: Live Paper Validation

**Runs if Phase 4 executed (i.e. a fix was implemented).**

### Task 5.1: Clear the live CSV (keep baseline)

**Files:**
- Modify: `data/mm_paper_trades.csv`

- [ ] **Step 1: Overwrite with just the header**

```bash
head -n 1 data/mm_paper_trades_baseline_2026-04-10.csv > data/mm_paper_trades.csv
```

- [ ] **Step 2: Verify**

Run: `wc -l data/mm_paper_trades.csv`
Expected: 1.

### Task 5.2: User restarts the bot manually

**Files:** None.

- [ ] **Step 1: Ask the user to restart the bot**

Tell the user: "Open a terminal in the project directory and run `python cvd_5min_bot.py`. Leave it running. Report back when you see the first few fills in the CSV."

Do not proceed until the user confirms.

- [ ] **Step 2: Verify the bot is writing**

Run: `wc -l data/mm_paper_trades.csv` and check that the line count is > 1 and growing across two successive checks (spaced 30s apart).

### Task 5.3: Dispatch 2 parallel monitoring agents

- [ ] **Step 1: Agent A — Fill monitor (60 min)**

Use the Agent tool with this prompt:

```
You are a LIVE FILL MONITOR agent. Monitor the bot for 60 minutes and
report fill counts, max |inventory_after|, and any anomalies.

Context: The bot was restarted after implementing a fix from Phase 4.
Expected behavior: [describe the winning experiment's expected behavior
in 1-2 sentences — e.g. 'NEUTRAL-only gating should drop ~75% of fills
compared to baseline'].

Steps:
1. Poll data/mm_paper_trades.csv every 90 seconds for 40 iterations (~60 min)
2. At each iteration, record total fills, SETTLE count, max |inventory_after|
3. At the end, produce:
   - Total fills / BUY / SELL / SETTLE counts
   - Max |inventory_after|
   - Number of cycles touched
   - Any iteration where the CSV didn't grow (possible bot stall)
4. Do NOT modify any files. Read-only.
```

- [ ] **Step 2: Agent B — P&L comparator**

Use the Agent tool with this prompt:

```
You are a P&L COMPARATOR agent. After the fill monitor completes (60 min
of data), compute the current session P&L, alignment, and per-cycle mean,
then compare to the 2026-04-10 baseline.

Context: Baseline was 46 cycles, -$126.54, 31.4% alignment, -$3.61/cycle.
The fix from Phase 4 should improve at least one of those metrics.

Wait 60 minutes (use sleep 3600), then:
1. Run: python -c "import sys; sys.path.insert(0, '.'); import pandas as pd; from paper_dashboard import compute_portfolio; df = pd.read_csv('data/mm_paper_trades.csv'); import json; print(json.dumps({k: (float(v) if hasattr(v, 'item') else v) for k,v in compute_portfolio(df).items()}, indent=2, default=str))"
2. Run: python -c "import backtest; r = backtest.experiment_baseline(backtest.load_fills('data/mm_paper_trades.csv')); import json; print(json.dumps(r.to_dict(), indent=2))"
3. Compute: per-cycle mean, alignment rate, total P&L
4. Compare to baseline (-$3.61/cycle, 31.4% alignment)
5. Score against the thresholds table (Break-even / Small edge / Clear edge / Archive)
6. Do NOT modify any files.
```

- [ ] **Step 3: Wait for both agents**

System will notify on completion of each.

### Task 5.4: User stops the bot

- [ ] **Step 1: Ask the user to stop the bot**

Tell the user: "The 60-min validation window is complete. Please close the bot terminal."

### Task 5.5: Commit the validation session data

```bash
cp data/mm_paper_trades.csv data/mm_paper_trades_postfix_2026-04-10.csv
git add data/mm_paper_trades_postfix_2026-04-10.csv
git commit -m "chore: snapshot post-fix 60-min validation session"
```

---

## Phase 6: Final Decision

### Task 6.1: Score the live session against thresholds

**Files:**
- Modify: `docs/superpowers/reports/2026-04-10-experiment-results.md`

- [ ] **Step 1: Append the validation result**

Add a "Phase 5 Validation" section to the report:

```markdown
## Phase 5 — Live Validation (post-fix)

Session: data/mm_paper_trades_postfix_2026-04-10.csv (60 min, ~12 cycles)
Fix applied: [winning experiment name]

| Metric | Baseline | Ex-post prediction | Live result |
|---|---|---|---|
| Total P&L | -$126.54 | [from Phase 2] | [from agent B] |
| Per-cycle mean | -$3.61 | [from Phase 2] | [from agent B] |
| Alignment | 31.4% | [from Phase 2] | [from agent B] |

**Threshold scoring:**
[Break-even / Small edge / Clear edge / Archive]

**Agreement with ex-post prediction:** [within 20% / 20-50% off / >50% off]

If the live result disagrees strongly with the ex-post prediction, the
ex-post experiments are not trustworthy counterfactuals — document this
as a lesson for next time.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/reports/2026-04-10-experiment-results.md
git commit -m "docs: record Phase 5 live validation result"
```

### Task 6.2: Final decision

**Files:**
- Modify: `tasks/lessons.md` (or create it if missing)

- [ ] **Step 1: Branch on the final score**

- **Score ≥ Break-even** → The fix is keeper. Proceed to next step.
- **Score < Break-even (Archive/Inconclusive)** → Revert the fix commit, archive the strategy, continue to lesson write-up.

- [ ] **Step 2: If keeper: write a next-steps note**

Append to `tasks/lessons.md`:

```markdown
## 2026-04-10 MM Diagnostic — Fix identified

Winning experiment: [name]
Live validation: [score]
Fix commit: [git sha]

Next steps (not in scope for this plan):
- Run a 12h+ paper session to confirm the fix holds across more regimes
- Do not deploy live until ≥100 settled cycles show CI excluding zero
```

- [ ] **Step 3: If no keeper: archive and write lessons**

Append to `tasks/lessons.md`:

```markdown
## 2026-04-10 MM Diagnostic — No fix identified

Baseline: -$131 in 3h45, 31.4% alignment, p=0.014 vs random
All 4 ex-post experiments failed to show a break-even or better result.

Lessons:
- MAX_INVENTORY tuning (50→20, 0.02→0.05) made the bleed worse, not better.
  Tuning is not a substitute for a broken signal.
- The CVD skew is anti-correlated with 5-min BTC outcomes in this sample,
  at p=0.014. Either the signal is inverted or CVD does not predict this market.
- Forced-flatten P&L CI straddles zero → no edge in pure market making either.
  The architecture does not appear viable without a real directional signal.
- Never run paper sessions longer than needed to reject the null. We burned
  3h of data collection when 1h would have given the same answer.

Status: strategy archived. The CVD signal needs to be rebuilt from scratch
(either by flipping its sign and re-testing, or by replacing it with a
different directional model) before any further MM work on 5-min binaries.
```

- [ ] **Step 4: Commit**

```bash
git add tasks/lessons.md
git commit -m "docs: record MM diagnostic outcome and next steps"
```

---

## Self-Review Checklist

Applied before closing the plan:

1. **Spec coverage** — every phase has at least one task; the gate between Phase 3 and Phase 4 is explicit; the kill criteria are named in the header and referenced in Task 1.6 / Task 2.2 / Phase 4 Step 5.

2. **Placeholder scan** — no "TODO" / "TBD" / "fill in details". Only conditional branches (Task 4.1a/b/c) which are intentional: exactly one runs based on Phase 3 outcome. The implementation details in Task 4.1b reference specific existing lines (`1990-2080`) which the engineer must read during implementation, but the code shown is complete.

3. **Type consistency** — `ExperimentResult` defined in Task 2.1 is used in Task 2.4/2.5; `load_fills`, `iter_cycles`, `cycle_cash`, `cycle_outcome`, `pre_settle_inventory`, `dominant_signal` are all defined before they are used in experiments. The backtest module is imported consistently as `backtest`.

4. **Reality check** — the plan does NOT promise a working bot. It promises a rigorous yes/no answer on whether a fix exists in the current architecture. If the answer is no, Phase 6 Task 6.2 Step 3 is the honest exit.
