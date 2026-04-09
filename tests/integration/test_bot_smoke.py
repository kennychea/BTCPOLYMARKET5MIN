"""Smoke tests — verify the bot module imports cleanly and paper mode is wired correctly."""
import cvd_5min_bot as bot


class TestModuleImport:
    def test_paper_mode_flag_is_true(self):
        """PAPER_MODE should be True (set by conftest.py)."""
        assert bot.PAPER_MODE is True

    def test_critical_constants_exist(self):
        assert bot.BOT_POLL_INTERVAL > 0
        assert bot.MARKET_DURATION == 300
        assert bot.PULLBACK_PCT > 0
        assert bot.ORDER_SIZE_USD > 0
        assert bot.BINANCE_WS_URL.startswith("wss://")
        assert bot.GAMMA_API_URL.startswith("https://")
        assert bot.CLOB_HOST.startswith("https://")

    def test_cvd_thresholds_configured(self):
        assert bot.CVD_DIVERGENCE_PRICE_THRESH > 0
        assert bot.CVD_DIVERGENCE_CVD_THRESH > 0
        assert bot.CVD_STRONG_PRICE_THRESH > 0
        assert bot.CVD_STRONG_CVD_THRESH > 0
        assert bot.CVD_STRONG_CVD_THRESH > bot.CVD_DIVERGENCE_CVD_THRESH

    def test_signal_timeframes_ordered(self):
        tfs = bot.CVD_SIGNAL_TIMEFRAMES
        assert tfs == tuple(sorted(tfs))
        assert all(tf > 0 for tf in tfs)

    def test_paper_log_file_set(self):
        assert bot.PAPER_LOG_FILE.endswith("paper_trades.csv")


class TestPaperModeGuards:
    def test_init_clob_returns_none(self):
        """In paper mode, init_clob_client returns None without hitting network."""
        result = bot.init_clob_client()
        assert result is None

    def test_place_limit_order_returns_paper_id(self):
        """In paper mode, place_limit_order returns a fake order with paper=True."""
        result = bot.place_limit_order("fake-token", "BUY", 0.45, 10)
        assert result["paper"] is True
        assert result["orderID"].startswith("PAPER-")

    def test_check_poly_filled_returns_true(self):
        """In paper mode, always returns True (instant fill sim)."""
        assert bot.check_poly_filled("fake-token") is True

    def test_cancel_token_orders_no_crash(self):
        """In paper mode, cancel is a no-op."""
        bot.cancel_token_orders("fake-token")

    def test_close_hedge_no_crash(self):
        """In paper mode, close_hyperliquid_hedge is a no-op."""
        bot.close_hyperliquid_hedge()


class TestBinanceCVDFeedCreation:
    def test_feed_creates_without_error(self):
        feed = bot.BinanceCVDFeed(max_trades=100)
        assert feed.get_last_price() == 0.0
        assert feed.get_trade_count() == 0
        assert feed.get_trades_since(60) == []

    def test_feed_default_max_trades(self):
        feed = bot.BinanceCVDFeed()
        assert feed.trades.maxlen == 50_000
