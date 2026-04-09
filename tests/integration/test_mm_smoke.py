"""Smoke tests for market maker constants and class creation."""
import cvd_5min_bot as bot


class TestMMConstants:
    def test_strategy_constant(self):
        assert bot.STRATEGY in ("stink", "mm")

    def test_mm_base_spread(self):
        assert 0 < bot.MM_BASE_SPREAD < 1

    def test_mm_order_size(self):
        assert bot.MM_ORDER_SIZE > 0

    def test_mm_max_inventory(self):
        assert bot.MM_MAX_INVENTORY > 0

    def test_mm_max_cvd_skew(self):
        assert 0 < bot.MM_MAX_CVD_SKEW < bot.MM_BASE_SPREAD

    def test_mm_refresh_interval(self):
        assert bot.MM_REFRESH_INTERVAL > 0

    def test_mm_stop_quoting_sec(self):
        assert bot.MM_STOP_QUOTING_SEC > 0

    def test_mm_log_file(self):
        assert bot.MM_LOG_FILE.endswith("mm_paper_trades.csv")


class TestCVDMarketMakerCreation:
    def test_creates_without_error(self):
        feed = bot.BinanceCVDFeed(max_trades=100)
        mm = bot.CVDMarketMaker(feed)
        assert mm.inventory.net_position == 0
        assert mm.current_bid == 0.0
        assert mm.current_ask == 0.0

    def test_reset_clears_state(self):
        feed = bot.BinanceCVDFeed(max_trades=100)
        mm = bot.CVDMarketMaker(feed)
        mm.current_bid = 0.48
        mm.current_ask = 0.52
        mm.cycle_fills = 5
        mm.reset()
        assert mm.current_bid == 0.0
        assert mm.current_ask == 0.0
        assert mm.cycle_fills == 0
        assert mm.inventory.net_position == 0

    def test_compute_cvd_skew_no_trades(self):
        feed = bot.BinanceCVDFeed(max_trades=100)
        mm = bot.CVDMarketMaker(feed)
        skew, sig_type = mm.compute_cvd_skew()
        assert skew == 0.0
        assert sig_type == "NEUTRAL"

    def test_cancel_orders_paper_no_crash(self):
        feed = bot.BinanceCVDFeed(max_trades=100)
        mm = bot.CVDMarketMaker(feed)
        mm.up_token_id = "fake-token"
        mm.cancel_orders()  # should not crash in paper mode
