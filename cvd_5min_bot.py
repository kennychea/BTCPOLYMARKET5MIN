#!/usr/bin/env python3

# ============================================================================
# 🌙 CVD 5-MINUTE BOT v2.0  (MoonDev pattern, Binance CVD upgrade)
#
# Trades BTC 5-minute Up/Down markets on Polymarket with stink bid entries
# + Hyperliquid hedge. Based on MoonDev's cvd_5min_bot architecture.
#
# IMPROVEMENT OVER MOONDEV:
#   MoonDev uses tick rule (+1/-1 per price tick) for CVD.
#   We use Binance real-time trades with VOLUME-WEIGHTED CVD:
#     - Buyer-initiated trade → CVD += price * qty  (buy pressure in USD)
#     - Seller-initiated trade → CVD -= price * qty  (sell pressure in USD)
#   This detects institutional flow that tick rule misses.
#
# STRATEGY (CVD Divergence):
#   1. Stream BTC/USDT trades from Binance WebSocket in background thread
#   2. Compute volume-weighted CVD across multiple timeframes (1m-15m)
#   3. Detect divergence between price movement and CVD:
#       - Price DOWN but CVD POSITIVE → buyers accumulating → BUY UP
#       - Price UP but CVD NEGATIVE  → sellers distributing → BUY DOWN
#   4. Place stink bid at PULLBACK_PCT below current ask
#   5. On fill → hedge inverse on Hyperliquid
#
# SIGNALS:
#   BULLISH_DIV:  Price < -0.07% but CVD > +500 USD → accumulation → EXPECT UP
#   BEARISH_DIV:  Price > +0.07% but CVD < -500 USD → distribution → EXPECT DOWN
#   STRONG_BULL:  Price > +0.03% AND CVD > +1000 USD → full momentum UP
#   STRONG_BEAR:  Price < -0.03% AND CVD < -1000 USD → full momentum DOWN
#
# ============================================================================

import sys
import os
import time
import math
import json
import threading
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
import pandas as pd
import eth_account
from dotenv import load_dotenv
from termcolor import colored

# Load environment variables
load_dotenv()

# ============================================================================
# BINANCE WEBSOCKET (lazy import - websocket-client)
# ============================================================================
try:
    import websocket as ws_module
except ImportError:
    print(colored("websocket-client not installed. Run: pip install websocket-client", "red"))
    sys.exit(1)

# ============================================================================
# POLYMARKET CLOB CLIENT (lazy import check)
# ============================================================================
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, ApiCreds
    from py_clob_client.constants import POLYGON
except ImportError:
    print(colored("py-clob-client not installed. Run: pip install py-clob-client", "red"))
    sys.exit(1)

# ============================================================================
# HYPERLIQUID SDK (lazy import check)
# ============================================================================
try:
    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants as hl_constants
except ImportError:
    print(colored("hyperliquid-python-sdk not installed. Run: pip install hyperliquid-python-sdk", "red"))
    sys.exit(1)


# ============================================================================
# CONFIGURATION
# ============================================================================

# --- Bot Speed ---
BOT_POLL_INTERVAL = int(os.getenv("BOT_POLL_INTERVAL", "10"))

# --- CVD Signal Thresholds (volume-weighted, in USD notional) ---
CVD_DIVERGENCE_PRICE_THRESH = 0.07    # Price must move > 0.07% for divergence
CVD_DIVERGENCE_CVD_THRESH = 500       # CVD must exceed 500 USD for divergence
CVD_STRONG_PRICE_THRESH = 0.03        # Price > 0.03% for strong trending
CVD_STRONG_CVD_THRESH = 1000          # CVD > 1000 USD for strong trending signal

# --- Timeframes to scan (in seconds) ---
CVD_SIGNAL_TIMEFRAMES = (60, 180, 300, 600, 900)   # 1m, 3m, 5m, 10m, 15m
CVD_CONFIRM_TIMEFRAMES = (60, 180)                   # 1m and 3m for confirmation
CVD_PRIMARY_TF = 300                                 # 5m is the primary (matches market)
REQUIRE_CONFIRMATION = True

# --- Paper Trading Mode ---
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"

# --- Stink Bid / Order Parameters ---
PULLBACK_PCT = 0.03                   # 3% below current ask for stink entry
ORDER_SIZE_USD = float(os.getenv("ORDER_SIZE_USD", "5"))
NEG_RISK = True                       # negative risk for BTC markets
MAX_STINK_BID_PRICE = 0.55            # cap stink bid price

# --- Hedge (Hyperliquid) ---
HEDGE_ENABLED = True
HEDGE_SYMBOL = "BTC"
HEDGE_USD = float(os.getenv("HEDGE_SIZE_USD", "5"))
HEDGE_LEVERAGE = 3

# --- Position Mgmt ---
MAX_POSITIONS = 3
MIN_TIME_LEFT = 60                    # minimum 60s remaining to enter a trade

# --- Risk ---
DAILY_LOSS_LIMIT = -15.0
MAX_DAILY_TRADES = 20

# --- Market Timing ---
MARKET_DURATION = 300                 # 5-minute markets = 300 seconds

# --- Data Sources ---
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# --- Files ---
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TRADE_LOG_FILE = os.path.join(DATA_DIR, "cvd_5min_trades.csv")
PAPER_LOG_FILE = os.path.join(DATA_DIR, "paper_trades.csv")

# --- Market Making Strategy ---
STRATEGY = os.getenv("STRATEGY", "stink")             # "stink" (directional) or "mm" (market making)
MM_BASE_SPREAD = float(os.getenv("MM_BASE_SPREAD", "0.04"))   # 4-cent base spread
MM_ORDER_SIZE = int(os.getenv("MM_ORDER_SIZE", "10"))          # shares per side per quote
MM_MAX_INVENTORY = int(os.getenv("MM_MAX_INVENTORY", "50"))    # max net position (shares)
MM_MAX_CVD_SKEW = float(os.getenv("MM_MAX_CVD_SKEW", "0.02")) # max CVD-derived price shift
MM_REFRESH_INTERVAL = int(os.getenv("MM_REFRESH_INTERVAL", "10"))  # seconds between quote refreshes
MM_STOP_QUOTING_SEC = 30                                       # stop quoting N sec before market end
MM_LOG_FILE = os.path.join(DATA_DIR, "mm_paper_trades.csv")

# ET timezone (UTC-5)
ET = timezone(timedelta(hours=-5))


# ============================================================================
# HYPERLIQUID SETUP
# ============================================================================

HYPER_LIQUID_KEY = os.getenv("HYPER_LIQUID_KEY")
hl_account = None
hl_info = None
hl_exchange = None
hl_address = None

if HYPER_LIQUID_KEY:
    try:
        hl_account = eth_account.Account.from_key(HYPER_LIQUID_KEY)
        hl_address = hl_account.address
        hl_info = Info(hl_constants.MAINNET_API_URL, skip_ws=True)
        hl_exchange = Exchange(hl_account, hl_constants.MAINNET_API_URL)
        print(colored("✅ Hyperliquid account loaded!", "green"))
    except Exception as e:
        print(colored(f"⚠️ Hyperliquid init failed: {e}", "yellow"))
        HEDGE_ENABLED = False
else:
    if not PAPER_MODE:
        print(colored("⚠️ HYPER_LIQUID_KEY not found - hedging disabled", "yellow"))
    HEDGE_ENABLED = False


# ============================================================================
# BINANCE WEBSOCKET CVD FEED (replaces MoonDev API)
# ============================================================================

