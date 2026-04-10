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
    def test_creates_csv_with_new_schema_columns(self):
        bot.log_paper_signal(
            market_ts=1000000,
            signal_type="BULLISH_DIV",
            direction="UP",
            detail="test signal",
            stink_price=0.45,
            btc_price_at_signal=84000.0,
            market_slug="btc-updown-5m-1000000",
            condition_id="0xabc",
            shares=11,
            direction_original="UP",
            invert_flag=False,
        )
        assert os.path.exists(bot.PAPER_LOG_FILE)
        df = pd.read_csv(bot.PAPER_LOG_FILE)
        assert len(df) == 1
        expected_cols = {
            "timestamp", "market_ts", "market_slug", "condition_id",
            "signal_type", "direction", "direction_original", "invert_flag",
            "detail", "stink_price", "shares",
            "btc_price_at_signal", "btc_price_at_close",
            "outcome_binance_perp", "outcome_binance_spot", "outcome_polymarket",
            "signal_correct_binance_perp", "signal_correct_binance_spot",
            "signal_correct_polymarket",
            "price_change_pct", "filled",
        }
        assert set(df.columns) == expected_cols

    def test_values_stored_correctly(self):
        bot.log_paper_signal(
            market_ts=1000000,
            signal_type="STRONG_BEAR",
            direction="UP",              # flipped from DOWN
            detail="strong selling",
            stink_price=0.52,
            btc_price_at_signal=85000.5,
            market_slug="btc-updown-5m-1000000",
            condition_id="0xdeadbeef",
            shares=9,
            direction_original="DOWN",
            invert_flag=True,
        )
        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["market_ts"] == 1000000
        assert row["signal_type"] == "STRONG_BEAR"
        assert row["direction"] == "UP"
        assert row["direction_original"] == "DOWN"
        assert bool(row["invert_flag"]) is True
        assert row["stink_price"] == 0.52
        assert row["shares"] == 9
        assert row["condition_id"] == "0xdeadbeef"
        assert row["btc_price_at_signal"] == 85000.5
        assert row["btc_price_at_close"] == 0.0
        assert bool(row["filled"]) is False
        # Outcome columns empty initially
        for col in ("outcome_binance_perp", "outcome_binance_spot", "outcome_polymarket",
                    "signal_correct_binance_perp", "signal_correct_binance_spot",
                    "signal_correct_polymarket"):
            val = row[col]
            assert pd.isna(val) or val == ""

    def test_appends_to_existing(self):
        bot.log_paper_signal(
            market_ts=1000, signal_type="BULLISH_DIV", direction="UP",
            detail="first", stink_price=0.4, btc_price_at_signal=84000.0,
            market_slug="slug-1", condition_id="0x1", shares=12,
            direction_original="UP", invert_flag=False,
        )
        bot.log_paper_signal(
            market_ts=2000, signal_type="BEARISH_DIV", direction="DOWN",
            detail="second", stink_price=0.5, btc_price_at_signal=84100.0,
            market_slug="slug-2", condition_id="0x2", shares=10,
            direction_original="DOWN", invert_flag=False,
        )
        df = pd.read_csv(bot.PAPER_LOG_FILE)
        assert len(df) == 2
        assert df.iloc[0]["signal_type"] == "BULLISH_DIV"
        assert df.iloc[1]["signal_type"] == "BEARISH_DIV"


# ── resolve_paper_outcome ────────────────────────────────────────────────

