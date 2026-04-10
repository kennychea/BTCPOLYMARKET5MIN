"""Unit tests for paper_gate.evaluate and helpers."""
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
        # Known reference: P(X >= 38 | n=60, p=0.5) computed via scipy:
        #   scipy.stats.binom.sf(37, 60, 0.5) ≈ 0.04386
        # We use sum of P(X=k) from k=38..60.
        expected = sum(comb(60, k) * (0.5 ** 60) for k in range(38, 61))
        assert paper_gate.one_sided_binomial_p(38, 60, 0.5) == pytest.approx(expected, abs=1e-12)
        assert expected < 0.05  # sanity: this is the PASS_WINRATE threshold at n=60


# ── compute_pnl_per_trade ────────────────────────────────────────────────

class TestComputePnl:
    def test_winning_trade_at_40c(self):
        """Buy 10 shares @ $0.40, win → payout = 10 × $1 = $10. Entry cost = $4.
        Gross = 10 × (1.0 - 0.40) = 6.00. Fees = 10 × 1.0 × 0.0315 = 0.315.
        Net = 5.685.
        """
        row = pd.Series(_row(signal_correct="YES", shares=10, stink_price=0.40))
        pnl = paper_gate.compute_pnl_per_trade(row)
        assert pnl == pytest.approx(5.685, abs=1e-9)

    def test_losing_trade_at_40c(self):
        """Buy 10 shares @ $0.40, lose → payout = 0. Gross = 10 × (0 - 0.40) = -4.0.
        Fees = 10 × 0 × 0.0315 = 0 (no fees on losers under payout-side model).
        Net = -4.0.
        """
        row = pd.Series(_row(signal_correct="NO", shares=10, stink_price=0.40))
        pnl = paper_gate.compute_pnl_per_trade(row)
        assert pnl == pytest.approx(-4.0, abs=1e-9)

    def test_winning_trade_at_55c_cap(self):
        row = pd.Series(_row(signal_correct="YES", shares=12, stink_price=0.55))
        # Gross = 12 × 0.45 = 5.40; fees = 12 × 1 × 0.0315 = 0.378; net = 5.022
        pnl = paper_gate.compute_pnl_per_trade(row)
        assert pnl == pytest.approx(5.022, abs=1e-9)


# ── evaluate ────────────────────────────────────────────────────────────

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
        import numpy as np
        rows = [dict(_row(signal_correct="YES"), filled=float("nan")) for _ in range(30)]
        rows += [dict(_row(signal_correct="NO"), filled=float("nan")) for _ in range(10)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "CONTINUE"
        assert status.n == 0

    def test_string_true_filled_counts(self):
        """Symmetric check: CSV string "True" must count as filled."""
        rows = [dict(_row(signal_correct="YES"), filled="True") for _ in range(10)]
        rows += [dict(_row(signal_correct="NO"), filled="True") for _ in range(20)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "KILL"
        assert status.n == 30

    def test_only_pending_trades_continues(self):
        df = _df([_row(signal_correct="PENDING", filled=True) for _ in range(40)])
        status = paper_gate.evaluate(df)
        assert status.status == "CONTINUE"
        assert status.n == 0

    def test_below_kill_n_bad_winrate_continues(self):
        # n=29, winrate=0.00 (all losses) → CONTINUE (below KILL_N)
        df = _df([_row(signal_correct="NO") for _ in range(29)])
        status = paper_gate.evaluate(df)
        assert status.status == "CONTINUE"
        assert status.n == 29

    def test_kill_at_threshold(self):
        # n=30, wins=10, winrate=0.333 → KILL
        rows = [_row(signal_correct="YES") for _ in range(10)]
        rows += [_row(signal_correct="NO") for _ in range(20)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "KILL"
        assert status.n == 30
        assert status.winrate == pytest.approx(10 / 30, abs=1e-9)

    def test_above_kill_n_borderline_continues(self):
        # n=30, wins=15, winrate=0.50 → CONTINUE (≥0.45, no PASS at n<60)
        rows = [_row(signal_correct="YES") for _ in range(15)]
        rows += [_row(signal_correct="NO") for _ in range(15)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "CONTINUE"

    def test_below_min_n_high_winrate_continues(self):
        # n=50, wins=40, winrate=0.80 → CONTINUE (n<60)
        rows = [_row(signal_correct="YES") for _ in range(40)]
        rows += [_row(signal_correct="NO") for _ in range(10)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "CONTINUE"

    def test_pass_all_gates(self):
        # n=60, wins=40, winrate=0.667, p≈0.004, EV strongly positive
        rows = [_row(signal_correct="YES", stink_price=0.40) for _ in range(40)]
        rows += [_row(signal_correct="NO", stink_price=0.40) for _ in range(20)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "PASS"
        assert status.n == 60
        assert status.winrate == pytest.approx(40 / 60, abs=1e-9)
        assert status.p_value < 0.05
        assert status.ev_per_trade >= 0.20

    def test_inconclusive_p_value_too_high(self):
        # n=60, wins=35, winrate=0.583, p≈0.123 > 0.05 → INCONCLUSIVE
        # (EV ≈ $0.65 is above threshold; p-value is what fails)
        rows = [_row(signal_correct="YES", stink_price=0.50) for _ in range(35)]
        rows += [_row(signal_correct="NO", stink_price=0.50) for _ in range(25)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "INCONCLUSIVE"
        assert status.n == 60

    def test_inconclusive_winrate_just_below(self):
        # n=60, wins=34, winrate=0.567 → INCONCLUSIVE (< PASS_WINRATE 0.58)
        rows = [_row(signal_correct="YES") for _ in range(34)]
        rows += [_row(signal_correct="NO") for _ in range(26)]
        status = paper_gate.evaluate(_df(rows))
        assert status.status == "INCONCLUSIVE"


# ── sentinel read/write ────────────────────────────────────────────────

class TestSentinel:
    def test_write_and_read_roundtrip(self, tmp_path):
        path = str(tmp_path / "sentinel.json")
        status = paper_gate.GateStatus(
            status="PASS",
            reason="test",
            n=60,
            winrate=0.67,
            p_value=0.004,
            ev_per_trade=0.25,
            evaluated_at="2026-04-10T12:00:00+00:00",
        )
        paper_gate.write_sentinel(status, path)
        loaded = paper_gate.read_sentinel(path)
        assert loaded is not None
        assert loaded.status == "PASS"
        assert loaded.n == 60
        assert loaded.winrate == pytest.approx(0.67)

    def test_read_missing_file_returns_none(self, tmp_path):
        path = str(tmp_path / "does_not_exist.json")
        assert paper_gate.read_sentinel(path) is None

    def test_read_corrupt_file_returns_none(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("not json at all")
        assert paper_gate.read_sentinel(str(path)) is None