class BinanceCVDFeed:
    """
    Background thread that streams BTC/USDT trades from Binance.
    Computes volume-weighted CVD in real-time.

    Each trade: (timestamp_ms, price, qty, is_buy, delta_usd)
      - is_buy = True  → buyer-initiated (aggressor buys at ask) → positive CVD
      - is_buy = False → seller-initiated (aggressor sells at bid) → negative CVD
      - delta_usd = price * qty * (+1 if is_buy else -1)
    """

    RECONNECT_DELAY = 3.0

    def __init__(self, max_trades: int = 50_000):
        self.trades: deque = deque(maxlen=max_trades)
        self.last_price: float = 0.0
        self.trade_count: int = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._ws_app = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        print(colored("✅ Binance CVD feed started (background thread)", "green"))

    def stop(self):
        self._running = False
        if self._ws_app:
            try:
                self._ws_app.close()
            except Exception:
                pass

    def _run_forever(self):
        while self._running:
            try:
                self._ws_app = ws_module.WebSocketApp(
                    BINANCE_WS_URL,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self._ws_app.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                print(colored(f"⚠️ Binance WS exception: {e}", "yellow"))
            if self._running:
                print(colored(f"🔄 Binance WS reconnecting in {self.RECONNECT_DELAY}s...", "yellow"))
                time.sleep(self.RECONNECT_DELAY)

    def _on_open(self, ws):
        print(colored("✅ Binance WS connected - streaming BTC/USDT trades", "green"))

    def _on_error(self, ws, error):
        print(colored(f"⚠️ Binance WS error: {error}", "yellow"))

    def _on_close(self, ws, close_status_code, close_msg):
        if self._running:
            print(colored("🔴 Binance WS disconnected", "yellow"))

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get("e") != "trade":
                return

            price = float(data["p"])
            qty = float(data["q"])
            ts = int(data["T"])
            is_buyer_maker = data["m"]

            # m=True → buyer is maker → trade initiated by SELLER (sell pressure)
            # m=False → seller is maker → trade initiated by BUYER (buy pressure)
            is_buy = not is_buyer_maker
            notional = price * qty
            delta = notional if is_buy else -notional

            with self._lock:
                self.trades.append((ts, price, qty, is_buy, delta))
                self.last_price = price
                self.trade_count += 1
        except Exception:
            pass

    def get_trades_since(self, seconds_ago: int) -> list:
        """Get all trades from the last N seconds. Thread-safe."""
        cutoff_ms = (time.time() - seconds_ago) * 1000
        with self._lock:
            return [(ts, p, q, ib, d) for ts, p, q, ib, d in self.trades if ts >= cutoff_ms]

    def get_last_price(self) -> float:
        with self._lock:
            return self.last_price

    def get_trade_count(self) -> int:
        with self._lock:
            return self.trade_count


# ============================================================================
# CVD CORE LOGIC
# ============================================================================

def compute_volume_cvd(trades: list) -> tuple:
    """
    Compute volume-weighted CVD from trade data.
    Returns (cvd_value_usd, price_change_pct, trade_count)
    """
    if not trades or len(trades) < 2:
        return 0.0, 0.0, 0

    cvd = 0.0
    first_price = trades[0][1]
    last_price = trades[-1][1]

    for ts, price, qty, is_buy, delta in trades:
        cvd += delta

    price_change = ((last_price - first_price) / first_price * 100) if first_price > 0 else 0.0
    return cvd, price_change, len(trades)


def detect_divergence(price_change: float, cvd_value: float) -> tuple:
    """
    Detect CVD divergence signals.
    Returns (signal_type, description, direction, strength)
    """
    abs_price = abs(price_change)
    abs_cvd = abs(cvd_value)

    # BULLISH DIVERGENCE: Price down but CVD positive (hidden buying)
    if price_change < -CVD_DIVERGENCE_PRICE_THRESH and cvd_value > CVD_DIVERGENCE_CVD_THRESH:
        strength = min(100, int(abs_cvd / CVD_DIVERGENCE_CVD_THRESH * 30))
        return (
            "BULLISH_DIV",
            f"Price DOWN {price_change:+.4f}% but CVD UP +${cvd_value:,.0f} = accumulation",
            "UP",
            strength,
        )

    # BEARISH DIVERGENCE: Price up but CVD negative (hidden selling)
    if price_change > CVD_DIVERGENCE_PRICE_THRESH and cvd_value < -CVD_DIVERGENCE_CVD_THRESH:
        strength = min(100, int(abs_cvd / CVD_DIVERGENCE_CVD_THRESH * 30))
        return (
            "BEARISH_DIV",
            f"Price UP +{price_change:.4f}% but CVD DOWN -${abs_cvd:,.0f} = distribution",
            "DOWN",
            strength,
        )

    # STRONG BULL: Price up AND CVD up (full momentum)
    if price_change > CVD_STRONG_PRICE_THRESH and cvd_value > CVD_STRONG_CVD_THRESH:
        strength = min(100, int(abs_cvd / CVD_STRONG_CVD_THRESH * 30))
        return (
            "STRONG_BULL",
            f"Price UP +{price_change:.4f}% AND CVD UP +${cvd_value:,.0f} = full momentum UP",
            "UP",
            strength,
        )

    # STRONG BEAR: Price down AND CVD down (full momentum)
    if price_change < -CVD_STRONG_PRICE_THRESH and cvd_value < -CVD_STRONG_CVD_THRESH:
        strength = min(100, int(abs_cvd / CVD_STRONG_CVD_THRESH * 30))
        return (
            "STRONG_BEAR",
            f"Price DOWN {price_change:+.4f}% AND CVD DOWN -${abs_cvd:,.0f} = full momentum DOWN",
            "DOWN",
            strength,
        )

    # Mild alignment (not strong enough to trade)
    if price_change > 0 and cvd_value > 0:
        return ("BULLISH", "BULLISH", "UP", 20)
    if price_change < 0 and cvd_value < 0:
        return ("BEARISH", "BEARISH", "DOWN", 20)

    return ("NEUTRAL", "NEUTRAL", "FLAT", 0)


def calculate_mm_quotes(
    midpoint: float,
    base_spread: float,
    cvd_skew: float,
    inventory: int,
    max_inventory: int,
    order_size: int,
    tick_size: float = 0.01,
) -> dict:
    """
    Calculate market maker bid/ask prices with CVD and inventory skew.

    Args:
        midpoint: Current orderbook midpoint price.
        base_spread: Base spread width (e.g., 0.04 = 4 cents).
        cvd_skew: CVD-derived price shift. Positive = bullish (shift up).
        inventory: Current net position in shares (positive = long UP).
        max_inventory: Maximum allowed net position.
        order_size: Shares per side.
        tick_size: Minimum price increment (default 0.01 for BTC markets).

    Returns:
        {"bid_price": float, "ask_price": float, "bid_size": int, "ask_size": int}
    """
    half_spread = base_spread / 2.0

    # Shift midpoint by CVD signal
    adjusted_mid = midpoint + cvd_skew

    # Inventory skew: penalize the overloaded side
    # Long inventory → lower mid → lower bid/ask → encourage sells, discourage buys
    # Skew is quantized to tick_size so it always produces a visible price shift.
    if max_inventory > 0:
        inv_ratio = inventory / max_inventory  # range: -1 to +1
        # Scale: full inventory → shift by base_spread; quantize to whole ticks
        raw_skew = inv_ratio * base_spread
        inv_skew = round(raw_skew / tick_size) * tick_size
        adjusted_mid -= inv_skew

    # Calculate raw prices
    bid_price = adjusted_mid - half_spread
    ask_price = adjusted_mid + half_spread

    # Round to tick: floor for bid (conservative buy), ceil for ask (conservative sell)
    bid_price = round(math.floor(bid_price / tick_size) * tick_size, 2)
    ask_price = round(math.ceil(ask_price / tick_size) * tick_size, 2)

    # Clamp to valid range [0.01, 0.99] BEFORE enforcing min spread
    bid_price = max(0.01, min(0.99, bid_price))
    ask_price = max(0.01, min(0.99, ask_price))

    # Enforce minimum spread of 1 tick AFTER clamping (push bid down at upper bound)
    if ask_price - bid_price < tick_size:
        if bid_price > 0.01:
            bid_price = round(ask_price - tick_size, 2)
        else:
            ask_price = round(bid_price + tick_size, 2)

    # Size: full size unless at inventory limit
    bid_size = order_size if inventory < max_inventory else 0
    ask_size = order_size if inventory > -max_inventory else 0

    return {
        "bid_price": bid_price,
        "ask_price": ask_price,
        "bid_size": bid_size,
        "ask_size": ask_size,
    }


def check_cvd_signal(feed: BinanceCVDFeed) -> tuple:
    """
    Check CVD across intraday timeframes for a trade signal.
    Fetches trades ONCE from the feed buffer, then slices into time windows.

    Returns: (direction, signal_type, details_str) or (None, None, "")
    """
    signals = []

    # Get all trades for the longest timeframe (15 min)
    all_trades = feed.get_trades_since(max(CVD_SIGNAL_TIMEFRAMES))
    if len(all_trades) < 50:
        print(colored(f"   🌙 Not enough trades ({len(all_trades)}), waiting...", "yellow"))
        return None, None, ""

    print(colored(f"   🌙 Analyzing {len(all_trades)} trades across {len(CVD_SIGNAL_TIMEFRAMES)} timeframes...", "white"))

    # Slice trades into each timeframe window
    now_ms = time.time() * 1000
    trade_data = {}
    for tf_sec in CVD_SIGNAL_TIMEFRAMES:
        cutoff = now_ms - (tf_sec * 1000)
        trade_data[tf_sec] = [(ts, p, q, ib, d) for ts, p, q, ib, d in all_trades if ts >= cutoff]

    # Check all signal timeframes
    for tf_sec in CVD_SIGNAL_TIMEFRAMES:
        trades = trade_data.get(tf_sec, [])
        if len(trades) < 10:
            tf_label = f"{tf_sec // 60}m"
            print(colored(f"   🌙 CVD [{tf_label}] Not enough trades ({len(trades)})", "yellow"))
            continue

        cvd_val, price_chg, count = compute_volume_cvd(trades)
        signal_type, signal_text, direction, strength = detect_divergence(price_chg, cvd_val)

        tf_label = f"{tf_sec // 60}m"
        print(colored(
            f"   🌙 CVD [{tf_label}] Price: {price_chg:+.3f}% | CVD: ${cvd_val:+,.0f} | "
            f"Signal: {signal_text} | Trades: {count}",
            "cyan",
        ))

        # Only trade on divergences and strong momentum
        if signal_type in ("BULLISH_DIV", "BEARISH_DIV", "STRONG_BULL", "STRONG_BEAR"):
            if "DIV" in signal_type:
                if direction == "UP":
                    detail = f"[{tf_label}] Price DOWN ({price_chg:+.3f}%) but buyers aggressive (CVD +${cvd_val:,.0f}) = accumulation"
                else:
                    detail = f"[{tf_label}] Price UP ({price_chg:+.3f}%) but sellers aggressive (CVD -${abs(cvd_val):,.0f}) = distribution"
            else:
                detail = f"[{tf_label}] Strong momentum {direction} (Price {price_chg:+.3f}%, CVD ${cvd_val:+,.0f})"

            # Boost strength for primary TF signals
            if tf_sec == CVD_PRIMARY_TF:
                strength = int(strength * 1.5)

            signals.append((direction, signal_type, detail, strength))

    # Check confirmation timeframes
    confirm_direction = None
    if REQUIRE_CONFIRMATION and signals:
        for tf_sec in CVD_CONFIRM_TIMEFRAMES:
            trades = trade_data.get(tf_sec, [])
            if len(trades) < 10:
                continue
            cvd_val, price_chg, count = compute_volume_cvd(trades)
            signal_type, signal_text, direction, strength = detect_divergence(price_chg, cvd_val)
            tf_label = f"{tf_sec // 60}m"
            print(colored(f"   🌙 CVD [{tf_label}] CONFIRM: Price: {price_chg:+.3f}% | CVD: ${cvd_val:+,.0f} | {signal_text}", "white"))
            if direction in ("UP", "DOWN"):
                confirm_direction = direction

    if not signals:
        return None, None, ""

    # Pick strongest signal
    signals.sort(key=lambda x: x[3], reverse=True)
    best = signals[0]

    # Confirmation gate
    if REQUIRE_CONFIRMATION and confirm_direction:
        if best[0] != confirm_direction:
            print(colored(f"   ⚠️ Best = {best[0]} but confirmation says {confirm_direction}, skipping!", "yellow"))
            return None, None, ""

    return best[0], best[1], best[2]


# ============================================================================
# POLYMARKET FUNCTIONS (replaces nice_funcs)
# ============================================================================

def init_clob_client() -> ClobClient | None:
    """Initialize authenticated Polymarket CLOB client."""
    if PAPER_MODE:
        print(colored("   📝 PAPER MODE — skipping Polymarket CLOB init", "yellow"))
        return None

    key = os.getenv("PRIVATE_KEY")
    browser_address = os.getenv("PUBLIC_KEY")
    api_key = os.getenv("API_KEY")
    api_secret = os.getenv("SECRET")
    api_passphrase = os.getenv("PASSPHRASE")

    if not key or not browser_address:
        print(colored("❌ Missing PRIVATE_KEY or PUBLIC_KEY in .env!", "red"))
        sys.exit(1)

    try:
        from web3 import Web3
        browser_wallet = Web3.to_checksum_address(browser_address)
    except AttributeError:
        from web3 import Web3
        browser_wallet = Web3.toChecksumAddress(browser_address)

    client = ClobClient(
        host=CLOB_HOST,
        key=key,
        chain_id=POLYGON,
        funder=browser_wallet,
        signature_type=1,
    )

    if api_key and api_secret and api_passphrase:
        creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        client.set_api_creds(creds=creds)
    else:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds=creds)

    print(colored("✅ Polymarket CLOB client initialized!", "green"))
    return client


