"""Unit tests for CVD core logic — compute_volume_cvd() and detect_divergence()."""
import pytest
from cvd_5min_bot import compute_volume_cvd, detect_divergence


# ── compute_volume_cvd ──────────────────────────────────────────────────

class TestComputeVolumeCvd:
    def test_empty_trades(self):
        assert compute_volume_cvd([]) == (0.0, 0.0, 0)

    def test_single_trade(self):
        # Less than 2 trades → returns zeros
        trades = [(1000, 84000.0, 0.1, True, 8400.0)]
        assert compute_volume_cvd(trades) == (0.0, 0.0, 0)

    def test_all_buys_positive_cvd(self):
        """All buyer-initiated trades → CVD must be positive."""
        trades = [
            (1000, 84000.0, 0.1, True, 8400.0),
            (2000, 84010.0, 0.2, True, 16802.0),
            (3000, 84020.0, 0.05, True, 4201.0),
        ]
        cvd, price_change, count = compute_volume_cvd(trades)
        assert cvd > 0
        assert cvd == pytest.approx(8400.0 + 16802.0 + 4201.0)
        assert count == 3

    def test_all_sells_negative_cvd(self):
        """All seller-initiated trades → CVD must be negative."""
        trades = [
            (1000, 84000.0, 0.1, False, -8400.0),
            (2000, 83990.0, 0.2, False, -16798.0),
        ]
        cvd, price_change, count = compute_volume_cvd(trades)
        assert cvd < 0
        assert cvd == pytest.approx(-8400.0 + -16798.0)
        assert count == 2

    def test_mixed_trades_net_cvd(self):
        """Mixed buys and sells — CVD is the net."""
        trades = [
            (1000, 84000.0, 0.1, True, 8400.0),     # +8400
            (2000, 84000.0, 0.2, False, -16800.0),   # -16800
            (3000, 84000.0, 0.05, True, 4200.0),     # +4200
        ]
        cvd, _, count = compute_volume_cvd(trades)
        assert cvd == pytest.approx(8400.0 - 16800.0 + 4200.0)
        assert count == 3

    def test_price_change_calculation(self):
        """Price change % is (last - first) / first * 100."""
        trades = [
            (1000, 80000.0, 0.1, True, 8000.0),
            (2000, 80100.0, 0.1, True, 8010.0),
        ]
        _, price_change, _ = compute_volume_cvd(trades)
        expected = (80100.0 - 80000.0) / 80000.0 * 100
        assert price_change == pytest.approx(expected)

    def test_price_decrease(self):
        """Negative price change when price drops."""
        trades = [
            (1000, 80000.0, 0.1, False, -8000.0),
            (2000, 79900.0, 0.1, False, -7990.0),
        ]
        _, price_change, _ = compute_volume_cvd(trades)
        assert price_change < 0


# ── detect_divergence ────────────────────────────────────────────────────

class TestDetectDivergence:
    def test_bullish_divergence(self):
        """Price down > 0.07% but CVD positive > 500 → BULLISH_DIV, UP."""
        signal_type, desc, direction, strength = detect_divergence(-0.10, 600.0)
        assert signal_type == "BULLISH_DIV"
        assert direction == "UP"
        assert strength > 0

    def test_bearish_divergence(self):
        """Price up > 0.07% but CVD negative < -500 → BEARISH_DIV, DOWN."""
        signal_type, desc, direction, strength = detect_divergence(0.10, -600.0)
        assert signal_type == "BEARISH_DIV"
        assert direction == "DOWN"
        assert strength > 0

    def test_strong_bull(self):
        """Price up > 0.03% AND CVD up > 1000 → STRONG_BULL, UP."""
        signal_type, desc, direction, strength = detect_divergence(0.05, 1200.0)
        assert signal_type == "STRONG_BULL"
        assert direction == "UP"
        assert strength > 0

    def test_strong_bear(self):
        """Price down < -0.03% AND CVD down < -1000 → STRONG_BEAR, DOWN."""
        signal_type, desc, direction, strength = detect_divergence(-0.05, -1200.0)
        assert signal_type == "STRONG_BEAR"
        assert direction == "DOWN"
        assert strength > 0

    def test_neutral_small_moves(self):
        """Small price + small CVD → NEUTRAL/FLAT or weak signal."""
        signal_type, desc, direction, strength = detect_divergence(0.01, 50.0)
        assert signal_type in ("NEUTRAL", "BULLISH")
        if signal_type == "NEUTRAL":
            assert direction == "FLAT"
            assert strength == 0

    def test_below_divergence_threshold(self):
        """Price just under threshold → no divergence signal."""
        signal_type, _, direction, _ = detect_divergence(-0.05, 600.0)
        assert signal_type != "BULLISH_DIV"

    def test_cvd_below_threshold(self):
        """CVD below 500 threshold → no divergence even if price qualifies."""
        signal_type, _, _, _ = detect_divergence(-0.10, 300.0)
        assert signal_type != "BULLISH_DIV"

    def test_strength_capped_at_100(self):
        """Strength never exceeds 100 even with huge CVD."""
        _, _, _, strength = detect_divergence(-0.10, 50000.0)
        assert strength <= 100

    def test_divergence_priority_over_strong(self):
        """Divergence (price down + CVD up) is checked before strong trending."""
        signal_type, _, direction, _ = detect_divergence(-0.10, 1500.0)
        assert signal_type == "BULLISH_DIV"
        assert direction == "UP"
