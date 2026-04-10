"""Tests for the diagnostic backtest harness."""
import pandas as pd
import pytest

import backtest


BASELINE_CSV = "data/mm_paper_trades_baseline_2026-04-10.csv"


class TestLoadFills:
    def test_loads_csv_with_expected_columns(self):
        df = backtest.load_fills(BASELINE_CSV)
        expected = {"timestamp", "market_ts", "market_slug", "side", "price",
                    "size", "inventory_after", "cvd_skew", "signal_type"}
        assert set(df.columns) == expected

    def test_nonempty(self):
        df = backtest.load_fills(BASELINE_CSV)
        assert len(df) > 800  # baseline has 822


class TestIterCycles:
    def test_groups_by_market_ts(self):
        df = backtest.load_fills(BASELINE_CSV)
        cycles = list(backtest.iter_cycles(df))
        # 81 distinct cycles in the 822-row baseline session
        assert len(cycles) == 81

    def test_cycle_rows_preserve_order(self):
        df = backtest.load_fills(BASELINE_CSV)
        cycles = list(backtest.iter_cycles(df))
        for market_ts, rows in cycles:
            assert (rows["market_ts"] == market_ts).all()
            ts = pd.to_datetime(rows["timestamp"])
            assert ts.is_monotonic_increasing


class TestCycleCash:
    def test_reproduces_dashboard_cash(self):
        """Sum of cycle_cash across all cycles must match dashboard['cash'] to $0.01.

        NOTE: We target dash['cash'], not dash['settled_pnl'].

        paper_dashboard.compute_portfolio() uses a single running cycle_cash
        accumulator that only resets on SETTLE rows (see paper_dashboard.py:88-103).
        When a cycle has BUY/SELL fills but no SETTLE row ("flat cycle" — bot
        flattened the position mid-cycle via market making), that cycle's cash
        flow leaks forward into the next settled cycle's settled_pnl contribution.
        Fills after the last SETTLE are dropped entirely.

        Consequently dash['settled_pnl'] is not a clean per-cycle sum and cannot
        be reproduced by a cycle-independent model. dash['cash'] is the true
        realized cash flow (ignoring the mark-to-market of any open position)
        and equals the sum of per-cycle cash computed independently.

        The diagnostic goal is to analyze per-cycle behavior, so we want the
        clean cycle-independent model (not the leaky dashboard accumulator).
        """
        df = backtest.load_fills(BASELINE_CSV)
        total = sum(backtest.cycle_cash(rows) for _, rows in backtest.iter_cycles(df))

        import sys
        sys.path.insert(0, ".")
        from paper_dashboard import compute_portfolio
        dash = compute_portfolio(df)
        assert abs(total - dash["cash"]) < 0.01, \
            f"backtest={total:.4f}, dashboard_cash={dash['cash']:.4f}"