# Global CLOB client (initialized in main)
clob_client: ClobClient | None = None


def place_limit_order(token_id: str, side: str, price: float, size: int, neg_risk: bool = False) -> dict:
    """Place a limit order on Polymarket CLOB."""
    if PAPER_MODE:
        fake_id = f"PAPER-{int(time.time() * 1000)}"
        print(colored(f"   📝 PAPER ORDER: {side} {size} shares @ ${price:.4f} | Token: {str(token_id)[:20]}...", "yellow"))
        return {"orderID": fake_id, "paper": True}

    if clob_client is None:
        print(colored("❌ CLOB client not initialized!", "red"))
        return {}

    order_args = OrderArgs(
        token_id=str(token_id),
        price=round(price, 4),
        size=size,
        side=side.upper(),
        fee_rate_bps=1000,
    )

    print(colored(f"   📋 Placing {side} limit order...", "cyan"))
    print(colored(f"      Token: {str(token_id)[:20]}...", "white"))
    print(colored(f"      Price: ${price:.4f} | Size: {size} shares | Neg Risk: {neg_risk}", "white"))

    try:
        if neg_risk:
            from py_clob_client.clob_types import PartialCreateOrderOptions
            signed_order = clob_client.create_order(order_args, options=PartialCreateOrderOptions(neg_risk=True))
        else:
            signed_order = clob_client.create_order(order_args)

        response = clob_client.post_order(signed_order)

        if response and isinstance(response, dict) and response.get("orderID"):
            print(colored(f"   🟢 Order placed! ID: {response['orderID'][:20]}...", "green"))
            return response
        else:
            print(colored(f"   ❌ Order failed: {response}", "red"))
            return response if isinstance(response, dict) else {}
    except Exception as e:
        print(colored(f"   ❌ Order exception: {e}", "red"))
        return {}


def cancel_token_orders(token_id: str):
    """Cancel all open orders for a specific token."""
    if PAPER_MODE:
        print(colored(f"   📝 PAPER: Cancel orders (simulated)", "yellow"))
        return

    if clob_client is None:
        return
    try:
        open_orders = clob_client.get_orders()
        if not open_orders:
            return
        for order in open_orders:
            asset_id = order.get("asset_id", "")
            if asset_id == token_id:
                order_id = order.get("id", "")
                if order_id:
                    clob_client.cancel(order_id)
                    print(colored(f"   🗑️ Cancelled order {order_id[:16]}...", "yellow"))
    except Exception as e:
        print(colored(f"   ⚠️ Cancel orders error: {e}", "yellow"))


