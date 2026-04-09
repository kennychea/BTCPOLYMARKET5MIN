"""Unit tests for MMInventory class."""
import pytest
from cvd_5min_bot import MMInventory


class TestMMInventoryInitial:
    def test_initial_state(self):
        inv = MMInventory()
        assert inv.net_position == 0
        assert inv.cash == 0.0
        assert inv.fills == []

    def test_reset_cycle(self):
        inv = MMInventory()
        inv.record_fill("BUY", 0.48, 10)
        inv.reset_cycle()
        assert inv.net_position == 0
        assert inv.cash == 0.0
        assert inv.fills == []


class TestMMInventoryFills:
    def test_buy_increases_position(self):
        inv = MMInventory()
        inv.record_fill("BUY", 0.48, 10)
        assert inv.net_position == 10
        assert inv.cash == pytest.approx(-4.80)

    def test_sell_decreases_position(self):
        inv = MMInventory()
        inv.record_fill("SELL", 0.52, 10)
        assert inv.net_position == -10
        assert inv.cash == pytest.approx(5.20)

    def test_fills_tracked(self):
        inv = MMInventory()
        inv.record_fill("BUY", 0.48, 10)
        assert len(inv.fills) == 1
        assert inv.fills[0]["side"] == "BUY"
        assert inv.fills[0]["price"] == 0.48
        assert inv.fills[0]["size"] == 10
        assert inv.fills[0]["net_position_after"] == 10

    def test_fill_count(self):
        inv = MMInventory()
        inv.record_fill("BUY", 0.48, 10)
        inv.record_fill("SELL", 0.52, 5)
        inv.record_fill("BUY", 0.47, 10)
        total, buys, sells = inv.get_fill_count()
        assert total == 3
        assert buys == 2
        assert sells == 1


class TestMMInventoryPnL:
    def test_round_trip_pnl(self):
        """Buy at 0.48, sell at 0.52 → P&L = 0.04 per share × 10 = 0.40."""
        inv = MMInventory()
        inv.record_fill("BUY", 0.48, 10)
        inv.record_fill("SELL", 0.52, 10)
        assert inv.net_position == 0
        assert inv.cash == pytest.approx(0.40)
        assert inv.get_pnl(0.50) == pytest.approx(0.40)

    def test_pnl_with_long_inventory(self):
        """Holding 10 shares bought at 0.48, marked to market."""
        inv = MMInventory()
        inv.record_fill("BUY", 0.48, 10)
        assert inv.get_pnl(0.52) == pytest.approx(0.40)
        assert inv.get_pnl(0.45) == pytest.approx(-0.30)

    def test_pnl_with_short_inventory(self):
        """Sold 10 shares at 0.52, marked to market."""
        inv = MMInventory()
        inv.record_fill("SELL", 0.52, 10)
        assert inv.get_pnl(0.48) == pytest.approx(0.40)
        assert inv.get_pnl(0.55) == pytest.approx(-0.30)

    def test_multiple_trades_pnl(self):
        """Multiple buys and sells → correct net P&L."""
        inv = MMInventory()
        inv.record_fill("BUY", 0.47, 10)
        inv.record_fill("SELL", 0.53, 10)
        inv.record_fill("BUY", 0.48, 5)
        assert inv.net_position == 5
        assert inv.cash == pytest.approx(-1.80)
        assert inv.get_pnl(0.50) == pytest.approx(0.70)
