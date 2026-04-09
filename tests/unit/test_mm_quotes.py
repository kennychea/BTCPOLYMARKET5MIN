"""Unit tests for market maker quote calculation."""
import pytest
from cvd_5min_bot import calculate_mm_quotes


class TestSymmetricQuotes:
    def test_no_skew_no_inventory(self):
        """No CVD skew, no inventory → symmetric around midpoint."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=0, max_inventory=50, order_size=10,
        )
        assert result["bid_price"] == 0.48
        assert result["ask_price"] == 0.52
        assert result["bid_size"] == 10
        assert result["ask_size"] == 10

    def test_spread_maintained(self):
        """Spread should be at least base_spread."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.06, cvd_skew=0.0,
            inventory=0, max_inventory=50, order_size=10,
        )
        spread = result["ask_price"] - result["bid_price"]
        assert spread >= 0.06


class TestCVDSkew:
    def test_bullish_shifts_up(self):
        """Positive CVD skew shifts both prices up."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.01,
            inventory=0, max_inventory=50, order_size=10,
        )
        assert result["bid_price"] > 0.48
        assert result["ask_price"] > 0.52

    def test_bearish_shifts_down(self):
        """Negative CVD skew shifts both prices down."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=-0.01,
            inventory=0, max_inventory=50, order_size=10,
        )
        assert result["bid_price"] < 0.48
        assert result["ask_price"] < 0.52


class TestInventorySkew:
    def test_long_shifts_down(self):
        """Positive inventory → lower prices (encourage selling)."""
        neutral = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=0, max_inventory=50, order_size=10,
        )
        long_inv = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=25, max_inventory=50, order_size=10,
        )
        assert long_inv["bid_price"] < neutral["bid_price"]
        assert long_inv["ask_price"] < neutral["ask_price"]

    def test_short_shifts_up(self):
        """Negative inventory → higher prices (encourage buying)."""
        neutral = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=0, max_inventory=50, order_size=10,
        )
        short_inv = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=-25, max_inventory=50, order_size=10,
        )
        assert short_inv["bid_price"] > neutral["bid_price"]
        assert short_inv["ask_price"] > neutral["ask_price"]

    def test_max_inventory_stops_buying(self):
        """At max inventory → bid_size = 0."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=50, max_inventory=50, order_size=10,
        )
        assert result["bid_size"] == 0
        assert result["ask_size"] == 10

    def test_max_short_stops_selling(self):
        """At -max inventory → ask_size = 0."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.04, cvd_skew=0.0,
            inventory=-50, max_inventory=50, order_size=10,
        )
        assert result["bid_size"] == 10
        assert result["ask_size"] == 0


class TestPriceBounds:
    def test_minimum_spread_one_tick(self):
        """Spread never less than tick_size."""
        result = calculate_mm_quotes(
            midpoint=0.50, base_spread=0.001, cvd_skew=0.0,
            inventory=0, max_inventory=50, order_size=10,
            tick_size=0.01,
        )
        assert result["ask_price"] - result["bid_price"] >= 0.01

    def test_low_bound(self):
        """Prices never below 0.01."""
        result = calculate_mm_quotes(
            midpoint=0.02, base_spread=0.04, cvd_skew=-0.01,
            inventory=0, max_inventory=50, order_size=10,
        )
        assert result["bid_price"] >= 0.01
        assert result["ask_price"] >= 0.01

    def test_high_bound(self):
        """Prices never above 0.99."""
        result = calculate_mm_quotes(
            midpoint=0.98, base_spread=0.04, cvd_skew=0.01,
            inventory=0, max_inventory=50, order_size=10,
        )
        assert result["bid_price"] <= 0.99
        assert result["ask_price"] <= 0.99

    def test_prices_rounded_to_tick(self):
        """Prices are multiples of tick_size (0.01)."""
        result = calculate_mm_quotes(
            midpoint=0.505, base_spread=0.04, cvd_skew=0.003,
            inventory=7, max_inventory=50, order_size=10,
            tick_size=0.01,
        )
        assert round(result["bid_price"] * 100) == result["bid_price"] * 100
        assert round(result["ask_price"] * 100) == result["ask_price"] * 100
