"""Unit tests for CVD_INVERT_SIGNAL flip logic inside check_cvd_signal."""
import time
import pytest

import cvd_5min_bot as bot


class FakeFeed:
    """Stub feed returning a fixed canned trade list so check_cvd_signal
    produces a deterministic signal. We only care about the flip wrapper,
    not the divergence math itself.
    """
    def __init__(self, trades):
        self._trades = trades

    def get_trades_since(self, seconds):
        return list(self._trades)

    def get_last_price(self):
        return 84000.0

    def get_trade_count(self):
        return len(self._trades)


def _stub_signal(monkeypatch, direction, signal_type="BULLISH_DIV", detail="stub"):
    """Monkeypatch check_cvd_signal's internals so it returns a fixed tuple.

    Easier than crafting real CVD-diverging trade data: we replace the
    function's core divergence detector to yield the desired (direction,
    signal_type, detail). The flip wrapper still runs on top.
    """
    # Replace detect_divergence to always return our forced signal.
    def fake_detect(price_change, cvd_value):
        return (signal_type, "stub", direction, 100)
    monkeypatch.setattr(bot, "detect_divergence", fake_detect)
    # Provide enough stub trades to pass the len<50 gate.
    # Timestamps must be within the longest CVD timeframe (15 min = 900 s)
    # so the time-window slice inside check_cvd_signal keeps them.
    # Tuple: (timestamp_ms, price, qty, is_buy, delta_usd)
    now_ms = time.time() * 1000
    fake_trades = [(now_ms - i * 100, 84000.0, 0.1, True, 8400.0) for i in range(100)]
    return FakeFeed(fake_trades)


class TestSignalFlip:
    def test_flip_disabled_returns_original_up(self, monkeypatch):
        monkeypatch.setattr(bot, "CVD_INVERT_SIGNAL", False)
        feed = _stub_signal(monkeypatch, "UP")
        direction, signal_type, _ = bot.check_cvd_signal(feed)
        assert direction == "UP"
        assert signal_type == "BULLISH_DIV"

    def test_flip_disabled_returns_original_down(self, monkeypatch):
        monkeypatch.setattr(bot, "CVD_INVERT_SIGNAL", False)
        feed = _stub_signal(monkeypatch, "DOWN", signal_type="BEARISH_DIV")
        direction, signal_type, _ = bot.check_cvd_signal(feed)
        assert direction == "DOWN"
        assert signal_type == "BEARISH_DIV"

    def test_flip_enabled_up_becomes_down(self, monkeypatch):
        monkeypatch.setattr(bot, "CVD_INVERT_SIGNAL", True)
        feed = _stub_signal(monkeypatch, "UP")
        direction, signal_type, _ = bot.check_cvd_signal(feed)
        assert direction == "DOWN"
        # Signal type label is NOT renamed — only the bet target flips.
        assert signal_type == "BULLISH_DIV"

    def test_flip_enabled_down_becomes_up(self, monkeypatch):
        monkeypatch.setattr(bot, "CVD_INVERT_SIGNAL", True)
        feed = _stub_signal(monkeypatch, "DOWN", signal_type="BEARISH_DIV")
        direction, signal_type, _ = bot.check_cvd_signal(feed)
        assert direction == "UP"
        assert signal_type == "BEARISH_DIV"
