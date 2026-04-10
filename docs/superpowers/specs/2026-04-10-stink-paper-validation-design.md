# Stink Paper Validation — Design Spec

**Date:** 2026-04-10
**Status:** Draft, pending user review
**Topic:** Rigorous paper-trade validation of the `stink` strategy with CVD signal inversion hypothesis

---

## Goal

Validate or reject the hypothesis that the CVD signal produced by `check_cvd_signal()` is anti-correlated with Polymarket 5-minute binary outcomes, by running the `stink` strategy in paper mode with direction inversion, rigorous truth-source measurement, and a mechanical statistical gate that auto-halts the bot on PASS or KILL.

## Context & motivation

The earlier session of 2026-04-10 ran the `mm` (market maker) strategy in paper mode for 3h45 and produced a catastrophic result: −$269 cash, 64 settled cycles, alignment 17/64 = 26.6% (p=1.1×10⁻⁴ one-sided binomial vs 0.5). The Phase 3 diagnostic report `docs/superpowers/reports/2026-04-10-experiment-results.md` established that the CVD signal is strongly anti-correlated, not noise. The `forced_flatten_0.50` counterfactual further proved that pure MM spread P&L was essentially zero, meaning the loss came entirely from the directional exposure driven by the inverted signal. The MM strategy was archived in Phase 6 (`docs/superpowers/reports/2026-04-10-phase6-decision.md`).

The `stink` strategy uses the **same** `check_cvd_signal()` function. It is therefore expected to suffer the same signal inversion, unless the cheap fix of flipping the bet direction produces a genuine edge. This spec designs the harness to test that fix empirically, with a statistical gate strict enough to prevent the false-positive that nearly deployed neutral_only (p=0.227, n=16).

## Scope

### In scope
- Add an `CVD_INVERT_SIGNAL` env flag that flips the direction returned by `check_cvd_signal()`.
- Fix the existing `resolve_paper_outcome()` truth-source bug by adding Polymarket Gamma (primary, Chainlink-derived) and Binance spot (independent cross-check) alongside the current Binance perp source.
- Add a `paper_gate.py` module that mechanically evaluates PASS / KILL / CONTINUE after each settled trade and writes a sentinel file.
- Add a main-loop integration that reads the sentinel and auto-halts on PASS or KILL.
- Support session-resume across bot restarts by accumulating trades in `paper_trades.csv` without truncation.
- TDD test coverage for all new code.

### Out of scope (explicit exclusions)
- No Hyperliquid hedge. The stink strategy as implemented has `hedge_size_btc=0` historically; EV math is Polymarket-only.
- No live dashboard. Gate status is written to a JSON sentinel; operator reads the CSV and the sentinel manually if they want visibility.
- No automatic pivot to a new signal source if the gate KILLs. A KILL outcome terminates this spec's work; pivot to a new signal is a separate brainstorming + spec cycle.
- No fix of the latent `paper_dashboard.py:88-103` cycle_cash leak-forward bug (tracked separately).
- No retuning of CVD signal thresholds (CVD_MIN_DIV, pullback pct, etc.). The only variable under test is the sign of the bet direction.

## Success criteria (gate thresholds)

All thresholds frozen during brainstorming. Sample-size-aware to prevent the `neutral_only` false-positive pattern.

| Gate | Condition | Outcome |
|---|---|---|
| KILL | `n ≥ 30 AND winrate < 0.45` | Halt bot, archive strategy, log "KILL — strategy failed gate" |
| PASS | `n ≥ 60 AND winrate ≥ 0.58 AND p_value ≤ 0.05 AND ev_per_trade ≥ $0.20` | Halt bot, log "PASS — ready for live review" |
| INCONCLUSIVE | `n ≥ 60 AND (any PASS condition unmet)` | Halt bot, log "INCONCLUSIVE — extend session or archive" |
| CONTINUE | Any other state | Loop continues |

