"""Unit tests for MM paper fills and logging."""
import os

import pandas as pd
import pytest

import cvd_5min_bot as bot


# ── check_mm_paper_fills ────────────────────────────────────────────────

class TestCheckMmPaperFills:
    def test_bid_filled_when_ask_drops(self):
        """Market ask ≤ our bid → we buy."""
        book = {"best_bid": 0.45, "best_ask": 0.47, "spread": 0.02}
        fills = bot.check_mm_paper_fills(book, our_bid=0.48, our_ask=0.52,
                                         bid_size=10, ask_size=10)
        assert len(fills) == 1
        assert fills[0]["side"] == "BUY"
        assert fills[0]["price"] == 0.48
        assert fills[0]["size"] == 10

    def test_ask_filled_when_bid_rises(self):
        """Market bid ≥ our ask → we sell."""
        book = {"best_bid": 0.53, "best_ask": 0.55, "spread": 0.02}
        fills = bot.check_mm_paper_fills(book, our_bid=0.48, our_ask=0.52,
                                         bid_size=10, ask_size=10)
        assert len(fills) == 1
        assert fills[0]["side"] == "SELL"
        assert fills[0]["price"] == 0.52
        assert fills[0]["size"] == 10

    def test_no_fill_within_spread(self):
        """Market inside our spread → no fills."""
        book = {"best_bid": 0.49, "best_ask": 0.51, "spread": 0.02}
        fills = bot.check_mm_paper_fills(book, our_bid=0.48, our_ask=0.52,
                                         bid_size=10, ask_size=10)
        assert len(fills) == 0

    def test_crossed_book_picks_one_side(self):
        """Crossed book (API anomaly) — mutual exclusion, pick one side only."""
        book = {"best_bid": 0.53, "best_ask": 0.47, "spread": -0.06}
        fills = bot.check_mm_paper_fills(book, our_bid=0.48, our_ask=0.52,
                                         bid_size=10, ask_size=10)
        assert len(fills) == 1  # only one side, not both

    def test_zero_size_no_fill(self):
        """Size 0 → no fill even if price crosses."""
        book = {"best_bid": 0.53, "best_ask": 0.47, "spread": -0.06}
        fills = bot.check_mm_paper_fills(book, our_bid=0.48, our_ask=0.52,
                                         bid_size=0, ask_size=0)
        assert len(fills) == 0

    def test_zero_quote_no_fill(self):
        """Quote price 0 → no fill."""
        book = {"best_bid": 0.53, "best_ask": 0.47, "spread": -0.06}
        fills = bot.check_mm_paper_fills(book, our_bid=0.0, our_ask=0.0,
                                         bid_size=10, ask_size=10)
        assert len(fills) == 0


# ── MM Logging ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mm_tmp_dir(tmp_path, monkeypatch):
    """Redirect MM_LOG_FILE to temp directory."""
    monkeypatch.setattr(bot, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "MM_LOG_FILE", str(tmp_path / "mm_paper_trades.csv"))
    return tmp_path


class TestLogMmFill:
    def test_creates_csv(self):
        bot.log_mm_fill(1000, "slug-1", "BUY", 0.48, 10, 10, 0.005, "BULLISH_DIV")
        assert os.path.exists(bot.MM_LOG_FILE)
        df = pd.read_csv(bot.MM_LOG_FILE)
        assert len(df) == 1

    def test_appends(self):
        bot.log_mm_fill(1000, "slug-1", "BUY", 0.48, 10, 10, 0.005, "BULLISH_DIV")
        bot.log_mm_fill(1000, "slug-1", "SELL", 0.52, 10, 0, 0.005, "BULLISH_DIV")
        df = pd.read_csv(bot.MM_LOG_FILE)
        assert len(df) == 2

    def test_correct_columns(self):
        bot.log_mm_fill(1000, "slug-1", "BUY", 0.48, 10, 10, 0.005, "NEUTRAL")
        df = pd.read_csv(bot.MM_LOG_FILE)
        expected = {"timestamp", "market_ts", "market_slug", "side", "price",
                    "size", "inventory_after", "cvd_skew", "signal_type"}
        assert set(df.columns) == expected

    def test_values_stored(self):
        bot.log_mm_fill(2000, "slug-2", "SELL", 0.52, 5, -5, -0.01, "BEARISH_DIV")
        df = pd.read_csv(bot.MM_LOG_FILE)
        row = df.iloc[0]
        assert row["market_ts"] == 2000
        assert row["side"] == "SELL"
        assert row["price"] == 0.52
        assert row["size"] == 5
        assert row["inventory_after"] == -5


