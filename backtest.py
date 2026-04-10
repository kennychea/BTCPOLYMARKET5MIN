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


def iter_cycles(df: pd.DataFrame) -> Iterator[tuple[int, pd.DataFrame]]:
    """Yield (market_ts, cycle_rows) for each distinct cycle, in CSV order."""
    seen: list[int] = []
    for ts in df["market_ts"]:
        if ts not in seen:
            seen.append(ts)
    for ts in seen:
        yield ts, df[df["market_ts"] == ts].reset_index(drop=True)


def cycle_cash(cycle_rows: pd.DataFrame) -> float:
    """Compute total cash flow for one cycle (fills + rebates + settlement).

    Matches paper_dashboard.compute_portfolio accounting.
    Returns ~0 for flat cycles with no SETTLE row.
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
            fills = cycle_rows[cycle_rows["side"].isin(["BUY", "SELL"])]
            net = 0
            for _, f in fills.iterrows():
                net += int(f["size"]) if f["side"] == "BUY" else -int(f["size"])
            cash += net * row["price"]
    return cash


def cycle_outcome(cycle_rows: pd.DataFrame) -> str:
    """Return 'UP', 'DOWN', or 'FLAT' based on SETTLE row presence and price.

    - 'UP'   : a SETTLE row with price >= 0.5 (binary resolved true)
    - 'DOWN' : a SETTLE row with price < 0.5 (binary resolved false)
    - 'FLAT' : no SETTLE row (cycle ended without settlement)
    """
    settle = cycle_rows[cycle_rows["side"] == "SETTLE"]
    if len(settle) == 0:
        return "FLAT"
    price = float(settle.iloc[0]["price"])
    if price >= 0.5:
        return "UP"
    return "DOWN"


def pre_settle_inventory(cycle_rows: pd.DataFrame) -> int:
    """Return the net inventory (signed shares) from BUY/SELL rows only.

    Ignores SETTLE rows. Long positive, short negative, 0 if net-flat.
    """
    net = 0
    for _, row in cycle_rows.iterrows():
        if row["side"] == "BUY":
            net += int(row["size"])
        elif row["side"] == "SELL":
            net -= int(row["size"])
    return net


def dominant_signal(cycle_rows: pd.DataFrame) -> str:
    """Return the most frequent signal_type across BUY/SELL rows (excludes SETTLE).

    Returns 'NEUTRAL' if no trading rows. Pandas idxmax breaks ties by first-seen.
    """
    trading = cycle_rows[cycle_rows["side"].isin(["BUY", "SELL"])]
    if len(trading) == 0:
        return "NEUTRAL"
    counts = trading["signal_type"].value_counts()
    return str(counts.idxmax())
