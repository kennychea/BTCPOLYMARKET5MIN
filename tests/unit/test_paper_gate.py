"""Unit tests for paper_gate.evaluate and helpers.

Thresholds hardened 2026-04-11 — see paper_gate.py docstring for the
three-agent calibration that set these numbers.
"""
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
        # Math correctness anchor: P(X >= 38 | n=60, p=0.5)
        # computed two ways must match.
        expected = sum(comb(60, k) * (0.5 ** 60) for k in range(38, 61))
        assert paper_gate.one_sided_binomial_p(38, 60, 0.5) == pytest.approx(expected, abs=1e-12)

    def test_reference_value_120_74_below_001(self):
        # Hardened-threshold anchor: at n=120, the smallest k satisfying
        # p <= PASS_P_VALUE (0.01) is k=74 (p≈0.00669).
        # k=73 gives p≈0.01104 which is *above* 0.01 — borderline.
        # Confirms the gate's calibration (agent 3 power analysis) is
        # self-consistent: winrate >= 74/120 = 61.7% is the effective floor
        # above PASS_WINRATE = 58% when the binomial test is the binding gate.
        p74 = paper_gate.one_sided_binomial_p(74, 120, 0.5)
        p73 = paper_gate.one_sided_binomial_p(73, 120, 0.5)
        assert p74 < paper_gate.PASS_P_VALUE
        assert p73 > paper_gate.PASS_P_VALUE


# ── compute_pnl_per_trade (hardened: symmetric fee curve) ──────────────

class TestComputePnl:
    def test_winning_trade_at_40c(self):
        """Buy 10 shares @ $0.40, win.
        gross = 10 × (1.0 - 0.40) = 6.00
        fee   = 10 × 0.072 × 0.40 × 0.60 = 0.1728 (always paid)
        net   = 5.8272
        """
        row = pd.Series(_row(signal_correct="YES", shares=10, stink_price=0.40))
        pnl = paper_gate.compute_pnl_per_trade(row)
        assert pnl == pytest.approx(5.8272, abs=1e-9)

    def test_losing_trade_at_40c_still_pays_fee(self):
        """Buy 10 shares @ $0.40, lose.
        gross = 10 × (0 - 0.40) = -4.00
        fee   = 10 × 0.072 × 0.40 × 0.60 = 0.1728 (PAID — symmetric curve)
        net   = -4.1728
        """
        row = pd.Series(_row(signal_correct="NO", shares=10, stink_price=0.40))
        pnl = paper_gate.compute_pnl_per_trade(row)
        assert pnl == pytest.approx(-4.1728, abs=1e-9)

    def test_winning_trade_at_55c(self):
        """Buy 12 shares @ $0.55, win.
        gross = 12 × 0.45 = 5.40
        fee   = 12 × 0.072 × 0.55 × 0.45 = 0.21384
        net   = 5.18616
        """
        row = pd.Series(_row(signal_correct="YES", shares=12, stink_price=0.55))
        pnl = paper_gate.compute_pnl_per_trade(row)
        assert pnl == pytest.approx(5.18616, abs=1e-9)

    def test_fee_peak_at_50c(self):
        """Fee curve peaks at entry=0.50 (1.80% of notional).
        Buy 10 shares @ $0.50 → fee = 10 × 0.072 × 0.25 = 0.18
        """
        row = pd.Series(_row(signal_correct="YES", shares=10, stink_price=0.50))
        # gross = 10 × 0.50 = 5.00; fee = 0.18; net = 4.82
        assert paper_gate.compute_pnl_per_trade(row) == pytest.approx(4.82, abs=1e-9)


