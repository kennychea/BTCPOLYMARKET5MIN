"""Unit tests for truth_sources.fetch_binance_spot_price and fetch_polymarket_resolution."""
from unittest.mock import MagicMock, patch

import pytest

import truth_sources


# ── fetch_binance_spot_price ────────────────────────────────────────────

class TestFetchBinanceSpot:
    def test_success_returns_close_price(self):
        """Mock a valid Klines response and assert the close price is parsed."""
        # Binance Klines format: [[openTime, open, high, low, close, volume, ...], ...]
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = [
            [1712000000000, "84000.00", "84200.00", "83900.00", "84150.50", "12.5",
             1712000059999, "1052000.0", 100, "6.0", "505000.0", "0"]
        ]
        with patch("truth_sources.requests.get", return_value=fake_response):
            price = truth_sources.fetch_binance_spot_price(1712000000)
        assert price == 84150.50

    def test_timeout_returns_none(self):
        import requests
        with patch("truth_sources.requests.get", side_effect=requests.Timeout("slow")):
            price = truth_sources.fetch_binance_spot_price(1712000000)
        assert price is None

    def test_http_error_returns_none(self):
        fake_response = MagicMock()
        fake_response.status_code = 500
        with patch("truth_sources.requests.get", return_value=fake_response):
            price = truth_sources.fetch_binance_spot_price(1712000000)
        assert price is None

    def test_empty_response_returns_none(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = []
        with patch("truth_sources.requests.get", return_value=fake_response):
            price = truth_sources.fetch_binance_spot_price(1712000000)
        assert price is None

    def test_exception_returns_none(self):
        with patch("truth_sources.requests.get", side_effect=ValueError("corrupt json")):
            price = truth_sources.fetch_binance_spot_price(1712000000)
        assert price is None


# ── fetch_polymarket_resolution ──────────────────────────────────────────

class TestFetchPolymarketResolution:
    def test_resolved_up(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = [{"conditionId": "0xabc", "resolvedPrice": "1.0"}]
        with patch("truth_sources.requests.get", return_value=fake_response):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome == "UP"

    def test_resolved_down(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = [{"conditionId": "0xabc", "resolvedPrice": "0.0"}]
        with patch("truth_sources.requests.get", return_value=fake_response):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome == "DOWN"

    def test_pending_resolution(self):
        """Null resolvedPrice means market not yet settled."""
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = [{"conditionId": "0xabc", "resolvedPrice": None}]
        with patch("truth_sources.requests.get", return_value=fake_response):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome == "PENDING"

    def test_missing_resolvedprice_field_is_pending(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = [{"conditionId": "0xabc"}]
        with patch("truth_sources.requests.get", return_value=fake_response):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome == "PENDING"

    def test_http_error_returns_none(self):
        """HTTP 500 is a transient error distinct from PENDING — we return None."""
        fake_response = MagicMock()
        fake_response.status_code = 500
        with patch("truth_sources.requests.get", return_value=fake_response):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome is None

    def test_empty_list_returns_none(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = []
        with patch("truth_sources.requests.get", return_value=fake_response):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome is None

    def test_exception_returns_none(self):
        import requests
        with patch("truth_sources.requests.get", side_effect=requests.Timeout("slow")):
            outcome = truth_sources.fetch_polymarket_resolution("0xabc")
        assert outcome is None
