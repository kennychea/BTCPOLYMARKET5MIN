"""Integration test: synthetic paper_trades.csv → paper_gate.evaluate → sentinel.

Updated 2026-04-11 for hardened thresholds:
  MIN_N_FOR_DECISION = 120
  PASS_P_VALUE = 0.01
  CRYPTO_FEE_RATE = 0.072 (symmetric curve; fees on both winners and losers)
  KILL_WARMUP_WR = 0.50 for n in [30, 50)
  KILL_TOTAL_LOSS = -50.0 dollar drawdown
"""
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
    """140 trades, 90 wins (64.3% winrate) at stink_price 0.40 → PASS.

    Under hardened thresholds:
      n = 140 >= MIN_N_FOR_DECISION (120) ✓
      winrate = 0.643 >= PASS_WINRATE (0.58) ✓
      p-value(90, 140, 0.5) ≈ 5e-4 <= PASS_P_VALUE (0.01) ✓

    PnL sanity:
      win  = 10*(1-0.4) - 10*0.072*0.4*0.6 = 6.0 - 0.1728 = 5.8272
      loss = 10*(0-0.4) - 0.1728          = -4.1728
      total = 90*5.8272 + 50*(-4.1728) = 524.45 - 208.64 = 315.81 > 0 (no dollar KILL)
    """
    rows = [_fake_row("YES", market_ts=1_000_000 + i * 300) for i in range(90)]
    rows += [_fake_row("NO", market_ts=1_100_000 + i * 300) for i in range(50)]
    df = pd.DataFrame(rows)

    sentinel_path = str(tmp_path / "paper_gate_status.json")
    status = paper_gate.evaluate(df)
    paper_gate.write_sentinel(status, sentinel_path)

    assert status.status == "PASS"
    assert status.n == 140
    assert status.winrate == pytest.approx(90 / 140, abs=1e-9)
    assert status.p_value < paper_gate.PASS_P_VALUE
    assert status.ev_per_trade > 0.0

    # Sentinel round-trip
    with open(sentinel_path) as f:
        data = json.load(f)
    assert data["status"] == "PASS"
    assert data["n"] == 140


def test_synthetic_kill_flow(tmp_path):
    """40 trades, 8 wins (20% winrate) → KILL.

    At n=40 this sits in the warmup window [30, 50) and winrate 0.20 < 0.50,
    so the warmup binomial KILL fires. Dollar-drawdown KILL would also fire
    independently (total PnL ≈ -86.91 < -50.00), but whichever triggers first
    gives the same verdict: KILL.
    """
    rows = [_fake_row("YES", market_ts=2_000_000 + i * 300) for i in range(8)]
    rows += [_fake_row("NO", market_ts=2_020_000 + i * 300) for i in range(32)]
    df = pd.DataFrame(rows)

    status = paper_gate.evaluate(df)
    assert status.status == "KILL"
    assert status.n == 40
    assert status.winrate == pytest.approx(8 / 40, abs=1e-9)


def test_synthetic_inconclusive_flow(tmp_path):
    """n=130, winrate=56.9% (just below PASS_WINRATE 0.58) → INCONCLUSIVE.

    Under hardened thresholds:
      n = 130 >= MIN_N_FOR_DECISION (120) ✓ (eligible for decision)
      winrate = 74/130 = 0.569 < PASS_WINRATE (0.58) ✗ (PASS blocked)
      winrate > KILL_WINRATE (0.45) (no KILL)
      total PnL = 74*5.8272 + 56*(-4.1728) = 431.21 - 233.68 = 197.53 > -50 (no dollar KILL)
    """
    rows = [_fake_row("YES", market_ts=3_000_000 + i * 300) for i in range(74)]
    rows += [_fake_row("NO", market_ts=3_100_000 + i * 300) for i in range(56)]
    df = pd.DataFrame(rows)

    status = paper_gate.evaluate(df)
    assert status.status == "INCONCLUSIVE"
    assert status.n == 130
    assert status.winrate == pytest.approx(74 / 130, abs=1e-9)