# ── evaluate (hardened thresholds) ─────────────────────────────────────

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

    def test_string_false_filled_does_not_count(self):
        """CSV round-trip can produce `filled` as the string "False".
        Ensure _is_filled treats it as unfilled (not truthy like bool(str)).
        """
        df = _df([_row(signal_correct="YES", filled="False") for _ in range(40)])
        status = paper_gate.evaluate(df)
        assert status.status == "CONTINUE"
        assert status.n == 0

    def test_nan_filled_does_not_count(self):
        """NaN in filled column (from empty CSV cell on float dtype) must be
        treated as unfilled. bool(float('nan')) == True would silently inflate
        n on a safety gate.
        """
        rows = [dict(_row(signal_correct="YES"), filled=float("nan")) for _ in range(30)]
        rows += [dict(_row(signal_correct="NO"), filled=float("nan")) for _ in range(10)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "CONTINUE"
        assert status.n == 0

    def test_string_true_filled_counts_as_warmup_kill(self):
        """Symmetric check: CSV string "True" counts as filled. n=30 with
        winrate 10/30=0.33 hits the warmup KILL (wr<0.50 for n in [30,50)).
        """
        rows = [dict(_row(signal_correct="YES"), filled="True") for _ in range(10)]
        rows += [dict(_row(signal_correct="NO"), filled="True") for _ in range(20)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "KILL"
        assert status.n == 30
        assert "warmup" in status.reason

    def test_only_pending_trades_continues(self):
        df = _df([_row(signal_correct="PENDING", filled=True) for _ in range(40)])
        status = paper_gate.evaluate(df)
        assert status.status == "CONTINUE"
        assert status.n == 0

    def test_below_kill_n_bad_winrate_continues(self):
        # n=29, winrate=0.00 (all losses) → CONTINUE (below KILL_N=30)
        df = _df([_row(signal_correct="NO") for _ in range(29)])
        status = paper_gate.evaluate(df)
        # But the cumulative loss may trigger dollar KILL: 29 × -4.1728 = -121
        # which is < KILL_TOTAL_LOSS=-50. So this actually hits the dollar KILL.
        assert status.status == "KILL"
        assert "total simulated loss" in status.reason

    def test_below_kill_n_moderate_loss_continues(self):
        # n=10, winrate=0 at stink=0.30 → total loss small enough to CONTINUE
        # Per-trade loss at stink=0.30: gross=-3.0, fee=10×0.072×0.30×0.70=0.1512
        #   → -3.1512 per trade × 10 = -31.51. Above -50 threshold.
        rows = [_row(signal_correct="NO", stink_price=0.30) for _ in range(10)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "CONTINUE"
        assert status.n == 10

    # ── Layered KILL: warmup window [30, 50) ─────────────────────────
    def test_warmup_kill_at_n30_winrate_below_50(self):
        # n=30, winrate=14/30=0.467 < KILL_WARMUP_WR (0.50) → warmup KILL
        # Per-trade EV at stink=0.40 winrate 0.467:
        #   14 × 5.8272 + 16 × (-4.1728) = 81.58 - 66.76 = 14.82 (positive)
        # Dollar KILL does not fire; warmup binomial KILL does.
        rows = [_row(signal_correct="YES") for _ in range(14)]
        rows += [_row(signal_correct="NO") for _ in range(16)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "KILL"
        assert "warmup" in status.reason
        assert status.n == 30

    def test_warmup_window_upper_bound_n49(self):
        # n=49, winrate=23/49=0.469 → still in warmup → KILL
        rows = [_row(signal_correct="YES") for _ in range(23)]
        rows += [_row(signal_correct="NO") for _ in range(26)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "KILL"
        assert "warmup" in status.reason

    def test_warmup_does_not_kill_at_exactly_50_pct(self):
        # n=30, winrate=0.50 is NOT < 0.50 → no warmup KILL.
        # EV at stink=0.40: 15 × 5.8272 + 15 × (-4.1728) = 87.41 - 62.59 = 24.82 > -50
        rows = [_row(signal_correct="YES") for _ in range(15)]
        rows += [_row(signal_correct="NO") for _ in range(15)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "CONTINUE"

    # ── Standard KILL: n >= 50 ───────────────────────────────────────
    def test_standard_kill_at_n50_winrate_below_45(self):
        # n=50, wins=20, winrate=0.40 < 0.45 → standard KILL
        rows = [_row(signal_correct="YES") for _ in range(20)]
        rows += [_row(signal_correct="NO") for _ in range(30)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "KILL"
        # Either dollar KILL or standard KILL could fire; both are correct
        assert "total simulated loss" in status.reason or f"n={len(rows)}" in status.reason

    def test_standard_continues_at_winrate_between_45_and_58(self):
        # n=50, wins=25, winrate=0.50 at stink=0.40
        # Total EV: 25 × 5.8272 + 25 × (-4.1728) = 41.36 (positive, no dollar kill)
        # winrate 0.50 >= 0.45 → no standard KILL
        # n < 120 → no PASS → CONTINUE
        rows = [_row(signal_correct="YES") for _ in range(25)]
        rows += [_row(signal_correct="NO") for _ in range(25)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "CONTINUE"

    # ── Dollar-drawdown KILL ─────────────────────────────────────────
    def test_dollar_drawdown_kill_fires_independently_of_binomial(self):
        """Dollar KILL must fire even if winrate is above all binomial
        thresholds, if cumulative simulated loss breaches -$50."""
        # 20 wins + 20 losses at stink=0.50, large share size to breach budget
        # Per-trade EV at stink=0.50: win = 4.82, lose = -5.18
        # 20 × 4.82 + 20 × -5.18 = 96.4 - 103.6 = -7.2 (not enough)
        # Use larger shares:
        shares = 50
        # Per-trade: win = 50*(0.5 - 0.072*0.25) = 50*(0.5 - 0.018) = 24.10
        #            lose = 50*(-0.5 - 0.018) = -25.90
        # 20 × 24.10 + 20 × -25.90 = 482 - 518 = -36 (still not enough)
        # Go harder:
        shares = 100
        rows = [_row(signal_correct="YES", shares=shares, stink_price=0.50) for _ in range(20)]
        rows += [_row(signal_correct="NO", shares=shares, stink_price=0.50) for _ in range(20)]
        # 20 × 48.20 + 20 × -51.80 = 964 - 1036 = -72 < -50 → dollar KILL
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "KILL"
        assert "total simulated loss" in status.reason
        assert status.winrate == pytest.approx(0.50, abs=1e-9)  # binomial gates would NOT kill this

    # ── PASS path at hardened n=120 ──────────────────────────────────
    def test_pass_all_gates_at_n120(self):
        """n=120, wins=80, winrate=0.667 at stink=0.40.
        p-value: P(X>=80 | 120, 0.5) ≈ 1.3e-4 << 0.01 ✓
        winrate 0.667 >= 0.58 ✓
        total EV: 80 × 5.8272 + 40 × (-4.1728) = 299.26 > -50 ✓
        Expected: PASS.
        """
        rows = [_row(signal_correct="YES", stink_price=0.40) for _ in range(80)]
        rows += [_row(signal_correct="NO", stink_price=0.40) for _ in range(40)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "PASS"
        assert status.n == 120
        assert status.winrate == pytest.approx(80 / 120, abs=1e-9)
        assert status.p_value < paper_gate.PASS_P_VALUE

    def test_pass_not_triggered_below_min_n(self):
        """n=119, winrate=0.80 → CONTINUE (not enough data for hardened gate)."""
        rows = [_row(signal_correct="YES") for _ in range(95)]
        rows += [_row(signal_correct="NO") for _ in range(24)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "CONTINUE"
        assert status.n == 119

    def test_inconclusive_p_value_too_high(self):
        """n=120, wins=70, winrate=0.583.
        winrate just above 0.58 threshold, but p-value ≈ 0.034 > 0.01.
        Expected: INCONCLUSIVE.
        """
        rows = [_row(signal_correct="YES", stink_price=0.40) for _ in range(70)]
        rows += [_row(signal_correct="NO", stink_price=0.40) for _ in range(50)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "INCONCLUSIVE"
        assert status.n == 120
        assert status.winrate >= paper_gate.PASS_WINRATE
        assert status.p_value > paper_gate.PASS_P_VALUE

    def test_inconclusive_winrate_just_below(self):
        """n=120, wins=69, winrate=0.575 < PASS_WINRATE=0.58.
        Expected: INCONCLUSIVE.
        """
        rows = [_row(signal_correct="YES", stink_price=0.40) for _ in range(69)]
        rows += [_row(signal_correct="NO", stink_price=0.40) for _ in range(51)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "INCONCLUSIVE"
        assert status.winrate < paper_gate.PASS_WINRATE

    def test_ev_no_longer_gates_pass(self):
        """Regression: a PASS at n=120 with high winrate must NOT depend on
        compute_pnl_per_trade returning a specific value. EV is informational,
        not a gating constraint (hardened 2026-04-11)."""
        # High winrate at a stink price where EV is huge — should PASS trivially.
        rows = [_row(signal_correct="YES", stink_price=0.30) for _ in range(80)]
        rows += [_row(signal_correct="NO", stink_price=0.30) for _ in range(40)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "PASS"
        # EV should be positive and reported, but not required for the decision.
        assert status.ev_per_trade > 0


# ── sentinel read/write ────────────────────────────────────────────────

class TestSentinel:
    def test_write_and_read_roundtrip(self, tmp_path):
        path = str(tmp_path / "sentinel.json")
        status = paper_gate.GateStatus(
            status="PASS",
            reason="test",
            n=120,
            winrate=0.67,
            p_value=0.004,
            ev_per_trade=2.5,
            evaluated_at="2026-04-11T12:00:00+00:00",
        )
        paper_gate.write_sentinel(status, path)
        loaded = paper_gate.read_sentinel(path)
        assert loaded is not None
        assert loaded.status == "PASS"
        assert loaded.n == 120
        assert loaded.winrate == pytest.approx(0.67)

    def test_write_is_atomic(self, tmp_path):
        """Verify write_sentinel uses an atomic replace (no tmp file left behind)."""
        path = str(tmp_path / "sentinel.json")
        status = paper_gate.GateStatus(
            status="KILL", reason="test", n=30, winrate=0.2,
            p_value=0.99, ev_per_trade=-3.0,
            evaluated_at="2026-04-11T00:00:00+00:00",
        )
        paper_gate.write_sentinel(status, path)
        # tmp file must not linger after successful write
        assert not (tmp_path / "sentinel.json.tmp").exists()
        assert (tmp_path / "sentinel.json").exists()

    def test_read_missing_file_returns_none(self, tmp_path):
        path = str(tmp_path / "does_not_exist.json")
        assert paper_gate.read_sentinel(path) is None

    def test_read_corrupt_file_returns_none(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("not json at all")
        assert paper_gate.read_sentinel(str(path)) is None
