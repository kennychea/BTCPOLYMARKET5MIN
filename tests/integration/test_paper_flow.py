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