class TestResolvePaperOutcome:
    def _seed(self, market_ts, direction, btc_price, condition_id="0xabc", shares=10,
              stink_price=0.45):
        bot.log_paper_signal(
            market_ts=market_ts,
            signal_type="BULLISH_DIV",
            direction=direction,
            detail="test",
            stink_price=stink_price,
            btc_price_at_signal=btc_price,
            market_slug="slug",
            condition_id=condition_id,
            shares=shares,
            direction_original=direction,
            invert_flag=False,
        )

    def test_resolves_all_three_outcome_columns_up(self, monkeypatch):
        """BTC moves up, Polymarket returns UP, spot returns 84100 — all three agree."""
        self._seed(1000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 84105.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "UP",
        )

        bot.resolve_paper_outcome(1000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["outcome_binance_perp"] == "UP"
        assert row["signal_correct_binance_perp"] == "YES"
        assert row["outcome_binance_spot"] == "UP"
        assert row["signal_correct_binance_spot"] == "YES"
        assert row["outcome_polymarket"] == "UP"
        assert row["signal_correct_polymarket"] == "YES"
        assert bool(row["filled"]) is True
        assert row["btc_price_at_close"] == 84100.0

    def test_polymarket_pending_keeps_row_reevaluable(self, monkeypatch):
        """If Polymarket returns PENDING, the row is logged and re-runnable."""
        self._seed(2000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 84110.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "PENDING",
        )

        bot.resolve_paper_outcome(2000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["outcome_polymarket"] == "PENDING"
        assert row["signal_correct_polymarket"] == "PENDING"
        assert row["outcome_binance_perp"] == "UP"  # other sources still filled in
        assert row["outcome_binance_spot"] == "UP"

    def test_polymarket_none_writes_unknown(self, monkeypatch):
        """Transient HTTP failure → UNKNOWN, no retry."""
        self._seed(3000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 84110.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: None,
        )

        bot.resolve_paper_outcome(3000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["outcome_polymarket"] == "UNKNOWN"
        assert row["signal_correct_polymarket"] == "UNKNOWN"

    def test_binance_spot_none_writes_unknown_for_spot_only(self, monkeypatch):
        """Spot fetch fails but Polymarket still works — Polymarket drives gate."""
        self._seed(4000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: None,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "UP",
        )

        bot.resolve_paper_outcome(4000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["outcome_binance_spot"] == "UNKNOWN"
        assert row["signal_correct_binance_spot"] == "UNKNOWN"
        assert row["outcome_polymarket"] == "UP"
        assert row["signal_correct_polymarket"] == "YES"

    def test_wrong_direction_marked_no(self, monkeypatch):
        """Predicted UP, Polymarket says DOWN → signal_correct_polymarket = NO."""
        self._seed(5000, "UP", 84000.0)
        feed = FakeFeed(83900.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 83890.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "DOWN",
        )

        bot.resolve_paper_outcome(5000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["signal_correct_polymarket"] == "NO"

    def test_filled_flag_propagates(self, monkeypatch):
        """When the bot passes filled=False, the row marks filled=False."""
        self._seed(6000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 84105.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "UP",
        )

        bot.resolve_paper_outcome(6000, feed, filled=False)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert bool(row["filled"]) is False

    def test_pending_retries_next_cycle(self, monkeypatch):
        """A row with PENDING signal_correct_polymarket is re-resolved on next call."""
        self._seed(7000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        # First call: Polymarket returns PENDING
        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 84105.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "PENDING",
        )
        bot.resolve_paper_outcome(7000, feed, filled=True)

        # Second call (next cycle): Polymarket now resolved
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "UP",
        )
        bot.resolve_paper_outcome(7000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        assert row["outcome_polymarket"] == "UP"
        assert row["signal_correct_polymarket"] == "YES"

    def test_skips_already_resolved_non_pending(self, monkeypatch):
        """A row with signal_correct_polymarket in {YES,NO} is not re-touched."""
        self._seed(8000, "UP", 84000.0)
        feed = FakeFeed(84100.0)

        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: 84105.0,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "UP",
        )
        bot.resolve_paper_outcome(8000, feed, filled=True)

        # A second call — even with a different Polymarket answer — must NOT overwrite
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: "DOWN",
        )
        bot.resolve_paper_outcome(8000, feed, filled=True)

        df = pd.read_csv(bot.PAPER_LOG_FILE)
        assert df.iloc[0]["outcome_polymarket"] == "UP"
        assert df.iloc[0]["signal_correct_polymarket"] == "YES"

    def test_no_file_is_noop(self):
        feed = FakeFeed(84000.0)
        bot.resolve_paper_outcome(999, feed, filled=False)  # no crash

    def test_zero_feed_price_skips(self, monkeypatch):
        self._seed(9000, "UP", 84000.0)
        feed = FakeFeed(0.0)
        monkeypatch.setattr(
            "truth_sources.fetch_binance_spot_price",
            lambda ts, timeout_sec=5.0: None,
        )
        monkeypatch.setattr(
            "truth_sources.fetch_polymarket_resolution",
            lambda cid, timeout_sec=5.0: None,
        )
        bot.resolve_paper_outcome(9000, feed, filled=False)
        df = pd.read_csv(bot.PAPER_LOG_FILE)
        row = df.iloc[0]
        # Nothing resolvable — all three outcome columns should be UNKNOWN or blank
        assert row["outcome_binance_perp"] in ("", "UNKNOWN") or pd.isna(row["outcome_binance_perp"])


# ── print_paper_summary ──────────────────────────────────────────────────

class TestPrintPaperSummary:
    def test_no_file_no_crash(self, capsys):
        bot.print_paper_summary()
        out = capsys.readouterr().out
        assert "No paper trades" in out

    def test_with_resolved_data(self, capsys, monkeypatch):
        # Write two rows directly — one YES, one NO — with new-schema columns.
        df = pd.DataFrame([
            {
                "timestamp": "2026-04-10T12:00:00",
                "market_ts": 1000, "market_slug": "s1", "condition_id": "0x1",
                "signal_type": "BULLISH_DIV", "direction": "UP",
                "direction_original": "UP", "invert_flag": False,
                "detail": "t1", "stink_price": 0.4, "shares": 12,
                "btc_price_at_signal": 84000.0, "btc_price_at_close": 84100.0,
                "outcome_binance_perp": "UP", "outcome_binance_spot": "UP",
                "outcome_polymarket": "UP",
                "signal_correct_binance_perp": "YES",
                "signal_correct_binance_spot": "YES",
                "signal_correct_polymarket": "YES",
                "price_change_pct": 0.12, "filled": True,
            },
            {
                "timestamp": "2026-04-10T12:05:00",
                "market_ts": 2000, "market_slug": "s2", "condition_id": "0x2",
                "signal_type": "BEARISH_DIV", "direction": "DOWN",
                "direction_original": "DOWN", "invert_flag": False,
                "detail": "t2", "stink_price": 0.5, "shares": 10,
                "btc_price_at_signal": 84100.0, "btc_price_at_close": 84200.0,
                "outcome_binance_perp": "UP", "outcome_binance_spot": "UP",
                "outcome_polymarket": "UP",
                "signal_correct_binance_perp": "NO",
                "signal_correct_binance_spot": "NO",
                "signal_correct_polymarket": "NO",
                "price_change_pct": 0.12, "filled": True,
            },
        ])
        df.to_csv(bot.PAPER_LOG_FILE, index=False)

        bot.print_paper_summary()
        out = capsys.readouterr().out
        assert "Accuracy" in out
        assert "50.0%" in out
