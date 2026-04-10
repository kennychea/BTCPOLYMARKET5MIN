"""Paper-trade statistical gate for the stink validation harness.

Pure logic + single sentinel file I/O. No network, no state. Called at the
end of every market cycle in cvd_5min_bot.main(); halts the main loop when
the gate reaches a terminal state (PASS, KILL, or INCONCLUSIVE).

Thresholds hardened 2026-04-11 after a 3-agent calibration review:
  - Agent 1 (fee model): the old FEE_RATE=0.0315 was factually wrong.
    Polymarket Crypto category fees are a symmetric curve
    `shares * 0.072 * price * (1 - price)`, peak 1.80% at price=0.50,
    paid at fill time on BOTH winners and losers. See
    https://docs.polymarket.com/trading/fees.
  - Agent 2 (sensitivity): at n=60 the p-value gate dominates winrate and
    EV constraints trivially. PASS_EV_PER_TRADE was tautological at realistic
    stink prices — removed from the PASS decision.
  - Agent 3 (power): at PASS_P_VALUE=0.05 the Type-I error is ~5%; raising
    MIN_N_FOR_DECISION to 120 and tightening p to 0.01 reduces false-PASS
    to ~1% without losing power against a true 0.60 edge.
  - Layered KILL: stricter warmup threshold for n in [30, 50) catches
    catastrophes earlier. A dollar-drawdown kill is added as a defense-in-
    depth safety net that is independent of the binomial test — it fires
    if total EV-per-trade cumulated across all trades exceeds KILL_TOTAL_LOSS.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import comb

import pandas as pd

# ── Gate thresholds (hardened 2026-04-11) ─────────────────────────────
MIN_N_FOR_DECISION = 120   # was 60 — below 120 the binomial gate is untrustworthy
KILL_N = 30                # earliest possible KILL
KILL_WARMUP_N_MAX = 50     # warmup window boundary
KILL_WARMUP_WR = 0.50      # stricter WR threshold for n in [KILL_N, KILL_WARMUP_N_MAX)
KILL_WINRATE = 0.45        # standard WR threshold for n >= KILL_WARMUP_N_MAX
KILL_TOTAL_LOSS = -50.0    # dollar-drawdown KILL (independent of binomial gate)
PASS_WINRATE = 0.58        # kept as a sanity floor (slack at n>=120 under tight p)
PASS_P_VALUE = 0.01        # was 0.05 — primary α reduction for Type-I control
# PASS_EV_PER_TRADE removed from PASS decision; EV is still computed and
# reported in GateStatus.ev_per_trade for operator visibility.
CRYPTO_FEE_RATE = 0.072    # was 0.0315 — Polymarket Crypto category curve constant
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


def _is_filled(v) -> bool:
    """Robust check for the `filled` column across bool/int/float/str variants.

    `df["filled"].astype(bool)` is unsafe because bool("False") == True for any
    non-empty string, and bool(float('nan')) == True — a latent footgun for a
    safety gate that sees CSV round-trips and column-dtype drift.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if isinstance(v, float) and math.isnan(v):
            return False
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return False


def compute_pnl_per_trade(row: pd.Series) -> float:
    """Net PnL for one filled+settled stink trade.

    Polymarket Crypto fee model (hardened 2026-04-11):
      fee = shares * CRYPTO_FEE_RATE * entry * (1 - entry)

    This is the official symmetric-curve formula with peak 1.80% at
    entry=0.50. Fees are paid at fill time on BOTH winners and losers.
    """
    payout = 1.0 if row["signal_correct_polymarket"] == "YES" else 0.0
    shares = float(row["shares"])
    entry = float(row["stink_price"])
    gross = shares * (payout - entry)
    fee = shares * CRYPTO_FEE_RATE * entry * (1.0 - entry)
    return gross - fee