class TestLogMmSettle:
    def test_settle_row_logged(self):
        """SETTLE rows are written to CSV with correct fields."""
        bot.log_mm_fill(1000, "s1", "BUY", 0.48, 10, 10, 0.0, "NEUTRAL")
        bot.log_mm_fill(1000, "s1", "SETTLE", 1.0, 10, 0, 0.0, "UP")
        df = pd.read_csv(bot.MM_LOG_FILE)
        assert len(df) == 2
        settle = df.iloc[1]
        assert settle["side"] == "SETTLE"
        assert settle["price"] == 1.0
        assert settle["size"] == 10
        assert settle["inventory_after"] == 0
        assert settle["signal_type"] == "UP"

    def test_settle_down(self):
        """DOWN settlement logs price=0.0."""
        bot.log_mm_fill(1000, "s1", "SELL", 0.52, 10, -10, 0.0, "NEUTRAL")
        bot.log_mm_fill(1000, "s1", "SETTLE", 0.0, 10, 0, 0.0, "DOWN")
        df = pd.read_csv(bot.MM_LOG_FILE)
        settle = df.iloc[1]
        assert settle["price"] == 0.0
        assert settle["signal_type"] == "DOWN"


class TestPrintMmSummary:
    def test_no_file(self, capsys):
        bot.print_mm_summary()
        out = capsys.readouterr().out
        assert "No MM trades" in out

    def test_with_data(self, capsys):
        bot.log_mm_fill(1000, "s1", "BUY", 0.48, 10, 10, 0.0, "NEUTRAL")
        bot.log_mm_fill(1000, "s1", "SELL", 0.52, 10, 0, 0.0, "NEUTRAL")
        bot.print_mm_summary()
        out = capsys.readouterr().out
        assert "Market Maker Summary" in out


# ── MM_NEUTRAL_ONLY gate (Phase 4.1a, 2026-04-10 diagnostic) ────────────

class TestMmNeutralOnlyGate:
    """Gate: when MM_NEUTRAL_ONLY=True, skip quoting unless signal is NEUTRAL."""

    def _make_mm(self, monkeypatch) -> "bot.CVDMarketMaker":
        """Return a CVDMarketMaker with feed=None and paper mode on."""
        monkeypatch.setattr(bot, "PAPER_MODE", True)
        mm = bot.CVDMarketMaker(feed=None)
        mm.up_token_id = "test-token"
        return mm

    def test_gate_disabled_quotes_normally(self, monkeypatch):
        """MM_NEUTRAL_ONLY=False → quotes posted even on non-NEUTRAL signal."""
        monkeypatch.setattr(bot, "MM_NEUTRAL_ONLY", False)
        mm = self._make_mm(monkeypatch)
        monkeypatch.setattr(mm, "compute_cvd_skew", lambda: (0.01, "BEARISH_DIV"))
        book = {"best_bid": 0.49, "best_ask": 0.51, "spread": 0.02}
        mm._refresh_quotes(book)
        assert mm.current_bid > 0.0
        assert mm.current_ask > 0.0
        assert mm.current_bid_size > 0
        assert mm.current_ask_size > 0

    def test_gate_enabled_skips_non_neutral(self, monkeypatch):
        """MM_NEUTRAL_ONLY=True + non-NEUTRAL signal → sizes forced to 0, early return."""
        monkeypatch.setattr(bot, "MM_NEUTRAL_ONLY", True)
        mm = self._make_mm(monkeypatch)
        monkeypatch.setattr(mm, "compute_cvd_skew", lambda: (0.015, "BULLISH_DIV"))
        book = {"best_bid": 0.49, "best_ask": 0.51, "spread": 0.02}
        mm._refresh_quotes(book)
        assert mm.current_bid_size == 0
        assert mm.current_ask_size == 0

    def test_gate_enabled_allows_neutral(self, monkeypatch):
        """MM_NEUTRAL_ONLY=True + NEUTRAL signal → quotes posted normally."""
        monkeypatch.setattr(bot, "MM_NEUTRAL_ONLY", True)
        mm = self._make_mm(monkeypatch)
        monkeypatch.setattr(mm, "compute_cvd_skew", lambda: (0.0, "NEUTRAL"))
        book = {"best_bid": 0.49, "best_ask": 0.51, "spread": 0.02}
        mm._refresh_quotes(book)
        assert mm.current_bid > 0.0
        assert mm.current_ask > 0.0
        assert mm.current_bid_size > 0
        assert mm.current_ask_size > 0