class TestCycleOutcome:
    def test_up_cycle(self):
        rows = pd.DataFrame([
            {"side": "BUY", "price": 0.56, "size": 10, "inventory_after": 10,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"side": "SETTLE", "price": 1.00, "size": 10, "inventory_after": 0,
             "cvd_skew": 0.0, "signal_type": "UP", "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        assert backtest.cycle_outcome(rows) == "UP"

    def test_down_cycle(self):
        rows = pd.DataFrame([
            {"side": "SELL", "price": 0.55, "size": 10, "inventory_after": -10,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"side": "SETTLE", "price": 0.00, "size": 10, "inventory_after": 0,
             "cvd_skew": 0.0, "signal_type": "DOWN", "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        assert backtest.cycle_outcome(rows) == "DOWN"

    def test_flat_cycle_no_settle(self):
        rows = pd.DataFrame([
            {"side": "BUY", "price": 0.50, "size": 10, "inventory_after": 10,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"side": "SELL", "price": 0.51, "size": 10, "inventory_after": 0,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        assert backtest.cycle_outcome(rows) == "FLAT"


class TestPreSettleInventory:
    def test_long_pre_settle(self):
        rows = pd.DataFrame([
            {"side": "BUY", "price": 0.56, "size": 10, "inventory_after": 10,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"side": "SETTLE", "price": 1.00, "size": 10, "inventory_after": 0,
             "cvd_skew": 0.0, "signal_type": "UP", "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        assert backtest.pre_settle_inventory(rows) == 10

    def test_short_pre_settle(self):
        rows = pd.DataFrame([
            {"side": "SELL", "price": 0.55, "size": 10, "inventory_after": -10,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"side": "SETTLE", "price": 0.00, "size": 10, "inventory_after": 0,
             "cvd_skew": 0.0, "signal_type": "DOWN", "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        assert backtest.pre_settle_inventory(rows) == -10

    def test_flat_returns_zero(self):
        rows = pd.DataFrame([
            {"side": "BUY", "price": 0.50, "size": 10, "inventory_after": 10,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"side": "SELL", "price": 0.51, "size": 10, "inventory_after": 0,
             "cvd_skew": 0.0, "signal_type": "NEUTRAL", "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        assert backtest.pre_settle_inventory(rows) == 0


class TestDominantSignal:
    def test_most_frequent_signal(self):
        rows = pd.DataFrame([
            {"signal_type": "STRONG_BULL", "side": "BUY", "price": 0.5, "size": 10,
             "inventory_after": 10, "cvd_skew": 0.05, "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"signal_type": "STRONG_BULL", "side": "SELL", "price": 0.5, "size": 10,
             "inventory_after": 0, "cvd_skew": 0.05, "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
            {"signal_type": "NEUTRAL", "side": "BUY", "price": 0.5, "size": 10,
             "inventory_after": 10, "cvd_skew": 0.0, "market_ts": 1, "timestamp": "t3", "market_slug": "s"},
        ])
        assert backtest.dominant_signal(rows) == "STRONG_BULL"

    def test_excludes_settle_row(self):
        rows = pd.DataFrame([
            {"signal_type": "BULLISH", "side": "BUY", "price": 0.5, "size": 10,
             "inventory_after": 10, "cvd_skew": 0.01, "market_ts": 1, "timestamp": "t1", "market_slug": "s"},
            {"signal_type": "UP", "side": "SETTLE", "price": 1.0, "size": 10,
             "inventory_after": 0, "cvd_skew": 0.0, "market_ts": 1, "timestamp": "t2", "market_slug": "s"},
        ])
        assert backtest.dominant_signal(rows) == "BULLISH"


class TestExperimentBaseline:
    def test_reproduces_dashboard_cash(self):
        """experiment_baseline must reproduce dash['cash'] (the clean total)."""
        df = backtest.load_fills(BASELINE_CSV)
        result = backtest.experiment_baseline(df)

        import sys
        sys.path.insert(0, ".")
        from paper_dashboard import compute_portfolio
        dash = compute_portfolio(df)

        # See the extensive comment in TestCycleCash on why we target `cash`
        # and not `settled_pnl` (paper_dashboard.py:88-103 leak-forward bug).
        assert abs(result.total_pnl - dash["cash"]) < 0.01, \
            f"baseline={result.total_pnl:.4f}, dash_cash={dash['cash']:.4f}"

    def test_alignment_adverse_in_baseline(self):
        """The baseline session had clearly adverse alignment (inv vs outcome).

        Baseline stats (verified from dashboard): 822 rows, 81 cycles,
        64 settled, 16 won / 48 lost (dashboard's leaky counters) which
        corresponds to a clean inventory-sign alignment ≤ 35%.
        """
        df = backtest.load_fills(BASELINE_CSV)
        result = backtest.experiment_baseline(df)
        settled = result.alignment_good + result.alignment_bad
        assert 55 <= settled <= 70, f"settled count out of expected range: {settled}"
        assert result.alignment_rate < 0.35, \
            f"alignment higher than expected: {result.alignment_rate:.4f}"


class TestExperimentsSmoke:
    def test_all_experiments_return_finite(self):
        df = backtest.load_fills(BASELINE_CSV)
        for fn in (backtest.experiment_neutral_only,
                   backtest.experiment_forced_flatten,
                   backtest.experiment_half_cap):
            result = fn(df)
            assert result.cycles_kept > 0, f"{result.name}: no cycles kept"
            # Finite check (not NaN, not inf)
            assert result.total_pnl == result.total_pnl, f"{result.name}: NaN total"
            assert -10000 < result.total_pnl < 10000, \
                f"{result.name}: unreasonable total {result.total_pnl}"

    def test_experiment_result_to_dict_roundtrip(self):
        """ExperimentResult.to_dict must serialize all fields."""
        df = backtest.load_fills(BASELINE_CSV)
        result = backtest.experiment_baseline(df)
        d = result.to_dict()
        expected = {"name", "total_pnl", "cycles_kept", "cycles_dropped",
                    "alignment_good", "alignment_bad", "alignment_rate", "per_cycle_mean"}
        assert set(d.keys()) == expected