def evaluate(df: pd.DataFrame) -> GateStatus:
    """Evaluate the gate over all filled + settled trades in the DataFrame.

    Only rows where `filled == True` AND `signal_correct_polymarket` is
    "YES" or "NO" contribute to n. PENDING, UNKNOWN, or unfilled rows are
    excluded.

    Decision tree (evaluated in order):
      1. Dollar-drawdown KILL — total_ev <= KILL_TOTAL_LOSS (defense in depth)
      2. Layered binomial KILL:
         - warmup window [KILL_N, KILL_WARMUP_N_MAX): winrate < KILL_WARMUP_WR
         - standard window n >= KILL_WARMUP_N_MAX: winrate < KILL_WINRATE
      3. PASS — all three conditions: n >= MIN_N_FOR_DECISION,
         winrate >= PASS_WINRATE, p_value <= PASS_P_VALUE
      4. INCONCLUSIVE — n >= MIN_N_FOR_DECISION but PASS unmet
      5. CONTINUE — accumulating
    """
    if len(df) == 0:
        return GateStatus("CONTINUE", "no trades logged yet",
                          0, 0.0, 1.0, 0.0, _now_iso())

    settled = df[
        df["filled"].apply(_is_filled) &
        df["signal_correct_polymarket"].isin(["YES", "NO"])
    ]
    n = len(settled)
    if n == 0:
        return GateStatus("CONTINUE", "no settled trades yet",
                          0, 0.0, 1.0, 0.0, _now_iso())

    wins = int((settled["signal_correct_polymarket"] == "YES").sum())
    winrate = wins / n
    p_value = one_sided_binomial_p(wins, n, 0.5)
    per_trade_pnl = settled.apply(compute_pnl_per_trade, axis=1)
    ev = float(per_trade_pnl.mean())
    total_pnl = float(per_trade_pnl.sum())

    # 1. Dollar-drawdown KILL — agnostic to the binomial assumption.
    #    Fires regardless of n or winrate if cumulative simulated loss
    #    exceeds the budget. Defense in depth.
    if total_pnl <= KILL_TOTAL_LOSS:
        return GateStatus(
            "KILL",
            f"total simulated loss ${total_pnl:.2f} <= KILL_TOTAL_LOSS ${KILL_TOTAL_LOSS:.2f}",
            n, winrate, p_value, ev, _now_iso(),
        )

    # 2. Layered binomial KILL.
    if KILL_N <= n < KILL_WARMUP_N_MAX and winrate < KILL_WARMUP_WR:
        return GateStatus(
            "KILL",
            f"warmup: n={n} in [{KILL_N},{KILL_WARMUP_N_MAX}) "
            f"AND winrate={winrate:.1%} < {KILL_WARMUP_WR:.0%}",
            n, winrate, p_value, ev, _now_iso(),
        )
    if n >= KILL_WARMUP_N_MAX and winrate < KILL_WINRATE:
        return GateStatus(
            "KILL",
            f"n={n} >= {KILL_WARMUP_N_MAX} AND winrate={winrate:.1%} < {KILL_WINRATE:.0%}",
            n, winrate, p_value, ev, _now_iso(),
        )

    # 3. PASS — n + winrate + p_value. EV no longer votes (it was tautological
    #    at realistic stink prices; see the module docstring).
    if (n >= MIN_N_FOR_DECISION
            and winrate >= PASS_WINRATE
            and p_value <= PASS_P_VALUE):
        return GateStatus(
            "PASS",
            f"all gates met: n={n}, winrate={winrate:.1%}, "
            f"p={p_value:.4f}, EV=${ev:.2f}",
            n, winrate, p_value, ev, _now_iso(),
        )

    # 4. INCONCLUSIVE — sufficient n, but PASS conditions unmet.
    if n >= MIN_N_FOR_DECISION:
        return GateStatus(
            "INCONCLUSIVE",
            f"n={n} sufficient but gates unmet: "
            f"winrate={winrate:.1%}, p={p_value:.4f}, EV=${ev:.2f}",
            n, winrate, p_value, ev, _now_iso(),
        )

    # 5. CONTINUE — still accumulating.
    return GateStatus(
        "CONTINUE",
        f"accumulating: n={n}, winrate={winrate:.1%}",
        n, winrate, p_value, ev, _now_iso(),
    )


def write_sentinel(status: GateStatus, path: str = SENTINEL_PATH) -> None:
    """Write the gate status to a JSON sentinel file. Atomic replace to
    guarantee that a partial write on crash does not leave a zero-byte
    sentinel that read_sentinel would swallow as None (permissive)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(status), f, indent=2)
    os.replace(tmp, path)


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