def get_all_positions() -> dict:
    """Get all open positions from Polymarket."""
    if clob_client is None:
        return {}
    try:
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        positions = clob_client.get_orders()
        sys.stdout = old_stdout
        return {"positions": positions if positions else []}
    except Exception:
        sys.stdout = sys.__stdout__
        return {}


def check_poly_filled(token_id: str) -> bool:
    """Check if we have a filled position for this token."""
    if PAPER_MODE:
        return True  # Simulate instant fill in paper mode

    try:
        open_orders = clob_client.get_orders() if clob_client else []
        if not open_orders:
            return False
        # If our order for this token is no longer in open orders, it was filled
        for order in open_orders:
            if order.get("asset_id") == token_id and order.get("status") == "LIVE":
                return False  # Still open, not filled yet
        return True  # No open order found → was filled or cancelled
    except Exception:
        return False


def get_token_id(market_id: str) -> tuple:
    """
    Get token IDs for a market from Gamma API.
    Returns (condition_id, up_token_id, down_token_id) or empty tuple on failure.
    """
    try:
        url = f"{GAMMA_API_URL}/markets"
        params = {"id": market_id}
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            return ()

        markets = resp.json()
        if not markets:
            return ()

        market = markets[0] if isinstance(markets, list) else markets

        condition_id = market.get("conditionId", "")
        tokens_raw = market.get("clobTokenIds", "[]")
        outcomes_raw = market.get("outcomes", "[]")

        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw

        if len(tokens) < 2 or len(outcomes) < 2:
            return ()

        up_idx = -1
        down_idx = -1
        for i, outcome in enumerate(outcomes):
            if outcome.lower() in ("up", "yes"):
                up_idx = i
            elif outcome.lower() in ("down", "no"):
                down_idx = i

        if up_idx == -1 or down_idx == -1:
            return ()

        return (condition_id, tokens[up_idx], tokens[down_idx])
    except Exception as e:
        print(colored(f"   ⚠️ get_token_id error: {e}", "yellow"))
        return ()


# ============================================================================
# MARKET DISCOVERY (find active BTC 5-min markets)
# ============================================================================

