"""Unit tests for paper trading functions — log, resolve, summary."""
import os

import pandas as pd
import pytest

import cvd_5min_bot as bot


class FakeFeed:
    """Minimal mock of BinanceCVDFeed for testing resolve_paper_outcome."""

    def __init__(self, price: float):
        self._price = price

    def get_last_price(self) -> float:
        return self._price


@pytest.fixture(autouse=True)
def paper_tmp_dir(tmp_path, monkeypatch):
    """Redirect PAPER_LOG_FILE and DATA_DIR to a temp directory for each test."""
    monkeypatch.setattr(bot, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "PAPER_LOG_FILE", str(tmp_path / "paper_trades.csv"))
    return tmp_path


# ── log_paper_signal ─────────────────────────────────────────────────────

class TestLogPaperSignal:
    def test_creates_csv_with_correct_columns(self):
        bot.log_paper_signal(
            market_ts=1000000,
            signal_type="BULLISH_DIV",
            direction="UP",
            detail="test signal",
            stink_price=0.45,
            btc_price_at_signal=84000.0,
            market_slug="btc-updown-5m-1000000",
        )
        assert os.path.exists(bot.PAPER_LOG_FILE)
        df = pd.read_csv(bot.PAPER_LOG_FILE)
        assert len(df) == 1
        expected_cols = {
            "timestamp", "market_ts", "market_slug", "signal_type", "direction",
            "detail", "stink_price", "btc_price_at_signal", "btc_price_at_close",
            "market_outcome", "signal_correct", "price_change_pct",
        }
        assert set(df.columns) == expected_cols

    def test_values_stored_correctly(self):
        bot.log_paper_signal(
            market_ts=1000000,
            signal_type="STRONG_BEAR",
            direction="DOWN",
            detail="strong selling",
            stink_price=0.52,
            btc_price_at_signal=85000.5,
            market_slug="btc-updown-5m-1000000",
        )
        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["market_ts"] == 1000000
        assert row["signal_type"] == "STRONG_BEAR"
        assert row["direction"] == "DOWN"
        assert row["stink_price"] == 0.52
        assert row["btc_price_at_signal"] == 85000.5
        assert row["btc_price_at_close"] == 0.0
        # Empty strings are read as NaN by pandas — check for that
        assert pd.isna(row["market_outcome"]) or row["market_outcome"] == ""
        assert pd.isna(row["signal_correct"]) or row["signal_correct"] == ""

    def test_appends_to_existing(self):
        bot.log_paper_signal(1000, "BULLISH_DIV", "UP", "first", 0.4, 84000.0, "slug-1")
        bot.log_paper_signal(2000, "BEARISH_DIV", "DOWN", "second", 0.5, 84100.0, "slug-2")
        df = pd.read_csv(bot.PAPER_LOG_FILE)
        assert len(df) == 2
        assert df.iloc[0]["signal_type"] == "BULLISH_DIV"
        assert df.iloc[1]["signal_type"] == "BEARISH_DIV"


# ── resolve_paper_outcome ────────────────────────────────────────────────

class TestResolvePaperOutcome:
    def _seed_signal(self, market_ts: int, direction: str, btc_price: float):
        """Helper: log a paper signal that hasn't been resolved yet."""
        bot.log_paper_signal(market_ts, "BULLISH_DIV", direction, "test", 0.45, btc_price, "slug")

    def test_correct_up_prediction(self):
        """Signal predicted UP, BTC went up → signal_correct = YES."""
        self._seed_signal(1000, "UP", 84000.0)
        feed = FakeFeed(84100.0)
        bot.resolve_paper_outcome(1000, feed)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        assert df.iloc[0]["signal_correct"] == "YES"
        assert df.iloc[0]["market_outcome"] == "UP"
        assert df.iloc[0]["btc_price_at_close"] == 84100.0

    def test_correct_down_prediction(self):
        """Signal predicted DOWN, BTC went down → signal_correct = YES."""
        self._seed_signal(2000, "DOWN", 84000.0)
        feed = FakeFeed(83900.0)
        bot.resolve_paper_outcome(2000, feed)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        assert df.iloc[0]["signal_correct"] == "YES"
        assert df.iloc[0]["market_outcome"] == "DOWN"

    def test_wrong_prediction(self):
        """Signal predicted UP but BTC went down → signal_correct = NO."""
        self._seed_signal(3000, "UP", 84000.0)
        feed = FakeFeed(83800.0)
        bot.resolve_paper_outcome(3000, feed)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        assert df.iloc[0]["signal_correct"] == "NO"
        assert df.iloc[0]["market_outcome"] == "DOWN"

    def test_price_change_pct_calculated(self):
        """Price change % is correctly computed."""
        self._seed_signal(4000, "UP", 80000.0)
        feed = FakeFeed(80100.0)
        bot.resolve_paper_outcome(4000, feed)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        expected_pct = round(((80100.0 - 80000.0) / 80000.0) * 100, 4)
        assert df.iloc[0]["price_change_pct"] == pytest.approx(expected_pct)

    def test_no_file_is_noop(self):
        """No crash if CSV doesn't exist."""
        feed = FakeFeed(84000.0)
        bot.resolve_paper_outcome(999, feed)

    def test_skips_already_resolved(self):
        """Doesn't overwrite signals that were already resolved."""
        self._seed_signal(5000, "UP", 84000.0)
        feed1 = FakeFeed(84100.0)
        bot.resolve_paper_outcome(5000, feed1)

        feed2 = FakeFeed(83000.0)
        bot.resolve_paper_outcome(5000, feed2)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        assert df.iloc[0]["btc_price_at_close"] == 84100.0

    def test_zero_feed_price_skips(self):
        """If feed returns 0 price, don't resolve."""
        self._seed_signal(6000, "UP", 84000.0)
        feed = FakeFeed(0.0)
        bot.resolve_paper_outcome(6000, feed)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        # signal_correct should still be empty (NaN or "")
        val = df.iloc[0]["signal_correct"]
        assert pd.isna(val) or val == ""


# ── print_paper_summary ──────────────────────────────────────────────────

class TestPrintPaperSummary:
    def test_no_file_no_crash(self, capsys):
        bot.print_paper_summary()
        out = capsys.readouterr().out
        assert "No paper trades" in out

    def test_with_resolved_data(self, capsys):
        bot.log_paper_signal(1000, "BULLISH_DIV", "UP", "t1", 0.4, 84000.0, "s1")
        bot.log_paper_signal(2000, "BEARISH_DIV", "DOWN", "t2", 0.5, 84100.0, "s2")
        bot.resolve_paper_outcome(1000, FakeFeed(84100.0))  # correct
        bot.resolve_paper_outcome(2000, FakeFeed(84200.0))  # wrong (predicted DOWN, went UP)

        bot.print_paper_summary()
        out = capsys.readouterr().out
        assert "Accuracy" in out
        assert "50.0%" in out
