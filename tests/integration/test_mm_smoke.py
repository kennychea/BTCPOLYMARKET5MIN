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
