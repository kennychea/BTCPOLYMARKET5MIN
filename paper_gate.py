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
