"""Pre-flight smoke test: run paper_gate on the archived MM baseline CSV.

The MM baseline has 64 settled cycles with 17 GOOD alignment (26.6%, p<10^-4).
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
        print("\nExpected: MM baseline KILLED as intended. Gate thresholds are sane.", flush=True)
        return 0
    else:
        print(
            f"\nExpected KILL, got {status.status}. "
            "Gate thresholds may be miscalibrated - review spec before running real paper.",
            flush=True,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