def get_current_market_timestamp() -> int:
    """Get the epoch timestamp for the current active 5-minute market."""
    now = int(time.time())
    return (now // MARKET_DURATION) * MARKET_DURATION


def get_time_remaining(market_ts: int) -> int:
    """Seconds remaining in current market."""
    now = int(time.time())
    elapsed = now - market_ts
    return MARKET_DURATION - elapsed


def get_market_info(market_ts: int) -> dict | None:
    """
    Get market info for a BTC 5-min market.
    Searches by slug with fallback offsets (±300s).
    Returns dict with market_id, up/down token IDs, etc.
    """
    offsets = [0, 300, -300]

    for offset in offsets:
        target_ts = market_ts + offset
        market_slug = f"btc-updown-5m-{target_ts}"

        try:
            url = f"{GAMMA_API_URL}/events"
            params = {"slug": market_slug}
            resp = requests.get(url, params=params, timeout=10)

            if resp.status_code != 200:
                continue

            events = resp.json()
            if not events:
                continue

            event = events[0]
            markets = event.get("markets", [])
            if not markets:
                continue

            market = markets[0]
            market_id = market.get("id", "")

            # Parse token IDs and outcomes
            tokens_raw = market.get("clobTokenIds", "[]")
            outcomes_raw = market.get("outcomes", "[]")
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw

            if len(tokens) < 2 or len(outcomes) < 2:
                continue

            up_idx = -1
            down_idx = -1
            for i, outcome in enumerate(outcomes):
                if outcome.lower() in ("up", "yes"):
                    up_idx = i
                elif outcome.lower() in ("down", "no"):
                    down_idx = i

            if up_idx == -1 or down_idx == -1:
                continue

            neg_risk = market.get("negRisk", False)
            question = event.get("title", market.get("question", market_slug))

            print(colored(f"   ✅ Found market: {question}", "green"))

            return {
                "market_id": market_id,
                "up_token_id": tokens[up_idx],
                "down_token_id": tokens[down_idx],
                "question": question,
                "slug": market_slug,
                "neg_risk": neg_risk,
            }
        except Exception as e:
            print(colored(f"   ⚠️ Market discovery error (offset={offset}): {e}", "yellow"))
            continue

    return None


# ============================================================================
# ORDER BOOK
# ============================================================================

def get_order_book(token_id: str) -> dict | None:
    """Get best bid/ask from CLOB order book."""
    try:
        url = f"{CLOB_HOST}/book"
        params = {"token_id": token_id}
        resp = requests.get(url, params=params, timeout=10)

        if resp.status_code != 200:
            return None

        data = resp.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        if not bids or not asks:
            return None

        best_bid = float(bids[-1]["price"])
        best_ask = float(asks[0]["price"])

        return {"best_bid": best_bid, "best_ask": best_ask, "spread": best_ask - best_bid}
    except Exception:
        return None


# ============================================================================
# TRADE LOGGING (CSV with pandas)
# ============================================================================

def log_trade(signal_type: str, direction: str, cvd_detail: str,
              poly_price: float, stink_price: float, poly_shares: int,
              hedge_side: str, hedge_size: float, hedge_price: float,
              result: str, reason: str = ""):
    """Log trade to CSV file."""
    os.makedirs(DATA_DIR, exist_ok=True)

    poly_usd = round(stink_price * poly_shares, 4) if stink_price and poly_shares else 0
    hedge_usd = round(hedge_size * hedge_price, 4) if hedge_size and hedge_price else 0

    new_row = pd.DataFrame([{
        "timestamp": datetime.now().isoformat(),
        "signal_type": signal_type,
        "direction": direction,
        "cvd_detail": cvd_detail,
        "signal_price": round(poly_price, 4) if poly_price else 0,
        "stink_bid_price": round(stink_price, 4) if stink_price else 0,
        "pullback_pct": PULLBACK_PCT,
        "poly_shares": poly_shares if poly_shares else 0,
        "poly_usd": poly_usd,
        "hedge_side": hedge_side,
        "hedge_size_btc": round(hedge_size, 6) if hedge_size else 0,
        "hedge_price": round(hedge_price, 4) if hedge_price else 0,
        "hedge_usd": hedge_usd,
        "result": result,
        "reason": reason,
    }])

    if os.path.exists(TRADE_LOG_FILE):
        existing = pd.read_csv(TRADE_LOG_FILE)
        df = pd.concat([existing, new_row], ignore_index=True)
    else:
        df = new_row

    df.to_csv(TRADE_LOG_FILE, index=False)
    print(colored("   📝 Trade logged to CSV", "green"))


def print_trade_summary():
    """Print summary of all tracked trades."""
    if not os.path.exists(TRADE_LOG_FILE):
        print(colored("   📊 No trade history yet", "yellow"))
        return

    df = pd.read_csv(TRADE_LOG_FILE)
    if len(df) == 0:
        print(colored("   📊 No trades in log", "yellow"))
        return

    total = len(df)
    both_filled = len(df[df["result"] == "BOTH_FILLED"])
    poly_only = len(df[df["result"] == "POLY_ONLY"])
    expired = len(df[df["result"] == "TIME_EXPIRED"])
    cancelled = len(df[df["result"] == "CANCELLED"])

    print(colored(f"\n   📊 Trade Summary:", "cyan"))
    print(colored(f"      Total: {total} | Both filled: {both_filled} | Poly only: {poly_only}", "white"))
    print(colored(f"      Expired: {expired} | Cancelled: {cancelled}", "white"))

    if total > 0:
        print(colored(f"\n      Last 5 trades:", "cyan"))
        recent = df.tail(5)
        for _, row in recent.iterrows():
            ts = str(row["timestamp"])[:19]
            print(colored(
                f"      {ts} | {row['signal_type']:<14} | {row['direction']:<5} | "
                f"stink ${row['stink_bid_price']:.3f} | {row['result']}",
                "white",
            ))
        print()


# ============================================================================
# PAPER TRADING LOGGING + OUTCOME CHECKER
# ============================================================================

def log_paper_signal(market_ts: int, signal_type: str, direction: str, detail: str,
                     stink_price: float, btc_price_at_signal: float, market_slug: str):
    """Log paper trade signal for later outcome resolution."""
    os.makedirs(DATA_DIR, exist_ok=True)

    new_row = pd.DataFrame([{
        "timestamp": datetime.now().isoformat(),
        "market_ts": market_ts,
        "market_slug": market_slug,
        "signal_type": signal_type,
        "direction": direction,
        "detail": detail,
        "stink_price": round(stink_price, 4),
        "btc_price_at_signal": round(btc_price_at_signal, 2),
        "btc_price_at_close": 0.0,
        "market_outcome": "",
        "signal_correct": "",
        "price_change_pct": 0.0,
    }])

    if os.path.exists(PAPER_LOG_FILE):
        existing = pd.read_csv(PAPER_LOG_FILE)
        df = pd.concat([existing, new_row], ignore_index=True)
    else:
        df = new_row

    df.to_csv(PAPER_LOG_FILE, index=False)
    print(colored("   📝 Paper signal logged", "green"))


def resolve_paper_outcome(market_ts: int, feed: BinanceCVDFeed):
    """
    After a market cycle ends, check BTC price now vs at signal time.
    Update the paper log with the actual outcome.
    """
    if not os.path.exists(PAPER_LOG_FILE):
        return

    df = pd.read_csv(PAPER_LOG_FILE)
    df["market_outcome"] = df["market_outcome"].fillna("")
    df["signal_correct"] = df["signal_correct"].fillna("")
    mask = (df["market_ts"] == market_ts) & (df["market_outcome"] == "")
    if not mask.any():
        return

    btc_now = feed.get_last_price()
    if btc_now <= 0:
        return

    for idx in df[mask].index:
        btc_at_signal = df.loc[idx, "btc_price_at_signal"]
        if btc_at_signal <= 0:
            continue

        price_change = ((btc_now - btc_at_signal) / btc_at_signal) * 100
        actual_outcome = "UP" if btc_now > btc_at_signal else "DOWN"
        predicted = df.loc[idx, "direction"]
        correct = "YES" if predicted == actual_outcome else "NO"

        df.loc[idx, "btc_price_at_close"] = round(btc_now, 2)
        df.loc[idx, "market_outcome"] = actual_outcome
        df.loc[idx, "signal_correct"] = correct
        df.loc[idx, "price_change_pct"] = round(price_change, 4)

        emoji = "✅" if correct == "YES" else "❌"
        print(colored(
            f"   {emoji} PAPER RESULT: Predicted {predicted} | Actual {actual_outcome} | "
            f"BTC {btc_at_signal:,.1f} → {btc_now:,.1f} ({price_change:+.3f}%)",
            "green" if correct == "YES" else "red",
        ))

    df.to_csv(PAPER_LOG_FILE, index=False)


def print_paper_summary():
    """Print accuracy stats from paper trading log."""
    if not os.path.exists(PAPER_LOG_FILE):
        print(colored("   📝 No paper trades yet", "yellow"))
        return

    df = pd.read_csv(PAPER_LOG_FILE)
    resolved = df[df["signal_correct"].isin(["YES", "NO"])]
    if len(resolved) == 0:
        print(colored(f"   📝 {len(df)} paper signals logged, none resolved yet", "yellow"))
        return

    total = len(resolved)
    correct = len(resolved[resolved["signal_correct"] == "YES"])
    accuracy = (correct / total) * 100

    print(colored(f"\n   📝 Paper Trading Summary:", "cyan", attrs=["bold"]))
    print(colored(f"      Signals: {total} | Correct: {correct} | Wrong: {total - correct}", "white"))
    print(colored(f"      Accuracy: {accuracy:.1f}%", "green" if accuracy > 50 else "red", attrs=["bold"]))

    # Breakdown by signal type
    for sig_type in resolved["signal_type"].unique():
        subset = resolved[resolved["signal_type"] == sig_type]
        sub_correct = len(subset[subset["signal_correct"] == "YES"])
        sub_total = len(subset)
        sub_acc = (sub_correct / sub_total) * 100 if sub_total > 0 else 0
        print(colored(f"      {sig_type:<14}: {sub_correct}/{sub_total} ({sub_acc:.0f}%)", "white"))

    # Average price change for correct vs wrong
    correct_df = resolved[resolved["signal_correct"] == "YES"]
    wrong_df = resolved[resolved["signal_correct"] == "NO"]
    if len(correct_df) > 0:
        avg_correct = correct_df["price_change_pct"].abs().mean()
        print(colored(f"      Avg move (correct): {avg_correct:.3f}%", "white"))
    if len(wrong_df) > 0:
        avg_wrong = wrong_df["price_change_pct"].abs().mean()
        print(colored(f"      Avg move (wrong):   {avg_wrong:.3f}%", "white"))


# ============================================================================
# MARKET MAKER — INVENTORY TRACKER
# ============================================================================

class MMInventory:
    """Track market maker inventory, fills, and P&L."""

    def __init__(self):
        self.net_position: int = 0
        self.cash: float = 0.0
        self.fills: list = []

    def reset_cycle(self):
        """Reset for a new market cycle."""
        self.net_position = 0
        self.cash = 0.0
        self.fills = []

    def record_fill(self, side: str, price: float, size: int):
        """Record a fill. BUY increases position, SELL decreases it."""
        if side == "BUY":
            self.net_position += size
            self.cash -= price * size
        elif side == "SELL":
            self.net_position -= size
            self.cash += price * size

        self.fills.append({
            "timestamp": time.time(),
            "side": side,
            "price": price,
            "size": size,
            "net_position_after": self.net_position,
        })

    def get_pnl(self, mark_price: float) -> float:
        """P&L = cash + (net_position × mark_price)."""
        return self.cash + (self.net_position * mark_price)

    def get_fill_count(self) -> tuple:
        """Returns (total, buys, sells)."""
        buys = sum(1 for f in self.fills if f["side"] == "BUY")
        sells = sum(1 for f in self.fills if f["side"] == "SELL")
        return len(self.fills), buys, sells


# ============================================================================
# HYPERLIQUID HEDGE FUNCTIONS
# ============================================================================

def hl_get_position() -> dict | None:
    """Get current BTC position on Hyperliquid."""
    if not hl_info or not hl_address:
        return None
    try:
        user_state = hl_info.user_state(hl_address)
        for asset_pos in user_state.get("assetPositions", []):
            position = asset_pos.get("position", {})
            if position.get("coin") == HEDGE_SYMBOL:
                szi = float(position.get("szi", "0"))
                if abs(szi) > 0:
                    return {
                        "size": szi,
                        "entry_price": float(position.get("entryPx", "0")),
                        "unrealized_pnl": float(position.get("unrealizedPnl", "0")),
                        "is_long": szi > 0,
                    }
        return None
    except Exception as e:
        print(colored(f"   ⚠️ HL get_position error: {e}", "yellow"))
        return None


def hl_ask_bid() -> tuple:
    """Get best ask and bid for BTC on Hyperliquid."""
    if not hl_info:
        return 0.0, 0.0
    try:
        l2 = hl_info.l2_snapshot(HEDGE_SYMBOL)
        levels = l2.get("levels", [[], []])
        bid = float(levels[0][0]["px"]) if levels[0] else 0.0
        ask = float(levels[1][0]["px"]) if levels[1] else 0.0
        return ask, bid
    except Exception as e:
        print(colored(f"   ⚠️ HL ask_bid error: {e}", "yellow"))
        return 0.0, 0.0


def hl_sz_decimals() -> int:
    """Get size decimals for BTC on Hyperliquid."""
    if not hl_info:
        return 5
    try:
        meta = hl_info.meta()
        for asset in meta.get("universe", []):
            if asset.get("name") == HEDGE_SYMBOL:
                return asset.get("szDecimals", 5)
        return 5
    except Exception:
        return 5


def hl_cancel_all():
    """Cancel all open Hyperliquid orders."""
    if not hl_exchange:
        return
    try:
        open_orders = hl_info.open_orders(hl_address) if hl_info and hl_address else []
        for order in open_orders:
            hl_exchange.cancel(order["coin"], order["oid"])
    except Exception:
        pass


def fill_hyperliquid_hedge(poly_outcome: str) -> tuple:
    """
    Place hedge on Hyperliquid.
    If Polymarket outcome is "UP" → SHORT BTC on HL
    If Polymarket outcome is "DOWN" → LONG BTC on HL
    Returns (success, hedge_side, hedge_size_btc, hedge_price)
    """
    if PAPER_MODE:
        if poly_outcome.upper() == "UP":
            hedge_side = "SHORT"
        else:
            hedge_side = "LONG"
        btc_price = 0.0
        try:
            ask, bid = hl_ask_bid()
            btc_price = (ask + bid) / 2 if ask > 0 else 0.0
        except Exception:
            pass
        btc_size = HEDGE_USD * HEDGE_LEVERAGE / btc_price if btc_price > 0 else 0.0
        print(colored(f"   📝 PAPER HEDGE: {hedge_side} {btc_size:.6f} BTC @ ${btc_price:,.1f}", "yellow"))
        return True, hedge_side, btc_size, btc_price

    if not HEDGE_ENABLED or not hl_exchange:
        return False, "DISABLED", 0, 0

    if poly_outcome.upper() == "UP":
        is_buy = False
        hedge_side = "SHORT"
    elif poly_outcome.upper() == "DOWN":
        is_buy = True
        hedge_side = "LONG"
    else:
        print(colored(f"   ❌ Unknown outcome '{poly_outcome}'", "red"))
        return False, "UNKNOWN", 0, 0

    print(colored(
        f"\n   🔷 HEDGE LEG: {hedge_side} ${HEDGE_USD:.2f} BTC @ {HEDGE_LEVERAGE}x on Hyperliquid",
        "magenta", attrs=["bold"],
    ))

    # Cancel existing orders and check for existing position
    hl_cancel_all()
    time.sleep(0.5)

    existing = hl_get_position()
    if existing and abs(existing["size"]) > 0:
        actual_side = "LONG" if existing["is_long"] else "SHORT"
        print(colored(
            f"   ⚠️ Already have HL position: {actual_side} {abs(existing['size'])} BTC - skipping",
            "yellow",
        ))
        return True, actual_side, abs(existing["size"]), existing["entry_price"]

    # Set leverage
    try:
        hl_exchange.update_leverage(HEDGE_LEVERAGE, HEDGE_SYMBOL, is_cross=False)
        print(colored(f"   🔧 Leverage set to {HEDGE_LEVERAGE}x isolated", "magenta"))
    except Exception as e:
        print(colored(f"   ⚠️ Leverage update error: {e}", "yellow"))

    # Get prices and calculate size
    ask, bid = hl_ask_bid()
    if ask == 0 or bid == 0:
        print(colored("   ❌ Could not get HL prices", "red"))
        return False, hedge_side, 0, 0

    mid_price = (ask + bid) / 2
    btc_size = HEDGE_USD * HEDGE_LEVERAGE / mid_price

    sz_dec = hl_sz_decimals()
    factor = 10 ** sz_dec
    btc_size = math.ceil(btc_size * factor) / factor

    if btc_size == 0:
        print(colored("   ⚠️ Size too small to hedge", "yellow"))
        return False, hedge_side, 0, 0

    # Market order: use ask for buys, bid for sells (cross spread for instant fill)
    if is_buy:
        market_px = round(ask * 1.001, 1)  # slight slippage buffer
    else:
        market_px = round(bid * 0.999, 1)

    print(colored(f"   🔷 MARKET {hedge_side} {btc_size} BTC @ ${market_px:.1f}", "magenta"))

    try:
        result = hl_exchange.order(
            HEDGE_SYMBOL, is_buy, btc_size, market_px,
            {"limit": {"tif": "Gtc"}},
        )

        if result and "response" in result:
            statuses = result["response"].get("data", {}).get("statuses", [])
            if statuses:
                status = statuses[0]
                if "error" in status:
                    print(colored(f"   ❌ HL order error: {status['error']}", "red"))
                    return False, hedge_side, 0, 0
    except Exception as e:
        print(colored(f"   ❌ HL order exception: {e}", "red"))
        return False, hedge_side, 0, 0

    time.sleep(1)
    pos = hl_get_position()
    if pos and abs(pos["size"]) > 0:
        actual_side = "LONG" if pos["is_long"] else "SHORT"
        print(colored(
            f"   ✅ HEDGE FILLED! {actual_side} {abs(pos['size'])} BTC @ ${pos['entry_price']:.1f}",
            "green", attrs=["bold"],
        ))
        return True, actual_side, abs(pos["size"]), pos["entry_price"]

    hl_cancel_all()
    print(colored("   ⚠️ Hedge not filled, cancelled.", "yellow"))
    return False, hedge_side, 0, 0


def close_hyperliquid_hedge():
    """Close any open Hyperliquid hedge with IOC order. Up to 5 attempts."""
    if PAPER_MODE:
        print(colored("   📝 PAPER HEDGE CLOSE (simulated)", "yellow"))
        return

    if not HEDGE_ENABLED or not hl_exchange:
        return

    for attempt in range(5):
        pos = hl_get_position()
        if not pos or abs(pos["size"]) == 0:
            print(colored("   ✅ HEDGE CLOSED!", "green", attrs=["bold"]))
            return

        is_buy = not pos["is_long"]
        close_size = abs(pos["size"])

        hl_cancel_all()
        time.sleep(0.3)

        ask, bid = hl_ask_bid()
        if is_buy:
            close_px = round(ask + 50.0, 0)
        else:
            close_px = round(bid - 50.0, 0)

        side_str = "LONG" if pos["is_long"] else "SHORT"
        close_side = "BUY" if is_buy else "SELL"
        print(colored(
            f"\n   🔷 CLOSING HEDGE (attempt {attempt + 1}): {close_side} {close_size} BTC @ ${close_px:.0f} IOC (was {side_str})",
            "magenta", attrs=["bold"],
        ))

        try:
            result = hl_exchange.order(
                HEDGE_SYMBOL, is_buy, close_size, close_px,
                {"limit": {"tif": "Ioc"}},
                reduce_only=True,
            )
            if result and "response" in result:
                statuses = result["response"].get("data", {}).get("statuses", [])
                if statuses and "filled" in statuses[0]:
                    print(colored("   ✅ HEDGE CLOSED!", "green", attrs=["bold"]))
                    return
        except Exception as e:
            print(colored(f"   ⚠️ Close hedge error: {e}", "yellow"))

        time.sleep(0.5)

    print(colored("   ❌ Could not close hedge after 5 attempts!", "red"))


# ============================================================================
# CVD STINK BOT CLASS
# ============================================================================

class CVDStinkBot:
    def __init__(self, feed: BinanceCVDFeed):
        self.feed = feed
        self.current_market_ts: int | None = None
        self.market_info: dict | None = None
        self.signal_fired = False
        self.signal_type: str | None = None
        self.signal_direction: str | None = None
        self.signal_detail = ""
        self.target_outcome: str | None = None
        self.target_token_id: str | None = None
        self.signal_price = 0.0
        self.stink_bid_price = 0.0
        self.stink_bid_shares = 0
        self.order_placed = False
        self.poly_filled = False
        self.hedge_filled = False

    def reset(self):
        """Reset state for next market cycle."""
        self.current_market_ts = None
        self.market_info = None
        self.signal_fired = False
        self.signal_type = None
        self.signal_direction = None
        self.signal_detail = ""
        self.target_outcome = None
        self.target_token_id = None
        self.signal_price = 0.0
        self.stink_bid_price = 0.0
        self.stink_bid_shares = 0
        self.order_placed = False
        self.poly_filled = False
        self.hedge_filled = False

    def place_stink_bid(self) -> bool:
        """Place stink bid at PULLBACK_PCT below current ask."""
        token_id = self.target_token_id
        outcome = self.target_outcome

        book = get_order_book(token_id)
        if not book:
            print(colored(f"   ⚠️ No order book for {outcome}, market may be closed", "yellow"))
            return False

        best_ask = book["best_ask"]
        market_slug = self.market_info.get("slug", "") if self.market_info else ""
        print(colored(f"\n   🔗 Market: https://polymarket.com/event/{market_slug}", "cyan", attrs=["bold"]))
        print(colored(f"      Best Ask: ${best_ask:.4f} | Spread: ${book['spread']:.4f}", "white"))

        # Calculate stink bid price
        stink_bid_price = round(best_ask * (1 - PULLBACK_PCT), 2)
        if self.signal_price > 0:
            stink_bid_price = round(self.signal_price * (1 - PULLBACK_PCT), 2)

        # Cap stink bid price
        if stink_bid_price > MAX_STINK_BID_PRICE:
            stink_bid_price = MAX_STINK_BID_PRICE

        if stink_bid_price <= 0.01:
            stink_bid_price = 0.01

        # Calculate shares
        stink_bid_shares = round(ORDER_SIZE_USD / stink_bid_price)

        print(colored(
            f"\n   🎯 CVD STINK BID: {outcome} | {stink_bid_shares} shares @ ${stink_bid_price:.4f}",
            "green", attrs=["bold"],
        ))

        self.stink_bid_price = stink_bid_price
        self.stink_bid_shares = stink_bid_shares
        self.signal_price = best_ask

        neg_risk = self.market_info.get("neg_risk", False) if self.market_info else False

        response = place_limit_order(
            token_id=token_id,
            side="BUY",
            price=self.stink_bid_price,
            size=self.stink_bid_shares,
            neg_risk=neg_risk,
        )

        if response and response.get("orderID"):
            print(colored("   ✅ CVD stink bid placed! Waiting for pullback fill...", "green"))
            self.order_placed = True
            return True
        else:
            print(colored("   ❌ Stink bid order rejected", "red"))
            return False

    def check_for_fill(self) -> bool:
        """Check if stink bid was filled."""
        if self.poly_filled:
            return True
        if not self.target_token_id:
            return False

        if check_poly_filled(self.target_token_id):
            print(colored(
                f"\n   🎉 CVD STINK BID FILLED! {self.target_outcome} @ ${self.stink_bid_price:.4f} ✅",
                "green", attrs=["bold"],
            ))
            self.poly_filled = True
            return True
        return False

    def cancel_orders(self):
        """Cancel unfilled stink bids."""
        if self.target_token_id and self.order_placed and not self.poly_filled:
            print(colored("   🗑️ Cancelling unfilled CVD stink bid", "yellow"))
            cancel_token_orders(self.target_token_id)

    def run_market_cycle(self, market_ts: int):
        """
        Run one complete 5-minute market cycle.
        Flow:
          1. Find the market
          2. Poll CVD for divergence/momentum signal
          3. On signal → place stink bid at pullback price
          4. Monitor for fill
          5. On fill → hedge on Hyperliquid
          6. Cancel if time runs out
        """
        self.current_market_ts = market_ts
        market_dt = datetime.fromtimestamp(market_ts, tz=timezone.utc)
        market_et = datetime.fromtimestamp(market_ts, tz=ET)

        print(colored(f"\n{'=' * 70}", "cyan"))
        print(colored("🌙 CVD 5-MIN MARKET CYCLE", "cyan", attrs=["bold"]))
        print(colored(
            f"   Market time: {market_et.strftime('%I:%M:%S%p ET')} | {market_dt.strftime('%H:%M:%S UTC')}",
            "white",
        ))
        print(colored(f"{'=' * 70}", "cyan"))

        # Wait briefly for market to be indexed
        time_remaining = get_time_remaining(market_ts)
        if time_remaining > MARKET_DURATION - 10:
            print(colored("   ⏳ Waiting 10s for market index...", "yellow"))
            time.sleep(10)

        # Find market (up to 5 retries)
        self.market_info = None
        for attempt in range(5):
            self.market_info = get_market_info(market_ts)
            if self.market_info:
                break
            print(colored(f"   🔄 Retry {attempt + 1}/5 in 2s...", "yellow"))
            time.sleep(2)

        if not self.market_info:
            print(colored("   ❌ Could not find market, skipping cycle", "red"))
            return

        market_slug = self.market_info.get("slug", "")
        print(colored(f"   🔗 Market: https://polymarket.com/event/{market_slug}", "cyan", attrs=["bold"]))

        # Main polling loop
        while True:
            time_remaining = get_time_remaining(market_ts)

            # Market ended
            if time_remaining <= 0:
                print(colored(f"\n   🔴 Market ended!", "yellow"))
                self.cancel_orders()
                if self.hedge_filled:
                    print(colored("\n   🔷 Closing Hyperliquid hedge (market over)...", "magenta", attrs=["bold"]))
                    close_hyperliquid_hedge()
                if self.order_placed and not self.poly_filled:
                    log_trade(
                        self.signal_type or "NONE", self.target_outcome or "NONE",
                        self.signal_detail, self.signal_price, self.stink_bid_price,
                        self.stink_bid_shares, "", 0, 0, "TIME_EXPIRED",
                        "Market ended before fill",
                    )
                # Resolve paper outcome
                if PAPER_MODE and self.signal_fired:
                    resolve_paper_outcome(market_ts, self.feed)
                break

            # Too little time left, cancel stink bid
            if time_remaining < MIN_TIME_LEFT and self.order_placed and not self.poly_filled:
                print(colored(f"\n   ⏳ < {MIN_TIME_LEFT}s left, canceling stink bid", "yellow"))
                self.cancel_orders()
                log_trade(
                    self.signal_type or "NONE", self.target_outcome or "NONE",
                    self.signal_detail, self.signal_price, self.stink_bid_price,
                    self.stink_bid_shares, "", 0, 0, "CANCELLED",
                    f"< {MIN_TIME_LEFT}s remaining",
                )
                break

            # Already filled, holding to expiry
            if self.poly_filled:
                mins = time_remaining // 60
                secs = time_remaining % 60
                emoji = "🟢" if self.target_outcome == "UP" else "🔴"
                hedged = "HEDGED" if self.hedge_filled else "UNHEDGED"
                print(colored(
                    f"\r   {emoji} Holding {self.target_outcome} ({hedged}) to expiry... "
                    f"{mins}:{secs:02d} remaining   ",
                    "white",
                ), end="", flush=True)
                time.sleep(2)
                continue

            # Check for fill if order is placed
            if self.order_placed and not self.poly_filled:
                if self.check_for_fill():
                    # FILLED! Now hedge on Hyperliquid
                    print(colored("\n   🔷 LEG 2: HYPERLIQUID HEDGE", "magenta", attrs=["bold"]))
                    hedge_ok, hedge_side, hedge_size, hedge_price = fill_hyperliquid_hedge(
                        self.target_outcome,
                    )

                    if hedge_ok:
                        result = "BOTH_FILLED"
                        self.hedge_filled = True
                        print(colored(
                            f"   ✅ CVD STAT ARB COMPLETE! Poly ({self.target_outcome}) + HL ({hedge_side}) 🟢",
                            "green", attrs=["bold"],
                        ))
                    else:
                        result = "POLY_ONLY"
                        print(colored(
                            "\n   ⚠️ Poly filled but hedge FAILED - unhedged!",
                            "yellow", attrs=["bold"],
                        ))

                    log_trade(
                        self.signal_type, self.target_outcome, self.signal_detail,
                        self.signal_price, self.stink_bid_price, self.stink_bid_shares,
                        hedge_side if hedge_ok else "", hedge_size, hedge_price, result,
                    )
                    continue

                # Print waiting status
                mins = time_remaining // 60
                secs = time_remaining % 60
                print(colored(
                    f"\r   ⏳ CVD stink bid @ ${self.stink_bid_price:.4f} waiting for pullback... "
                    f"{mins}:{secs:02d} left   ",
                    "white",
                ), end="", flush=True)
                time.sleep(2)
                continue

            # No signal yet → scan CVD
            if not self.signal_fired and time_remaining > MIN_TIME_LEFT:
                print(colored(f"\n   🔍 Scanning CVD...", "yellow"))
                direction, signal_type, detail = check_cvd_signal(self.feed)

                if direction:
                    self.signal_fired = True
                    self.signal_type = signal_type
                    self.signal_direction = direction
                    self.signal_detail = detail

                    if direction == "UP":
                        self.target_outcome = "UP"
                        self.target_token_id = self.market_info["up_token_id"]
                        print(colored(
                            f"\n   🔥🔥🔥 CVD SIGNAL: {signal_type} - Buying UP 🔥🔥🔥",
                            "green", attrs=["bold"],
                        ))
                    else:
                        self.target_outcome = "DOWN"
                        self.target_token_id = self.market_info["down_token_id"]
                        print(colored(
                            f"\n   🔥🔥🔥 CVD SIGNAL: {signal_type} - Buying DOWN 🔥🔥🔥",
                            "red", attrs=["bold"],
                        ))

                    print(colored(f"   📋 {detail}", "magenta"))

                    # Place the stink bid
                    self.place_stink_bid()

                    # Log paper signal for outcome tracking
                    if PAPER_MODE:
                        log_paper_signal(
                            market_ts=market_ts,
                            signal_type=self.signal_type,
                            direction=self.signal_direction,
                            detail=self.signal_detail,
                            stink_price=self.stink_bid_price,
                            btc_price_at_signal=self.feed.get_last_price(),
                            market_slug=self.market_info.get("slug", ""),
                        )
                else:
                    mins = time_remaining // 60
                    secs = time_remaining % 60
                    btc_price = self.feed.get_last_price()
                    trade_count = self.feed.get_trade_count()
                    print(colored(
                        f"   🚫 No CVD signal. BTC ${btc_price:,.1f} | "
                        f"Trades: {trade_count} | {mins}:{secs:02d} remaining",
                        "white",
                    ))

            time.sleep(BOT_POLL_INTERVAL)


# ============================================================================
# MAIN ENTRY
# ============================================================================

def main():
    global clob_client

    print(colored(f"""
╔══════════════════════════════════════════════════════════╗
║  🌙 CVD 5-Min BTC Bot v2.0  (Binance CVD Upgrade)  🌙  ║
║  ▸ Polymarket CVD Statistical Arbitrage                  ║
║  ▸ Hyperliquid Delta Hedge                               ║
║  ▸ BTC 5-Min Markets | Volume-Weighted CVD | Stink Bids  ║
╚══════════════════════════════════════════════════════════╝
    """, "cyan", attrs=["bold"]))

    print(colored("   🔧 Bot Configuration:", "yellow", attrs=["bold"]))
    print(colored(f"      CVD_DIVERGENCE_PRICE_THRESH = {CVD_DIVERGENCE_PRICE_THRESH}", "white"))
    print(colored(f"      CVD_DIVERGENCE_CVD_THRESH   = ${CVD_DIVERGENCE_CVD_THRESH}", "white"))
    print(colored(f"      CVD_STRONG_PRICE_THRESH     = {CVD_STRONG_PRICE_THRESH}", "white"))
    print(colored(f"      CVD_STRONG_CVD_THRESH       = ${CVD_STRONG_CVD_THRESH}", "white"))
    print(colored(f"      PULLBACK_PCT                = {PULLBACK_PCT * 100:.1f}%", "white"))
    print(colored(f"      ORDER_SIZE_USD              = ${ORDER_SIZE_USD}", "white"))
    print(colored(f"      MIN_TIME_LEFT               = {MIN_TIME_LEFT}s", "white"))
    print(colored(f"      MAX_STINK_BID_PRICE         = ${MAX_STINK_BID_PRICE}", "white"))
    print(colored(f"      BOT_POLL_INTERVAL           = {BOT_POLL_INTERVAL}s", "white"))
    print(colored(f"      HEDGE_ENABLED               = {HEDGE_ENABLED}", "white"))
    print(colored(f"      HEDGE_SYMBOL                = {HEDGE_SYMBOL}", "white"))
    print(colored(f"      HEDGE_USD                   = ${HEDGE_USD}", "white"))
    print(colored(f"      HEDGE_LEVERAGE              = {HEDGE_LEVERAGE}x", "white"))

    if PAPER_MODE:
        print(colored("""
   ╔══════════════════════════════════════════╗
   ║  📝 PAPER TRADING MODE — NO REAL ORDERS  ║
   ║  Signals + outcomes logged for calibration ║
   ╚══════════════════════════════════════════╝
        """, "yellow", attrs=["bold"]))

    print(colored(f"\n   🔄 Initializing...", "yellow", attrs=["bold"]))

    # Initialize Polymarket CLOB client
    clob_client = init_clob_client()

    # Check for existing HL positions
    if HEDGE_ENABLED:
        pos = hl_get_position()
        if pos:
            side = "LONG" if pos["is_long"] else "SHORT"
            print(colored(
                f"   ⚠️ Existing HL position: {side} {abs(pos['size'])} BTC | "
                f"Entry: ${pos['entry_price']:.1f} | PnL: ${pos['unrealized_pnl']:.2f}",
                "yellow",
            ))

    # Print trade summary
    print_trade_summary()
    if PAPER_MODE:
        print_paper_summary()

    # Start Binance CVD feed
    feed = BinanceCVDFeed()
    feed.start()

    print(colored("\n   ⏳ Waiting 10s for initial trade data...", "yellow"))
    time.sleep(10)

    btc_price = feed.get_last_price()
    trade_count = feed.get_trade_count()
    if btc_price > 0:
        print(colored(f"   ✅ Feed active: BTC ${btc_price:,.2f} | {trade_count} trades buffered", "green"))
    else:
        print(colored("   ⚠️ No trades received yet, feed may be connecting...", "yellow"))

    print(colored(f"\n   👁️ Watching BTC order flow for divergences and momentum...", "white"))
    print(colored(f"      Press Ctrl+C to stop.", "yellow"))
    print(colored(f"{'=' * 70}\n", "green"))

    bot = CVDStinkBot(feed)

    while True:
        try:
            market_ts = get_current_market_timestamp()
            time_remaining = get_time_remaining(market_ts)

            # If too little time left, wait for next market
            if time_remaining < MIN_TIME_LEFT + 30:
                next_ts = market_ts + MARKET_DURATION
                wait_time = next_ts - int(time.time())
                if wait_time > 0:
                    next_dt = datetime.fromtimestamp(next_ts, tz=ET)
                    print(colored(
                        f"\n   🌙 Waiting {wait_time}s for next market "
                        f"({next_dt.strftime('%I:%M:%S%p ET')})...",
                        "yellow",
                    ))
                    time.sleep(wait_time + 1)
                market_ts = get_current_market_timestamp()

            bot.reset()
            bot.run_market_cycle(market_ts)

        except KeyboardInterrupt:
            print(colored(f"\n\n{'=' * 70}", "yellow"))
            print(colored("🌙 CVD Bot stopped!", "yellow", attrs=["bold"]))
            print(colored(f"{'=' * 70}", "yellow"))
            bot.cancel_orders()
            if HEDGE_ENABLED:
                pos = hl_get_position()
                if pos:
                    print(colored("   ⚠️ Open HL hedge - close manually or restart.", "yellow"))
                else:
                    print(colored("   ✅ No open hedges - clean shutdown!", "green"))
            feed.stop()
            break

        except Exception as e:
            print(colored(f"\n   ⚠️ Hiccup: {str(e)[:80]}, back in 5s...", "yellow"))
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    print("🌙 CVD 5-Minute Bot v2.0 - Volume-weighted CVD alpha, let's go!")
    main()
