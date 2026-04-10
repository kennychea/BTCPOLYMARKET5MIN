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

    Mirrors paper_dashboard.compute_portfolio's BUY/SELL/SETTLE accounting
    with a running net_position, so SETTLE price is applied to whatever
    net position accrued from fills *preceding* the SETTLE row (matching
    the dashboard's line-by-line iteration order).

    For cycles without a SETTLE row, returns only the BUY/SELL cash flow:
    realized rebates plus any spread captured when the bot opened and
    closed positions within the cycle. This is NOT "zero" — a market
    maker that bought at 0.49 and sold at 0.51 realizes +0.02 per unit
    of volume even in a "flat" cycle.
    """
    cash = 0.0
    net = 0
    for _, row in cycle_rows.iterrows():
        if row["side"] == "BUY":
            notional = row["price"] * row["size"]
            cash += -notional + notional * MAKER_REBATE_RATE
            net += int(row["size"])
        elif row["side"] == "SELL":
            notional = row["price"] * row["size"]
            cash += notional + notional * MAKER_REBATE_RATE
            net -= int(row["size"])
        elif row["side"] == "SETTLE":
            cash += net * row["price"]
            net = 0
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


# ═══════════════════════════════════════════════════════════════════════
# EXPERIMENTS — each returns ExperimentResult for cross-comparison
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
    """Experiment 0: pure replay, no modification. Must match dash['cash']."""
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
    Cash = sum of BUY/SELL fills (with rebates) + open position marked to $0.50.
    Isolates pure MM spread P&L from settlement exposure.
    """
    total = 0.0
    kept = 0
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
        # Mark any residual inventory to the binary midpoint
        inv = pre_settle_inventory(rows)
        cash += inv * 0.50
        total += cash
        kept += 1
        per_cycle.append(cash)
    mean = sum(per_cycle) / len(per_cycle) if per_cycle else 0.0
    # Alignment is N/A when forced-flat (no position at settlement)
    return ExperimentResult("forced_flatten_0.50", total, kept, 0, 0, 0, 0.0, mean)


def experiment_half_cap(df: pd.DataFrame) -> ExperimentResult:
    """Experiment 3: truncate all fills that would push |inventory| past 10.

    Simulates MM_MAX_INVENTORY=10 (half the current cap). Drops fills that
    couldn't have happened under the tighter cap. NB: imperfect simulation
    because the bot's subsequent fills would differ under a real tighter
    cap — treat as directional evidence only, not a true counterfactual.
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
                net = 0
                continue
            delta = int(r["size"]) if r["side"] == "BUY" else -int(r["size"])
            if abs(net + delta) > 10:
                continue  # simulated cap rejects this fill
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
        # Alignment: recompute pre-settle net under the simulated cap
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