- `n` = number of filled AND settled trades where `outcome_polymarket ∈ {UP, DOWN}`. Excludes PENDING trades and stink-bids that expired unfilled.
- `winrate` = `wins / n` where `wins = count(signal_correct_polymarket == "YES")`.
- `p_value` = one-sided binomial probability `P(X ≥ wins | n, p=0.5)`. Computed with `math.comb` (no scipy dependency).
- `ev_per_trade` = mean of `pnl_per_trade` across filled+settled trades. `pnl_per_trade = shares × (payout - entry_price) - fees` where `payout ∈ {0, 1}` and `fees = shares × 1 × 0.0315` (Polymarket dynamic fees per CLAUDE.md §Domain Rules #4, applied on winning side notional).

## Architecture

Three independent components, each with a clear boundary and unit-testable interface.

### Component 1: Signal flip

**Responsibility:** Conditionally invert the `direction` returned by `check_cvd_signal()` without touching signal detection logic.

**Interface:**
- New module-level constant in `cvd_5min_bot.py` near line 146 (after `MM_MAX_CVD_SKEW`):
  ```python
  CVD_INVERT_SIGNAL = os.getenv("CVD_INVERT_SIGNAL", "false").lower() == "true"
  ```
- Modification inside `check_cvd_signal()` at line ~440: immediately before the return, if `CVD_INVERT_SIGNAL and direction in ("UP", "DOWN")`, swap `direction`. The `signal_type` label (BULLISH_DIV, STRONG_BULL, etc.) is NOT renamed — only the bet target flips.

**Consumers touched:**
- `CVDStinkBot.run_market_cycle` (line ~1745): receives flipped direction transparently, no code change needed.
- `CVDMarketMaker.compute_cvd_skew`: this method has its own skew derivation and is not affected by the flip. Since MM is archived (`STRATEGY=stink`), no real risk.

**Default safety:** When the env var is unset, absent, or anything other than exactly `"true"`, the constant evaluates `False` and `check_cvd_signal` behaves identically to current code. Backwards-compat is absolute.

### Component 2: Dual-source outcome resolver

**Responsibility:** Determine whether a stink trade was "correct" using the true settlement source, with parallel recording of two additional sources for cross-check.

**New file: `truth_sources.py`**

Two functions, each independently testable with mocked HTTP:

```python
def fetch_binance_spot_price(timestamp: int, timeout_sec: float = 5.0) -> float | None:
    """Fetch BTC/USDT spot price from Binance at the given unix timestamp.

    Uses the public Binance Klines REST endpoint:
        GET https://api.binance.com/api/v3/klines
            ?symbol=BTCUSDT&interval=1m&startTime={ts_ms}&limit=1

    Returns the close price of the 1-minute candle containing the given timestamp.
    Returns None on timeout, HTTP error, or empty response.

    Why spot, not perp: Polymarket's settlement target (Chainlink BTC/USD) tracks
    spot, not perp-futures. Perp diverges from spot due to funding rate and
    basis, which adds noise to the cross-check. Spot is closer to the truth
    source Polymarket actually uses, without requiring Chainlink on-chain access.
    """

def fetch_polymarket_resolution(condition_id: str, timeout_sec: float = 5.0) -> str | None:
    """Fetch the resolved price from Polymarket Gamma API for the given condition ID.

    Endpoint: GET https://gamma-api.polymarket.com/markets?condition_ids={cid}
    Reads the market's resolvedPrice field from the JSON response.

    Returns 'UP' if resolvedPrice > 0.5, 'DOWN' if < 0.5, 'PENDING' if the market
    has not yet been resolved (resolvedPrice is null), None on HTTP error.

    This is the PRIMARY truth source. Polymarket settles via Chainlink internally,
    so the Gamma API resolvedPrice IS the Chainlink-derived outcome without the
    complexity of re-fetching Chainlink on-chain.
    """
```

**Modified function: `resolve_paper_outcome()` in `cvd_5min_bot.py` (line 951)**

Current behavior: reads `paper_trades.csv`, computes outcome from `feed.get_last_price()` (Binance perp), writes `market_outcome` and `signal_correct` columns.

New behavior:
1. Keep the Binance perp outcome but rename the column to `outcome_binance_perp` and add a corresponding `signal_correct_binance_perp` (kept for historical continuity and perp-vs-spot drift detection).
2. Call `fetch_binance_spot_price(market_ts + 300)` and compute `outcome_binance_spot` + `signal_correct_binance_spot`. This is the perp-independent spot cross-check.
3. Call `fetch_polymarket_resolution(condition_id)` and compute `outcome_polymarket` + `signal_correct_polymarket`. If the API returns `PENDING`, write `"PENDING"` in both columns and leave the trade re-evaluable.
4. All three fetches run sequentially with per-call timeout (no ThreadPoolExecutor in v1 — simpler, lower risk of thread-safety bugs with pandas).
5. The gate (Component 3) reads only `signal_correct_polymarket` for decisions. The other two columns exist for cross-check and debugging.

**New CSV schema** (backward-compatible via `pd.read_csv` with `fillna`):

| Column | Type | Notes |
|---|---|---|
| timestamp | str ISO | unchanged |
| market_ts | int | unchanged |
| market_slug | str | unchanged |
| condition_id | str | **new** — needed for Polymarket fetch |
| signal_type | str | unchanged (BULLISH_DIV, etc.) |
| direction | str | **now reflects flipped direction** if CVD_INVERT_SIGNAL=true |
| direction_original | str | **new** — the pre-flip direction, for audit |
| invert_flag | bool | **new** — snapshot of CVD_INVERT_SIGNAL at trade time, for audit across flag changes |
| detail | str | unchanged |
| stink_price | float | unchanged |
| shares | int | **new** — shares sized from ORDER_SIZE_USD / stink_price |
| btc_price_at_signal | float | unchanged (Binance perp) |
| btc_price_at_close | float | unchanged (Binance perp) |
| outcome_binance_perp | str | renamed from market_outcome |
| outcome_binance_spot | str | **new** — cross-check via Binance Klines REST |
| outcome_polymarket | str | **new** — primary truth source |
| signal_correct_binance_perp | str | renamed from signal_correct |
| signal_correct_binance_spot | str | **new** |
| signal_correct_polymarket | str | **new** — drives the gate |
| price_change_pct | float | unchanged (perp-derived, kept for debug) |
| filled | bool | **new** — True if the stink bid was filled, False if expired. Unfilled trades are logged but excluded from the gate. |

**PENDING retry logic:** The main loop re-calls `resolve_paper_outcome()` after every cycle, not just the cycle that just ended. This naturally retries any PENDING trades from earlier cycles. No background thread needed.

### Component 3: Auto-stop stats gate

**New file: `paper_gate.py`**

Pure functions, no I/O except the single sentinel write. Unit-testable with pandas DataFrame fixtures.

```python
from dataclasses import dataclass, asdict
from math import comb
import json
import pandas as pd

MIN_N_FOR_DECISION = 60
KILL_N = 30
KILL_WINRATE = 0.45
PASS_WINRATE = 0.58
PASS_P_VALUE = 0.05
PASS_EV_PER_TRADE = 0.20
FEE_RATE = 0.0315
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
    """P(X >= wins | n, p) using math.comb (no scipy)."""
    return sum(comb(n, k) * (p ** k) * ((1 - p) ** (n - k)) for k in range(wins, n + 1))

def compute_pnl_per_trade(row: pd.Series) -> float:
    """Net PnL for one filled+settled stink trade."""
    if row["signal_correct_polymarket"] == "YES":
        payout = 1.0
    else:
        payout = 0.0
    shares = row["shares"]
    entry = row["stink_price"]
    gross = shares * (payout - entry)
    fees = shares * payout * FEE_RATE  # only charged on winning side notional
    return gross - fees

def evaluate(df: pd.DataFrame) -> GateStatus:
    """Evaluate the gate over settled trades only."""
    settled = df[
        df["filled"].astype(bool) &
        df["signal_correct_polymarket"].isin(["YES", "NO"])
    ]
    n = len(settled)
    if n == 0:
        return GateStatus("CONTINUE", "no settled trades yet", 0, 0.0, 1.0, 0.0, _now_iso())
    wins = int((settled["signal_correct_polymarket"] == "YES").sum())
    winrate = wins / n
    p_value = one_sided_binomial_p(wins, n, 0.5)
    ev = float(settled.apply(compute_pnl_per_trade, axis=1).mean())

    # KILL gate (checked first — fast exit on failure)
    if n >= KILL_N and winrate < KILL_WINRATE:
        return GateStatus("KILL",
            f"n={n} ≥ {KILL_N} AND winrate={winrate:.1%} < {KILL_WINRATE:.0%}",
            n, winrate, p_value, ev, _now_iso())

    # PASS gate (all four conditions)
    if (n >= MIN_N_FOR_DECISION
        and winrate >= PASS_WINRATE
        and p_value <= PASS_P_VALUE
        and ev >= PASS_EV_PER_TRADE):
        return GateStatus("PASS",
            f"all gates met: n={n}, winrate={winrate:.1%}, p={p_value:.4f}, EV=${ev:.2f}",
            n, winrate, p_value, ev, _now_iso())

    # INCONCLUSIVE: enough n but not all gates met
    if n >= MIN_N_FOR_DECISION:
        return GateStatus("INCONCLUSIVE",
            f"n={n} sufficient but gates unmet: winrate={winrate:.1%}, p={p_value:.4f}, EV=${ev:.2f}",
            n, winrate, p_value, ev, _now_iso())

    return GateStatus("CONTINUE",
        f"accumulating: n={n}, winrate={winrate:.1%}",
        n, winrate, p_value, ev, _now_iso())

def write_sentinel(status: GateStatus, path: str = SENTINEL_PATH) -> None:
    with open(path, "w") as f:
        json.dump(asdict(status), f, indent=2)

def read_sentinel(path: str = SENTINEL_PATH) -> GateStatus | None:
    try:
        with open(path) as f:
            data = json.load(f)
        return GateStatus(**data)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
```

**Integration point in `cvd_5min_bot.py`:**

1. **At bot startup** (near line 2207, main loop entry): read `paper_gate.read_sentinel()`. If the previous run left a sentinel with `status == "KILL"`, refuse to start unless the operator has deleted the sentinel file. Print the reason and exit. This prevents blind restart of a KILLED strategy.

2. **At end of every cycle** (after `resolve_paper_outcome()` returns): read `paper_trades.csv` into a DataFrame, call `paper_gate.evaluate(df)`, write the sentinel. If `status.status in ("PASS", "KILL", "INCONCLUSIVE")`, print a multi-line summary and `break` out of the main loop.

3. **At bot boot** (startup banner): print `[PAPER_GATE] CVD_INVERT_SIGNAL=<value> — direction flip <active|inactive>` so the operator visually confirms the flag state before letting the bot trade.

## Data flow

```
bot startup
  ↓
read sentinel → KILL? refuse start. else continue.
  ↓
main loop cycle N:
  ↓
  check_cvd_signal()  ← CVD_INVERT_SIGNAL flips direction here
  ↓
  place stink bid (paper)  ← logged to CSV with invert_flag + direction_original
  ↓
  wait for fill / expire
  ↓
  market_ts + 300 reached: resolve_paper_outcome()
    ├── fetch_polymarket_resolution(condition_id) → outcome_polymarket (may be PENDING)
    ├── fetch_binance_spot_price(market_ts + 300) → outcome_binance_spot
    └── feed.get_last_price() → outcome_binance_perp
  ↓
  re-run resolve for any earlier PENDING trades
  ↓
  paper_gate.evaluate(paper_trades.csv) → GateStatus
  ↓
  write_sentinel(status)
  ↓
  status in {PASS, KILL, INCONCLUSIVE}? → print summary + break
  status == CONTINUE? → next cycle
```

## Error handling & safety

| Failure mode | Detection | Recovery |
|---|---|---|
| Binance spot fetch timeout/HTTP error | `fetch_binance_spot_price` returns None | Write `outcome_binance_spot = "UNKNOWN"`. Trade still counts via Polymarket source. Log warning. |
| Polymarket not yet resolved | Gamma API returns `resolvedPrice = null` | Write `outcome_polymarket = "PENDING"`. Retry next cycle. Trade excluded from gate until resolved. |
| Polymarket fetch HTTP error | Exception or non-200 | Write `outcome_polymarket = "UNKNOWN"`, **do not retry** (distinguishes from PENDING). Trade excluded from gate permanently. Log warning. |
| Binance spot 429 rate-limit | HTTP 429 status | Exponential backoff 1s → 2s → 4s, max 3 attempts, then UNKNOWN. |
| Bot crash mid-cycle | Process death | Next run reads `paper_trades.csv`, any trades without resolved outcomes are re-resolved at next cycle's end. Gate accumulates naturally. |
| paper_trades.csv corruption | pandas parse error | Bot refuses to start, log explicit error message pointing to the file. Operator investigates manually. No automatic repair. |
| Sentinel file exists with KILL status | Startup check | Refuse to start, instruct operator to delete the sentinel if intentional restart. |
| CVD_INVERT_SIGNAL flag silently misread | Startup log line | Operator sees `[PAPER_GATE] CVD_INVERT_SIGNAL=True|False — direction flip active|inactive` at every boot. |
| Test coverage gap on signal flip | TDD | 4 unit tests specifically target flip on/off × UP/DOWN/NEUTRAL. |
| Fee rate drift from CLAUDE.md stated 3.15% | Hard-coded in paper_gate.py | Documented in spec as frozen for this validation; if fees change materially, re-run the gate evaluation retrospectively on the same CSV. |

**Safety module integration:** `paper_gate.py` opens no trades, places no orders. It only halts the main loop. It is purely read-only on external systems and write-only on the local sentinel + CSV. No new risk surface versus CLAUDE.md §Domain Rules.

## Testing approach (TDD strict)

Every new function gets a failing test before its implementation. Run order: write test → run → confirm RED → implement → run → confirm GREEN → commit.

### Unit tests

**`tests/unit/test_signal_flip.py`** (4 tests)
- `test_flip_disabled_returns_original`: `monkeypatch.setattr(bot, "CVD_INVERT_SIGNAL", False)`, stub a BULLISH_DIV signal, assert direction = "UP".
- `test_flip_enabled_up_becomes_down`: flag True, stub forces direction="UP", assert returned direction = "DOWN".
- `test_flip_enabled_down_becomes_up`: symmetric.
- `test_flip_neutral_unchanged`: flag True, stub forces NEUTRAL/None, assert unchanged.

**`tests/unit/test_truth_sources.py`** (~7 tests)
- `test_fetch_binance_spot_success`: mock HTTP 200 with sample Klines JSON `[[openTime, open, high, low, close, ...]]`, assert parsed close float.
- `test_fetch_binance_spot_timeout`: mock `requests.Timeout`, assert returns None.
- `test_fetch_binance_spot_http_error`: mock HTTP 500, assert returns None.
- `test_fetch_binance_spot_empty_response`: mock HTTP 200 with `[]`, assert returns None.
- `test_fetch_polymarket_pending`: mock `resolvedPrice=null`, assert "PENDING".
- `test_fetch_polymarket_resolved_up`: mock `resolvedPrice=1.0`, assert "UP".
- `test_fetch_polymarket_resolved_down`: mock `resolvedPrice=0.0`, assert "DOWN".
- `test_fetch_polymarket_http_error`: mock 500, assert returns None (distinct from PENDING).

**`tests/unit/test_paper_gate.py`** (~10 tests)
- `test_empty_df_returns_continue`
- `test_only_pending_trades_returns_continue`
- `test_only_unfilled_trades_returns_continue`
- `test_below_kill_n_bad_winrate_continues`: n=29, winrate=0.30 → CONTINUE
- `test_kill_at_kill_n`: n=30, winrate=0.40 → KILL
- `test_above_kill_n_acceptable_winrate_continues`: n=30, winrate=0.50 → CONTINUE (no KILL, no PASS)
- `test_below_min_n_high_winrate_continues`: n=50, winrate=0.70 → CONTINUE
- `test_pass_all_gates`: n=60, winrate=0.62, ev=0.25 → PASS
- `test_inconclusive_ev_too_low`: n=60, winrate=0.62, p<0.05, ev=0.15 → INCONCLUSIVE
- `test_inconclusive_p_too_high`: n=60, winrate=0.56, p=0.18, ev=0.25 → INCONCLUSIVE
- `test_pnl_computation_winning_trade`
- `test_pnl_computation_losing_trade`
- `test_binomial_p_value_matches_scipy_reference`: three reference (wins, n) pairs with known scipy values, assert our `one_sided_binomial_p` matches to 1e-9.

### Integration test

**`tests/integration/test_paper_flow.py`** (1 test)
- Generates a synthetic `paper_trades.csv` with 70 filled+settled trades, 50 of which are "YES" (71.4% winrate, p≈0.0004, EV≈$0.30). Calls `paper_gate.evaluate()`, asserts status = PASS and the sentinel file is written with correct contents.

### Pre-flight smoke test

**`scripts/smoke_paper_gate.py`** (not a test, an operator tool)
- Loads `data/mm_paper_trades_baseline_2026-04-10.csv` (the archived MM CSV).
- Extracts the cycles as if they were stink trades (approximation — the MM CSV has different structure, so we compute alignment per cycle and treat each cycle as 1 "trade").
- Runs `paper_gate.evaluate()` on the reconstructed DataFrame.
- Prints what the gate would have said on the real MM session: expected `KILL` at n=30, winrate ≈ 26.6%.
- **This is a sanity check that the thresholds are realistic before committing 5h of paper time.** If smoke_paper_gate doesn't KILL on the MM baseline data, the thresholds are wrong and the spec needs adjustment before implementation.

### Full suite regression

After every commit, run `pytest tests/` and confirm the prior 92 tests still pass plus all new tests. Kill criterion: any pre-existing test breaking = stop and investigate.

## Files touched

**New files:**
- `truth_sources.py` — 2 fetch functions, ~60 LOC
- `paper_gate.py` — dataclass + evaluate + sentinel I/O, ~100 LOC
- `tests/unit/test_signal_flip.py` — ~40 LOC
- `tests/unit/test_truth_sources.py` — ~80 LOC
- `tests/unit/test_paper_gate.py` — ~200 LOC
- `tests/integration/test_paper_flow.py` — ~40 LOC
- `scripts/smoke_paper_gate.py` — ~40 LOC

**Modified files:**
- `cvd_5min_bot.py`:
  - New constant `CVD_INVERT_SIGNAL` near line 146
  - Flip logic inside `check_cvd_signal()` near line 440 (~5 LOC)
  - Rewrite of `resolve_paper_outcome()` at line 951 (~60 LOC net add)
  - New logging in `log_paper_signal()` for `condition_id`, `shares`, `direction_original`, `invert_flag`, `filled` columns (~10 LOC)
  - Main-loop sentinel integration near line 2207 (~15 LOC)
  - Startup banner line for `CVD_INVERT_SIGNAL` state (~2 LOC)
- `.env.example`:
  - Add `CVD_INVERT_SIGNAL=false` with comment explaining the hypothesis it tests

**Untouched (explicitly):**
- `paper_dashboard.py` (leak-forward bug is separate scope)
- `backtest.py` (backtest harness stays as-is)
- Existing MM code (`CVDMarketMaker`, `MMInventory`, `MM_NEUTRAL_ONLY` gate)
- `.env` (the operator sets `CVD_INVERT_SIGNAL=true` manually when ready to run Phase B)

## Operator runbook (how to use this)

1. `git pull` the completed implementation.
2. Run pre-flight smoke: `python scripts/smoke_paper_gate.py` — confirms gate would KILL on the MM baseline.
3. Ensure `PAPER_MODE=true` and `STRATEGY=stink` in `.env`.
4. Set `CVD_INVERT_SIGNAL=true` in `.env`.
5. Backup or clear `data/paper_trades.csv` if starting a fresh run (the gate accumulates, so stale rows from a prior run will pollute).
6. Delete `data/paper_gate_status.json` if it exists with KILL status (startup will refuse otherwise).
7. `python cvd_5min_bot.py` — bot runs until the gate halts it.
8. When the bot halts, read `data/paper_gate_status.json` for the summary. Read `data/paper_trades.csv` for per-trade detail. Cross-check the three outcome columns (`outcome_polymarket`, `outcome_binance_spot`, `outcome_binance_perp`) for drift between the primary truth source and the two independent references.
9. On PASS: brainstorm live deployment in a separate spec.
10. On KILL: brainstorm pivot to a new signal source in a separate spec.
11. On INCONCLUSIVE: brainstorm session extension criteria in a separate spec.

## Decision log

Questions asked during brainstorming and answers chosen:

| # | Question | Answer |
|---|---|---|
| 1 | Which strategy to paper trade? | B — `stink` with flipped CVD sign (same signal family, cheapest fix) |
| 2 | Success criterion? | A — strict: n≥60, winrate≥58%, p≤0.05, EV≥+$0.20, kill at n=30 if <45% |
| 3 | Truth source for outcomes? | D — Polymarket Gamma (Chainlink-derived, primary) + Binance spot (cross-check), Binance perp kept for historical continuity. Self-review downgraded direct Chainlink fetch to Gamma API due to on-chain complexity. |
| 4 | Automation level? | A+D — auto-stop on gates + session-resume across runs |
| 5 | EV model? | A + (iii) — Polymarket-only, fees 3.15%, no-fills tracked but excluded from gate |
| 6 | Scope? | B only in this spec; C (new signal) is a separate brainstorm if B kills |

## References

- Prior diagnostic: `docs/superpowers/plans/2026-04-10-mm-diagnostic-and-fix.md`
- Baseline report: `docs/superpowers/reports/2026-04-10-experiment-results.md`
- Phase 6 archive decision: `docs/superpowers/reports/2026-04-10-phase6-decision.md`
- Latent bug (out of scope but tracked): `paper_dashboard.py:88-103` — cycle_cash leak-forward
- Lessons from prior session: `tasks/lessons.md` section "2026-04-10 — CVD Market Maker Diagnostic"
