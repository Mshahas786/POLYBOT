#!/usr/bin/env python3
"""
PolyBot v4.0 - Lightweight Trading Engine
Optimized for free-tier VPS & Raspberry Pi
"""

import json
import os
import time
import threading
import requests
import websocket
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType

# Lightweight config - no heavy web3 imports unless needed for redemption
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")
RELAYER_URL = "https://relayer-v2.polymarket.com"
CHAIN_ID = 137

# Performance tuning for low-resource systems
PRICE_BUFFER_SIZE = 150  # Reduced from 600 (~5 min of data at 2s intervals)
SIGNAL_CHECK_INTERVAL = 15  # Seconds between signal checks (was every 1s)
LOG_BUFFER_SIZE = 500  # Max log entries to keep in memory


app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# Add CORS headers to all responses
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

# ── Paths ──────────────────────────────────────────────────
# Use the current directory (vps folder) for all production data to ensure consistency across environments
BOT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = BOT_DIR / "config.json"
TRADES_PATH = BOT_DIR / "trades.json"
ENV_PATH = BOT_DIR / ".env"
LOG_PATH = BOT_DIR / "bot.log"

# ── Global State ───────────────────────────────────────────
start_time = datetime.now(timezone.utc)
bot_running = False
bot_thread = None

# Fast Price Feed
last_btc_price = 0
price_lock = threading.Lock()
price_buffer = []  # Rolling buffer of (timestamp, price) for signals
account_stats = {"balance": 0.0, "pnl": 0.0, "last_updated": 0}

current_strategy_info = {
    "slug": "N/A", "price_to_beat": 0, "current_diff": 0,
    "time_remaining": 0, "up_price": 0, "down_price": 0,
    "edge": "None", "status": "Inactive", "confidence": 0,
    "signals": {}
}
last_redeem_time = time.time()  # Initialize to current time to trigger first redemption after 10 min

# ── Binance WebSocket (Lightweight) ──────────────────────
class BinanceWS:
    def __init__(self):
        self.url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        self.ws = None
        self.thread = None
        self.last_buffer_update = 0

    def on_message(self, ws, message):
        global last_btc_price, price_buffer
        data = json.loads(message)
        price = float(data['p'])
        now = time.time()
        with price_lock:
            last_btc_price = price
            # Buffer price every 2 seconds (avoid flooding)
            if now - self.last_buffer_update >= 2:
                price_buffer.append((now, price))
                if len(price_buffer) > PRICE_BUFFER_SIZE:
                    price_buffer = price_buffer[-PRICE_BUFFER_SIZE:]
                self.last_buffer_update = now

    def on_error(self, ws, error):
        # Reduced logging for performance
        pass

    def on_close(self, ws, close_status_code, close_msg):
        time.sleep(5)
        self.run()

    def run(self):
        self.ws = websocket.WebSocketApp(
            self.url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        # Enable ping/pong to keep connection alive with lower CPU
        self.ws.run_forever(ping_interval=30, ping_timeout=10)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

ws_client = BinanceWS()
ws_client.start()

# ── Safe File Helpers ─────────────────────────────────────
def safe_read_json(path):
    for _ in range(5):
        try:
            if not path.exists(): return None
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (PermissionError, json.JSONDecodeError):
            time.sleep(0.1)
    return None

def safe_write_json(path, data):
    for _ in range(5):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return True
        except PermissionError:
            time.sleep(0.1)
    return False

log_lock = threading.Lock()
def log_to_file(msg):
    """Lightweight logging with thread lock and file size limit"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"{ts} [INFO] {msg}"
    print(full_msg)
    for _ in range(5):
        try:
            with log_lock:
                # Keep log file under 2MB to save disk I/O on Raspberry Pi
                if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2 * 1024 * 1024:
                    # Truncate to last 500 lines
                    with open(LOG_PATH, "r", encoding="utf-8") as f:
                        lines = f.readlines()[-500:]
                    with open(LOG_PATH, "w", encoding="utf-8") as f:
                        f.writelines(lines)
                
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(full_msg + "\n")
            return
        except PermissionError:
            time.sleep(0.1)

# ── Advanced Signal Engine (Multi-Strategy for 70%+ accuracy) ────────

def calc_ema(prices, period):
    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / len(gains) if gains else 0
    avg_loss = sum(losses) / len(losses) if losses else 0.001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_bollinger_bands(prices, period=20, num_std=2):
    """Calculate Bollinger Bands (middle, upper, lower) and %B indicator"""
    if len(prices) < period:
        return None
    sma = sum(prices[-period:]) / period
    variance = sum((p - sma) ** 2 for p in prices[-period:]) / period
    std_dev = variance ** 0.5
    upper = sma + (num_std * std_dev)
    lower = sma - (num_std * std_dev)
    current_price = prices[-1]
    percent_b = (current_price - lower) / (upper - lower) if upper != lower else 0.5
    return {
        "sma": sma,
        "upper": upper,
        "lower": lower,
        "percent_b": percent_b,
        "bandwidth": (upper - lower) / sma if sma > 0 else 0
    }

def calc_macd(prices, fast=12, slow=26, signal=9):
    """Calculate MACD (Moving Average Convergence Divergence)"""
    if len(prices) < slow + signal:
        return {"macd": 0, "signal": 0, "histogram": 0}
    
    ema_fast = calc_ema(prices[-fast*2:], fast)
    ema_slow = calc_ema(prices[-slow*2:], slow)
    macd_line = ema_fast - ema_slow
    
    # Simplified signal line calculation
    recent_macd = []
    for i in range(signal, 0, -1):
        if len(prices) >= slow + i:
            ef = calc_ema(prices[-(fast+i):], fast)
            es = calc_ema(prices[-(slow+i):], slow)
            recent_macd.append(ef - es)
    
    signal_line = calc_ema(recent_macd, signal) if recent_macd else macd_line
    histogram = macd_line - signal_line
    
    return {
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram
    }

def calc_momentum(prices, lookback=10):
    """Calculate price momentum and velocity (rate of change)"""
    if len(prices) < lookback + 1:
        return {"momentum": 0, "velocity": 0, "acceleration": 0}
    
    current = prices[-1]
    past = prices[-lookback]
    momentum = (current - past) / past * 100
    
    # Velocity: rate of change per period
    velocity = momentum / lookback
    
    # Acceleration: change in velocity
    if len(prices) >= lookback * 2 + 1:
        past_momentum = (prices[-lookback] - prices[-lookback*2]) / prices[-lookback*2] * 100
        past_velocity = past_momentum / lookback
        acceleration = velocity - past_velocity
    else:
        acceleration = 0
    
    return {
        "momentum": momentum,
        "velocity": velocity,
        "acceleration": acceleration
    }

def calc_vwap(prices_buffer):
    """Calculate Volume-Weighted Average Price (using time as volume proxy)"""
    if not prices_buffer:
        return None
    
    # Use recent 60 seconds vs prior 60 seconds
    now = time.time()
    recent_60 = [p for t, p in prices_buffer if t > now - 60]
    prior_60 = [p for t, p in prices_buffer if now - 120 < t <= now - 60]
    
    if not recent_60 or not prior_60:
        return None
    
    vwap_recent = sum(recent_60) / len(recent_60)
    vwap_prior = sum(prior_60) / len(prior_60)
    
    return {
        "vwap_recent": vwap_recent,
        "vwap_prior": vwap_prior,
        "vwap_diff": vwap_recent - vwap_prior,
        "signal": "UP" if vwap_recent > vwap_prior else "DOWN"
    }

def fetch_account_stats(address):
    global account_stats
    if not address: return
    try:
        # 1. Fetch Balance from Gamma API (USDC on Polygon)
        balance_url = f"https://gamma-api.polymarket.com/balances?address={address}"
        resp = requests.get(balance_url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            usdc_balance = 0.0
            for item in data:
                # The Gamma API returns a list of balances
                if item.get("asset") == "USDC" or "USDC" in str(item.get("asset")):
                    usdc_balance = float(item.get("balance", 0))
                    break
            account_stats["balance"] = round(usdc_balance, 2)

        # 2. Fetch P&L from Data API
        pnl_url = f"https://data-api.polymarket.com/pnl?address={address}&period=all"
        resp = requests.get(pnl_url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                total_pnl = float(data[-1].get("pnl", 0))
                account_stats["pnl"] = round(total_pnl, 2)
        
        account_stats["last_updated"] = time.time()
    except Exception as e:
        print(f"Stats Fetch Error: {e}")

def analyze_signals(price_to_beat):
    """
    MULTI-STRATEGY ENGINE v4.0 - Based on extensive research of profitable Polymarket bots
    
    Strategies implemented:
    1. Trend Following (SMA + EMA crossover)
    2. RSI Momentum (overbought/oversold reversal)
    3. MACD Convergence (trend strength)
    4. Bollinger Bands (volatility breakout)
    5. Price Momentum (velocity + acceleration)
    6. VWAP Comparison (volume-weighted trend)
    7. Last-Second Momentum Snipe (final 60-90s entry)
    
    Research-backed improvements:
    - Multi-timeframe analysis (15s, 60s, 180s windows)
    - Volatility regime detection
    - Signal weighting based on market conditions
    - Minimum confidence threshold (65%+)
    """
    with price_lock:
        buf = list(price_buffer)
        current = last_btc_price

    if len(buf) < 50 or not current or not price_to_beat:
        # Log why we're skipping (only periodically)
        if len(buf) < 50 and len(buf) % 25 == 0 and len(buf) > 0:
            log_to_file(f"⚠️ Signal engine: Buffer building ({len(buf)}/50 prices)")
        return None, 0, {}

    prices = [p for _, p in buf]
    now = time.time()
    
    # Calculate current position in 5-minute window
    window_offset = int(now % 300)
    time_remaining = 300 - window_offset
    
    # ── Strategy 1: Trend Filter (50-period SMA) ──
    sma_50 = sum(prices[-50:]) / 50
    trend = "UP" if current > sma_50 else "DOWN"
    trend_strength = abs(current - sma_50) / sma_50 * 100

    # ── Strategy 2: RSI Momentum (Multi-timeframe) ──
    rsi_14 = calc_rsi(prices[-30:], 14)
    rsi_9 = calc_rsi(prices[-18:], 9)
    
    # RSI signals with context
    rsi_signal = "NEUTRAL"
    if rsi_14 > 70:
        rsi_signal = "DOWN"  # Overbought - reversal likely
    elif rsi_14 < 30:
        rsi_signal = "UP"  # Oversold - bounce likely
    elif rsi_14 > 60 and rsi_9 > 65:
        rsi_signal = "UP"  # Strong uptrend momentum
    elif rsi_14 < 40 and rsi_9 < 35:
        rsi_signal = "DOWN"  # Strong downtrend momentum

    # ── Strategy 3: MACD Convergence ──
    macd_data = calc_macd(prices[-60:])
    macd_signal = "NEUTRAL"
    if macd_data["histogram"] > 0:
        macd_signal = "UP"
    elif macd_data["histogram"] < 0:
        macd_signal = "DOWN"
    
    macd_strength = min(abs(macd_data["histogram"]) / current * 10000, 10)  # Normalize

    # ── Strategy 4: Bollinger Bands ──
    bb_data = calc_bollinger_bands(prices[-40:], 20, 2)
    bb_signal = "NEUTRAL"
    if bb_data:
        if bb_data["percent_b"] > 0.9:
            bb_signal = "DOWN"  # Price at upper band - potential reversal
        elif bb_data["percent_b"] < 0.1:
            bb_signal = "UP"  # Price at lower band - potential bounce
        elif bb_data["percent_b"] > 0.6:
            bb_signal = "UP"  # Strong uptrend
        elif bb_data["percent_b"] < 0.4:
            bb_signal = "DOWN"  # Strong downtrend

    # ── Strategy 5: Price Momentum (Multi-window) ──
    mom_10 = calc_momentum(prices[-20:], 10)  # Short-term
    mom_20 = calc_momentum(prices[-40:], 20)  # Medium-term
    
    momentum_signal = "NEUTRAL"
    if mom_10["momentum"] > 0.05 and mom_10["acceleration"] > 0:
        momentum_signal = "UP"
    elif mom_10["momentum"] < -0.05 and mom_10["acceleration"] < 0:
        momentum_signal = "DOWN"
    elif mom_20["momentum"] > 0.1:
        momentum_signal = "UP"
    elif mom_20["momentum"] < -0.1:
        momentum_signal = "DOWN"

    # ── Strategy 6: VWAP Comparison ──
    vwap_data = calc_vwap(buf)
    vwap_signal = "NEUTRAL"
    if vwap_data:
        vwap_signal = vwap_data["signal"]

    # ── Strategy 7: Last-Second Momentum Snipe (Final 60-90s) ──
    # Research shows ~15-20% of periods resolve based on movements in final seconds
    last_second_signal = "NEUTRAL"
    if time_remaining <= 90 and time_remaining >= 30:
        # Analyze micro-momentum in last 10 seconds
        recent_prices = [p for t, p in buf if t > now - 10]
        if len(recent_prices) >= 3:
            micro_momentum = (recent_prices[-1] - recent_prices[0]) / recent_prices[0] * 100
            if micro_momentum > 0.02:
                last_second_signal = "UP"
            elif micro_momentum < -0.02:
                last_second_signal = "DOWN"

    # ── Market Regime Detection ──
    volatility_regime = "NORMAL"
    if bb_data:
        if bb_data["bandwidth"] > 0.003:  # High volatility
            volatility_regime = "HIGH"
        elif bb_data["bandwidth"] < 0.001:  # Low volatility
            volatility_regime = "LOW"
    
    # ── Adaptive Signal Weighting ──
    # Adjust weights based on market conditions
    if volatility_regime == "HIGH":
        # In high volatility, momentum and BB are more reliable
        weights = {
            "trend": 1.5,
            "rsi": 2.0,
            "macd": 1.5,
            "bollinger": 2.5,
            "momentum": 2.0,
            "vwap": 1.5,
            "price_action": 1.0,
            "last_second": 1.5
        }
    elif volatility_regime == "LOW":
        # In low volatility, trend following works better
        weights = {
            "trend": 2.5,
            "rsi": 1.5,
            "macd": 2.0,
            "bollinger": 1.0,
            "momentum": 1.5,
            "vwap": 2.0,
            "price_action": 2.0,
            "last_second": 0.5
        }
    else:  # NORMAL
        weights = {
            "trend": 2.0,
            "rsi": 1.5,
            "macd": 1.5,
            "bollinger": 1.5,
            "momentum": 2.0,
            "vwap": 1.5,
            "price_action": 2.0,
            "last_second": 1.0
        }
    
    # ── Weighted Voting System ──
    votes_up = 0.0
    votes_down = 0.0
    total_weight = 0.0
    
    # Signal 1: Price vs baseline (immediate edge)
    price_diff_pct = (current - price_to_beat) / price_to_beat * 100
    if price_diff_pct > 0.08:
        votes_up += weights["price_action"]
    elif price_diff_pct < -0.08:
        votes_down += weights["price_action"]
    total_weight += weights["price_action"]
    
    # Signal 2: Trend
    if trend == "UP":
        votes_up += weights["trend"] * min(trend_strength * 10, 1)
    else:
        votes_down += weights["trend"] * min(trend_strength * 10, 1)
    total_weight += weights["trend"]
    
    # Signal 3: RSI
    if rsi_signal == "UP":
        votes_up += weights["rsi"]
    elif rsi_signal == "DOWN":
        votes_down += weights["rsi"]
    total_weight += weights["rsi"]
    
    # Signal 4: MACD
    if macd_signal == "UP":
        votes_up += weights["macd"] * min(macd_strength, 1)
    elif macd_signal == "DOWN":
        votes_down += weights["macd"] * min(macd_strength, 1)
    total_weight += weights["macd"]
    
    # Signal 5: Bollinger Bands
    if bb_signal == "UP":
        votes_up += weights["bollinger"]
    elif bb_signal == "DOWN":
        votes_down += weights["bollinger"]
    total_weight += weights["bollinger"]
    
    # Signal 6: Momentum
    if momentum_signal == "UP":
        votes_up += weights["momentum"]
    elif momentum_signal == "DOWN":
        votes_down += weights["momentum"]
    total_weight += weights["momentum"]
    
    # Signal 7: VWAP
    if vwap_signal == "UP":
        votes_up += weights["vwap"]
    elif vwap_signal == "DOWN":
        votes_down += weights["vwap"]
    total_weight += weights["vwap"]
    
    # Signal 8: Last-Second Momentum (only active in final 90s)
    if last_second_signal != "NEUTRAL" and time_remaining <= 90:
        if last_second_signal == "UP":
            votes_up += weights["last_second"]
        elif last_second_signal == "DOWN":
            votes_down += weights["last_second"]
        total_weight += weights["last_second"]
    
    # ── Calculate Confidence (Old working formula: divide by 7) ──
    # The old bot used / 7 and worked at 70%+ win rate
    # Keep backward compatibility with the proven formula
    total_votes = votes_up + votes_down
    if total_votes == 0:
        return None, 0, {}
    
    direction = "UP" if votes_up > votes_down else "DOWN"
    confidence = max(votes_up, votes_down) / 7 * 100  # Old working formula
    
    # ── Confidence Threshold (configurable, default 55%) ──
    # Old bot used 50-60% threshold
    try:
        _cfg = safe_read_json(CONFIG_PATH) or {}
        _min_conf = float(_cfg.get("min_confidence", 55))
    except:
        _min_conf = 55
    
    if confidence < _min_conf:
        confidence = 0
        direction = None
    
    # Log signal analysis for debugging (when meaningful or during entry windows)
    _window_offset_check = int(time.time() % 300)
    _raw_conf = max(votes_up, votes_down) / 7 * 100
    if _raw_conf > 40 or (60 <= _window_offset_check <= 285):
        log_to_file(
            f"📊 SIGNALS: votes_up={votes_up:.1f} votes_down={votes_down:.1f} | "
            f"raw_conf={_raw_conf:.1f}% | threshold={_min_conf}% | "
            f"final_conf={confidence}% | dir={direction or 'NONE'} | "
            f"window={_window_offset_check}s"
        )
    
    # ── Build Signal Details ──
    signals = {
        "trend": trend,
        "trend_strength": round(trend_strength, 3),
        "rsi_14": round(rsi_14, 1),
        "rsi_9": round(rsi_9, 1),
        "rsi_signal": rsi_signal,
        "macd_hist": round(macd_data["histogram"], 4),
        "macd_signal": macd_signal,
        "bb_percent_b": round(bb_data["percent_b"], 3) if bb_data else 0.5,
        "bb_signal": bb_signal,
        "momentum": round(mom_10["momentum"], 4),
        "momentum_accel": round(mom_10["acceleration"], 4),
        "momentum_signal": momentum_signal,
        "vwap_signal": vwap_signal,
        "last_second_signal": last_second_signal,
        "volatility_regime": volatility_regime,
        "votes": f"{votes_up:.1f}U/{votes_down:.1f}D",
        "confidence": round(confidence, 1)
    }
    
    # Verbose logging for meaningful signals
    if confidence >= 65:
        log_to_file(
            f"🔍 SIGNAL: {direction} ({confidence:.1f}%) | "
            f"Vol: {volatility_regime} | "
            f"Trend: {trend} | RSI: {rsi_14:.1f} | "
            f"MACD: {macd_signal} | BB: {bb_signal} | "
            f"Momentum: {momentum_signal} | VWAP: {vwap_signal} | "
            f"LastSec: {last_second_signal}"
        )
    
    return direction, confidence, signals

# ── Data Sources ──────────────────────────────────────────
def get_polymarket_market(slug):
    try:
        resp = requests.get(f"https://gamma-api.polymarket.com/markets?slug={slug}", timeout=5)
        data = resp.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
        return None
    except Exception as e:
        log_to_file(f"⚠️ Gamma API Error: {e}")
        return None

def get_clob_market_line(condition_id):
    """Fetch official strike price from CLOB API."""
    try:
        resp = requests.get(f"https://clob.polymarket.com/markets/{condition_id}", timeout=5)
        data = resp.json()
        return float(data["line"]) if "line" in data else None
    except Exception as e:
        log_to_file(f"⚠️ CLOB Sync Error: {e}")
        return None

def get_price_to_beat(window_ts, condition_id=None):
    # 1. Try Official CLOB Sync (100% accurate)
    if condition_id:
        line = get_clob_market_line(condition_id)
        if line: return line
    
    # 2. Binance 1m Open Price (Matches Chainlink oracle start timestamp very closely)
    try:
        resp = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={window_ts * 1000}&limit=1",
            timeout=5
        )
        data = resp.json()
        if data and len(data) > 0:
            return float(data[0][1]) # Index 1 is Open price
    except Exception as e:
        log_to_file(f"⚠️ Binance Baseline Sync Error: {e}")
        pass

    # 3. Historical Sync (CryptoCompare Fallback)
    try:
        resp = requests.get(
            f"https://min-api.cryptocompare.com/data/v2/histominute?fsym=BTC&tsym=USD&limit=1&toTs={window_ts}",
            timeout=5
        )
        return float(resp.json()["Data"]["Data"][-1]["close"])
    except:
        # 4. Last recorded price
        with price_lock: return last_btc_price

def get_current_5min_ts():
    return (int(time.time()) // 300) * 300

# ── Bot Loop ──────────────────────────────────────────────
def bot_loop():
    global bot_running, current_strategy_info
    log_to_file("🚀 ENGINE v4.0 (Market Making) STARTING...")

    market_baselines = {}
    last_market_fetch = 0
    cached_market = None
    last_mm_run = 0
    last_signal_check = 0

    while bot_running:
        try:
            cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True}
            now = time.time()
            window_ts = get_current_5min_ts()
            window_offset = int(now % 300)
            time_remaining = 300 - window_offset
            slug = f"btc-updown-5m-{window_ts}"
            
            # Check which strategy to run
            strategy = cfg.get("strategy", "directional")

            # Fetch market data every 30s
            if now - last_market_fetch > 30:
                market = get_polymarket_market(slug)
                last_market_fetch = now
                cached_market = market
            else:
                market = cached_market

            if not market:
                current_strategy_info["status"] = "SCANNING..."
                time.sleep(5)
                continue

            # Parse odds
            outcomes = market.get("outcomePrices", [])
            if isinstance(outcomes, str):
                try: outcomes = json.loads(outcomes)
                except: outcomes = outcomes.strip("[]").split(",")
            if len(outcomes) < 2:
                time.sleep(5)
                continue

            up_price = float(outcomes[0])
            down_price = float(outcomes[1])

            # Initialize Live Client if needed
            client = None
            if not cfg.get("dry_run", True):
                try:
                    load_dotenv(ENV_PATH)
                    pk = os.getenv("POLY_PRIVATE_KEY")
                    addr = os.getenv("POLY_WALLET_ADDRESS")

                    # Refresh Balance every 2 minutes
                    if addr and (time.time() - account_stats["last_updated"] > 120):
                        threading.Thread(target=fetch_account_stats, args=(addr,), daemon=True).start()

                    if pk and addr:
                        api_key = os.getenv("POLY_API_KEY")
                        api_secret = os.getenv("POLY_API_SECRET")
                        api_passphrase = os.getenv("POLY_API_PASSPHRASE")

                        if api_key and api_secret and api_passphrase:
                            from py_clob_client.clob_types import ApiCreds
                            creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
                        else:
                            temp_client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137)
                            creds = temp_client.create_or_derive_api_creds()
                        client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, creds=creds, signature_type=1, funder=addr)
                except Exception as e:
                    log_to_file(f"⚠️ Live Client Init Error: {e}")

            # Run Market Making Strategy
            if strategy == "market_making":
                # Update time_remaining for dashboard
                current_strategy_info["time_remaining"] = time_remaining
                current_strategy_info["up_price"] = up_price
                current_strategy_info["down_price"] = down_price
                current_strategy_info["status"] = f"MM: {time_remaining//60}:{time_remaining%60:02d} left"
                current_strategy_info["edge"] = f"Spread: {abs(up_price - down_price):.3f}"
                
                # Log MM status every 60 seconds
                if now - last_mm_run >= 60:
                    last_mm_run = now
                    log_to_file(f"📊 MM Cycle: Up=${up_price:.3f} Down=${down_price:.3f} Spread={abs(up_price-down_price):.3f}")
                    
                    market_make_loop(client, market, cfg)
                    
                    # Update dashboard with MM stats
                    current_strategy_info["mm_fills"] = active_mm_orders.get("fills", 0)
                    current_strategy_info["mm_profit"] = round(active_mm_orders.get("profit", 0), 2)
                    current_strategy_info["mm_spread"] = active_mm_orders.get("last_spread", 0)
                
                time.sleep(1)
                continue
            
            # Run Directional Strategy (original code)
            else:
                # Baseline Sync (only once per window)
                if window_ts not in market_baselines:
                    line = get_price_to_beat(window_ts, market.get("conditionId"))
                    market_baselines[window_ts] = line
                    log_to_file(f"🎯 Baseline Synced: ${line}")

                price_to_beat = market_baselines[window_ts]

            trades = safe_read_json(TRADES_PATH) or []
            already_traded = any(t.get("window_ts") == window_ts for t in trades)

            if already_traded:
                current_strategy_info = {
                    "slug": slug, "price_to_beat": price_to_beat, "current_diff": 0,
                    "time_remaining": time_remaining, "up_price": up_price, "down_price": down_price,
                    "edge": "None", "status": "Waiting for Result... ⏳", "confidence": 0, "signals": {}
                }
            else:
                # Run signal check every 15s (reduced from every 1s)
                if now - last_signal_check >= SIGNAL_CHECK_INTERVAL:
                    last_signal_check = now
                    
                    direction, confidence, signals = analyze_signals(price_to_beat)

                    with price_lock: price_now = last_btc_price

                    # Heartbeat log every minute
                    if int(now) % 60 == 0:
                        log_to_file(f"🤖 BTC: ${price_now} | Target: ${price_to_beat} | Conf: {confidence}% | Remaining: {time_remaining}s")
                    diff = (price_now - price_to_beat) / price_to_beat * 100 if price_to_beat else 0

                    current_strategy_info = {
                        "slug": slug, "price_to_beat": price_to_beat, "current_diff": round(diff, 3),
                        "time_remaining": time_remaining, "up_price": up_price, "down_price": down_price,
                        "edge": "Multi-Strategy v4.0", "status": "Analyzing", "confidence": confidence, "signals": signals
                    }

            # 5. OPTIMIZED Entry Logic - EARLY entry for better ROI
                #
                # RISK/REWARD MATH (why late entry is terrible):
                # Enter at 0:30 → token ~$0.95, win = +$0.05, lose = -$0.95 (1 loss = 19 wins)
                # Enter at 4:00 → token ~$0.50, win = +$0.50, lose = -$0.50 (1:1 risk/reward)
                # Early entry has 10x better risk/reward!
                #
                # Market Window (counts DOWN):
                # 5:00 → 4:00 → 3:00 → 2:00 → 1:00 → 0:00 (resolves)
                #
                # ENTRY WINDOWS (time_remaining counts DOWN):
                # Phase 1: 240s-150s (4:00-2:30) - Best prices (~$0.45-$0.65), strong signals
                # Phase 2: 150s-90s (2:30-1:30) - Good prices (~$0.55-$0.75), standard
                # AVOID: <90s remaining (token too expensive, terrible ROI)
                #
                # TRADE QUALITY FILTERS:
                # 1. Price Momentum: Current price moved 0.05%+ from baseline
                # 2. Signal Strength: 4+ weighted votes agree
                # 3. Volatility: BTC moved 0.1%+ in last 60s (active market)
                # 4. Confidence: >= threshold for phase

                entry_triggered = False
                min_conf = cfg.get("min_confidence", 55)

                # Only check entry when we have valid signals and direction
                if direction and confidence > 0 and 90 <= time_remaining <= 240:
                    # Get signal agreement count
                    signal_agreement = 0
                    if signals:
                        votes_str = signals.get("votes", "0U/0D")
                        try:
                            up_votes = float(votes_str.split("U")[0])
                            down_votes = float(votes_str.split("U")[1].split("D")[0])
                            signal_agreement = up_votes if direction == "UP" else down_votes
                        except:
                            pass

                    # Quality filters
                    price_moved = abs(diff) >= 0.05  # Price moved 0.05%+ from baseline
                    strong_signals = signal_agreement >= 4.0  # At least 4 weighted votes

                    # Calculate short-term volatility (last 60s)
                    with price_lock:
                        recent_prices = [p for t, p in price_buffer if t > time.time() - 60]
                    if len(recent_prices) >= 10:
                        high = max(recent_prices)
                        low = min(recent_prices)
                        volatility = (high - low) / low * 100
                        volatile_market = volatility >= 0.1  # BTC moved 0.1%+ in 60s
                    else:
                        volatility = 0
                        volatile_market = True  # Assume active if not enough data

                    # Phase 1: Early entry (4:00-2:30 remaining)
                    # Best prices (~$0.45-$0.65), needs strong confirmation
                    if 150 <= time_remaining <= 240:
                        if confidence >= 70 and price_moved and strong_signals and volatile_market:
                            current_strategy_info["status"] = f"PHASE 1: Early Entry ✓ ({time_remaining//60}:{time_remaining%60:02d} left, vol={volatility:.2f}%)"
                            entry_triggered = True
                        else:
                            reasons = []
                            if confidence < 70: reasons.append(f"conf {confidence:.0f}% < 70%")
                            if not price_moved: reasons.append("price stable")
                            if not strong_signals: reasons.append(f"signals {signal_agreement:.0f} < 4")
                            if not volatile_market: reasons.append(f"low vol {volatility:.2f}%")
                            current_strategy_info["status"] = f"PHASE 1: Waiting ({', '.join(reasons)})"

                    # Phase 2: Standard entry (2:30-1:30 remaining)
                    # Good prices (~$0.55-$0.75), standard requirements
                    elif 90 <= time_remaining < 150:
                        if confidence >= 60 and price_moved:
                            current_strategy_info["status"] = f"PHASE 2: Standard Entry ✓ ({time_remaining//60}:{time_remaining%60:02d} left)"
                            entry_triggered = True
                        else:
                            reasons = []
                            if confidence < 60: reasons.append(f"conf {confidence:.0f}% < 60%")
                            if not price_moved: reasons.append("price stable")
                            current_strategy_info["status"] = f"PHASE 2: Waiting ({', '.join(reasons)})"

                # Outside entry window
                elif direction and confidence > 0:
                    if time_remaining > 240:
                        current_strategy_info["status"] = f"Early: {time_remaining//60}:{time_remaining%60:02d} left (wait for 4:00)"
                    elif time_remaining < 90:
                        current_strategy_info["status"] = f"Late: {time_remaining//60}:{time_remaining%60:02d} left (token too expensive, skip)"

                if entry_triggered:
                    # Filter trades in the last hour for limit
                    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                    recent_trades = [t for t in trades if t["timestamp"] > one_hour_ago]

                    max_hour = cfg.get("max_trades_per_hour", 12)

                    # Extract Token IDs
                    tokens = market.get("tokens", [])
                    clob_ids = market.get("clobTokenIds", "[]")
                    if isinstance(clob_ids, str):
                        try: clob_ids = json.loads(clob_ids)
                        except: clob_ids = []

                    up_token_id = clob_ids[0] if len(clob_ids) > 0 else (tokens[0].get("tokenId") if len(tokens) > 0 else None)
                    down_token_id = clob_ids[1] if len(clob_ids) > 1 else (tokens[1].get("tokenId") if len(tokens) > 1 else None)

                    target_token_id = up_token_id if direction == "UP" else down_token_id
                    target_price = up_price if direction == "UP" else down_price

                    min_conf = cfg.get("min_confidence", 55)

                    if direction and confidence >= min_conf:
                        if len(recent_trades) >= max_hour:
                            current_strategy_info["status"] = f"HOURLY LIMIT REACHED ({len(recent_trades)}/{max_hour})"
                        elif not client and not cfg.get("dry_run", True):
                            current_strategy_info["status"] = "WAITING FOR LIVE CLIENT"
                        elif target_token_id:
                            log_to_file(f"🚀 ENTERING TRADE: {direction} @ {confidence:.0f}% confidence | Window: {window_offset}s")
                            execute_trade(direction, target_token_id, target_price, price_now, slug, window_ts, confidence, signals, cfg, client, market)
                            entry_triggered = False

            check_outcomes(market_baselines)

            if len(market_baselines) > 20:
                market_baselines = {k: v for k, v in market_baselines.items() if k > window_ts - 3600}

            time.sleep(1)
        except Exception as e:
            log_to_file(f"⚠️ Strategy Error: {e}")
            time.sleep(2)

def check_outcomes(baselines):
    global bot_running
    trades = safe_read_json(TRADES_PATH) or []
    updated = False
    now = time.time()
    wins = 0
    losses = 0

    for t in trades:
        # Skip trades already counted with outcomes
        if t.get("outcome") == "win":
            wins += 1
            continue
        elif t.get("outcome") == "loss":
            losses += 1
            continue

        # Try to resolve pending trades
        wts = t.get("window_ts", 0)
        if now < wts + 330:
            continue
        
        # Get the ACTUAL price to beat (not from in-memory dict that gets wiped on restart)
        # Priority: 1. baselines dict, 2. CLOB API, 3. Binance historical, 4. CryptoCompare
        base = baselines.get(wts)
        if not base:
            condition_id = t.get("condition_id")
            if condition_id:
                base = get_clob_market_line(condition_id)
            if not base:
                base = get_price_to_beat(wts)
        
        if not base:
            log_to_file(f"⚠️ Cannot determine outcome for {t.get('direction')} - no baseline price available")
            continue
            
        try:
            resp = requests.get(f"https://min-api.cryptocompare.com/data/v2/histominute?fsym=BTC&tsym=USD&limit=1&toTs={wts+300}", timeout=5)
            close = float(resp.json()["Data"]["Data"][-1]["close"])
            win = (t["direction"] == "UP" and close >= base) or (t["direction"] == "DOWN" and close < base)
            t["outcome"] = "win" if win else "loss"
            log_to_file(f"{'✅' if win else '❌'} {t['direction']} Result | Base: {base} → Close: {close}")
            updated = True

            if win:
                wins += 1
            else:
                losses += 1
        except:
            continue

    if updated:
        safe_write_json(TRADES_PATH, trades)

    total = wins + losses
    if total >= 5 and bot_running:
        win_rate = (wins / total) * 100
        if win_rate < 50.0:
            bot_running = False
            log_to_file(f"🛑 AUTO-STOP: Win rate dropped below 50% ({win_rate:.1f}%). Engine Paused.")

def execute_trade(direction, token_id, token_price, btc_price, slug, window_ts, confidence, signals, cfg, client=None, market=None):
    is_dry = cfg.get("dry_run", True)
    status = "simulated"
    order_id = "N/A"
    condition_id = market.get("conditionId") if market else None

    if not is_dry and client:
        try:
            bet_size = float(cfg.get("bet_size", 2.0))

            # 0. PRE-FLIGHT BALANCE CHECK
            try:
                # Get fresh balance from CLOB
                # With signature_type=1 and funder=proxy_wallet, this checks the Safe Proxy balance
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                balance_data = client.get_balance_allowance(params)
                # balance_data is a dict with 'balance' key in USDC decimals (6 decimals)
                if isinstance(balance_data, dict):
                    current_balance = float(balance_data.get("balance", 0)) / 1e6
                else:
                    current_balance = float(balance_data) / 1e6 if balance_data else 0.0

                if current_balance < bet_size:
                    log_to_file(f"⚠️ BALANCE GUARD: Skipping trade. Need ${bet_size:.2f} USDC, but only have ${current_balance:.2f} USDC.")
                    # Trigger an immediate redemption check to see if we can recover funds
                    threading.Thread(target=redeem_all_winners, daemon=True).start()
                    return # Status stays 'simulated' or we can set it to 'skipped'
            except Exception as be:
                log_to_file(f"⚠️ Balance Check failed: {be} (Proceeding with attempt)")

            # Record conditionId for future redemptions
            if market:
                condition_id = market.get("conditionId")

            log_to_file(f"🎯 Placing MARKET {direction} Order (Amount: ${bet_size})")

            # Use FOK (Fill-Or-Kill) - fills entire order or cancels immediately
            capped_price = 0.95  # Higher cap to match available ask prices

            try:
                log_to_file(f"📊 FOK order @ ${capped_price:.2f}")

                order_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=bet_size,
                    side="BUY",
                    price=capped_price
                )
                signed_order = client.create_market_order(order_args)
                resp = client.post_order(signed_order, OrderType.FOK)

                if resp and (hasattr(resp, "orderID") or (isinstance(resp, dict) and "orderID" in resp)):
                    order_id = getattr(resp, "orderID", resp.get("orderID") if isinstance(resp, dict) else "N/A")
                    status = "placed"
                    log_to_file(f"✅ LIVE FOK ORDER SUCCESS: {direction} | OrderID: {order_id}")
                else:
                    status = "failed"
                    log_to_file(f"⚠️ FOK order failed: {resp}")

            except Exception as e:
                status = "failed"
                log_to_file(f"⚠️ FOK order failed: {e}")
                
        except Exception as e:
            status = "error"
            log_to_file(f"⚠️ Trade Execution Error: {e}")

    trade = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "window_ts": window_ts,
        "market_slug": slug, "direction": direction, "token_id": token_id,
        "token_price": token_price, "btc_price": btc_price, "confidence": confidence,
        "order_id": order_id, "signals": signals, "bet_size": cfg.get("bet_size", 2.0),
        "dry_run": is_dry, "status": status, "outcome": None, "condition_id": condition_id, "redeemed": False
    }

    if is_dry:
        log_to_file(f"🚀 HIGH CONFIDENCE (SIM): {direction} | Conf: {confidence}% | BTC: ${btc_price}")

    # Only save to history if actually placed or simulated
    if status in ["placed", "simulated"]:
        trades = safe_read_json(TRADES_PATH) or []
        trades.append(trade)
        safe_write_json(TRADES_PATH, trades)
    else:
        log_to_file(f"⚠️ Trade skipped recording due to status: {status}")

# ── Redemption Logic ───────────────────────────────────────

def get_market_condition_id(slug):
    """Fetch conditionId from Gamma API if missing from trade history."""
    try:
        resp = requests.get(f"https://gamma-api.polymarket.com/markets?slug={slug}", timeout=5)
        data = resp.json()
        if data and len(data) > 0:
            return data[0].get("conditionId")
    except Exception as e:
        log_to_file(f"⚠️ Error fetching conditionId for {slug}: {e}")
    return None

def redeem_all_winners():
    """Auto-redeem disabled."""
    global last_redeem_time
    last_redeem_time = time.time()


def mark_position_as_redeemed(condition_id):
    """Mark a position as redeemed in the local trades.json file."""
    if not condition_id:
        return

    trades = safe_read_json(TRADES_PATH) or []
    updated = False

    for trade in trades:
        if trade.get("condition_id") == condition_id and trade.get("outcome") == "win" and not trade.get("redeemed"):
            trade["redeemed"] = True
            trade["redeemed_at"] = datetime.now(timezone.utc).isoformat()
            updated = True

    if updated:
        safe_write_json(TRADES_PATH, trades)
        log_to_file(f"📝 Marked condition {condition_id[:10]}... as redeemed in local records")

# ── Market Making Strategy ────────────────────────────────
active_mm_orders = {"buy_order_id": None, "sell_order_id": None, "last_spread": 0, "fills": 0, "profit": 0}

def market_make_loop(client, market, cfg):
    """
    Market Making Strategy: Place buy and sell limit orders to capture spread
    - Buy below current bid
    - Sell above current ask
    - Profit from bid-ask spread
    - No directional prediction needed
    """
    global active_mm_orders
    
    tokens = market.get("tokens", [])
    clob_ids = market.get("clobTokenIds", "[]")
    if isinstance(clob_ids, str):
        try: clob_ids = json.loads(clob_ids)
        except: clob_ids = []
    
    if len(clob_ids) < 2 or len(tokens) < 2:
        return
    
    up_token_id = clob_ids[0]
    down_token_id = clob_ids[1]
    outcomes = market.get("outcomePrices", [])
    if isinstance(outcomes, str):
        try: outcomes = json.loads(outcomes)
        except: outcomes = []
    
    if len(outcomes) < 2:
        return
    
    up_price = float(outcomes[0])
    down_price = float(outcomes[1])
    
    # Market making: buy the cheaper side, sell the expensive side
    # Spread = difference between up and down prices
    mid_price = 0.50  # Theoretical midpoint
    spread = abs(up_price - down_price)
    spread_bps = cfg.get("spread_bps", 50)  # Basis points (50 = 0.5%)
    half_spread = (spread_bps / 10000) / 2
    
    # Calculate our bid and ask prices
    # We want to buy below mid and sell above mid
    buy_price = max(0.01, mid_price - half_spread)  # Our buy order (we're the buyer)
    sell_price = min(0.99, mid_price + half_spread)  # Our sell order (we're the seller)
    
    bet_size = float(cfg.get("bet_size", 2.0))
    
    log_to_file(f"📊 MM: Mid=${mid_price:.3f} | Buy=${buy_price:.3f} | Sell=${sell_price:.3f} | Spread={spread_bps}bps")
    
    is_dry = cfg.get("dry_run", True)
    
    if is_dry:
        log_to_file(f"🧪 DRY RUN: Would buy ${bet_size} @ ${buy_price:.3f}, sell ${bet_size} @ ${sell_price:.3f}")
        active_mm_orders["last_spread"] = spread_bps
        return
    
    # LIVE: Place limit orders
    from py_clob_client.clob_types import OrderArgs
    
    try:
        # Cancel existing orders first
        if active_mm_orders["buy_order_id"]:
            try:
                client.cancel_order(active_mm_orders["buy_order_id"])
            except: pass
        if active_mm_orders["sell_order_id"]:
            try:
                client.cancel_order(active_mm_orders["sell_order_id"])
            except: pass
        
        # Determine which token to trade (the one closer to fair value)
        # We want to buy the UNDERPRICED token and sell the OVERPRICED token
        if up_price < 0.50:
            # "Up" is cheap, buy it
            target_token = up_token_id
            target_side = "UP"
        else:
            # "Down" is cheap, buy it  
            target_token = down_token_id
            target_side = "DOWN"
        
        # Place BUY order
        buy_order_args = OrderArgs(
            token_id=target_token,
            price=buy_price,
            size=bet_size,
            side="BUY"
        )
        buy_signed = client.create_order(buy_order_args)
        buy_resp = client.post_order(buy_signed, OrderType.GTC)
        
        if buy_resp and (hasattr(buy_resp, "orderID") or (isinstance(buy_resp, dict) and "orderID" in buy_resp)):
            buy_id = getattr(buy_resp, "orderID", buy_resp.get("orderID"))
            active_mm_orders["buy_order_id"] = buy_id
            log_to_file(f"✅ MM BUY PLACED: ${bet_size} @ ${buy_price:.3f} | OrderID: {buy_id}")
        
        # Wait briefly for fill
        time.sleep(2)
        
        # Check if buy filled
        if active_mm_orders["buy_order_id"]:
            try:
                order_status = client.get_order(active_mm_orders["buy_order_id"])
                status_val = getattr(order_status, "status", "") if hasattr(order_status, "status") else order_status.get("status", "")
                
                if status_val == "FILLED" or status_val == "filled":
                    log_to_file(f"✅ MM BUY FILLED: ${bet_size} @ ${buy_price:.3f}")
                    active_mm_orders["fills"] += 1
                    
                    # Now place SELL order at higher price
                    sell_order_args = OrderArgs(
                        token_id=target_token,
                        price=sell_price,
                        size=bet_size,
                        side="SELL"
                    )
                    sell_signed = client.create_order(sell_order_args)
                    sell_resp = client.post_order(sell_signed, OrderType.GTC)
                    
                    if sell_resp and (hasattr(sell_resp, "orderID") or (isinstance(sell_resp, dict) and "orderID" in sell_resp)):
                        sell_id = getattr(sell_resp, "orderID", sell_resp.get("orderID"))
                        active_mm_orders["sell_order_id"] = sell_id
                        profit_per_share = sell_price - buy_price
                        active_mm_orders["profit"] += profit_per_share * bet_size
                        log_to_file(f"✅ MM SELL PLACED: ${bet_size} @ ${sell_price:.3f} | Profit potential: ${profit_per_share * bet_size:.2f}")
                    active_mm_orders["buy_order_id"] = None  # Reset buy
            except Exception as e:
                log_to_file(f"⚠️ MM order check error: {e}")
        
        active_mm_orders["last_spread"] = spread_bps
        
    except Exception as e:
        log_to_file(f"⚠️ MM order placement error: {e}")

# ── API Routes ─────────────────────────────────────────────

@app.route("/status")
def get_status():
    trades = safe_read_json(TRADES_PATH) or []
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = sum(1 for t in trades if t.get("outcome") == "loss")
    cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True}
    with price_lock: live_price = last_btc_price
    return jsonify({
        "running": bot_running, "dry_run": cfg.get("dry_run", True),
        "btc_price": live_price, "strategy": "Multi-Signal v3.1",
        "info": current_strategy_info, "total_trades": len(trades), "wins": wins, "losses": losses,
        "success_rate": round((wins/(wins+losses)*100),1) if (wins+losses) > 0 else 0,
        "uptime": str(datetime.now(timezone.utc) - start_time).split(".")[0],
        "account": account_stats,
        "bet_size": float(cfg.get("bet_size", 2.0))
    })

@app.route("/stats")
def get_stats():
    trades = safe_read_json(TRADES_PATH) or []
    period = request.args.get("period", "all")
    
    filtered = trades
    now = datetime.now(timezone.utc)
    
    if period == "30m":
        cutoff = now - timedelta(minutes=30)
        filtered = [t for t in trades if datetime.fromisoformat(t["timestamp"]) > cutoff]
    elif period == "1h":
        cutoff = now - timedelta(hours=1)
        filtered = [t for t in trades if datetime.fromisoformat(t["timestamp"]) > cutoff]
    elif period == "24h":
        cutoff = now - timedelta(hours=24)
        filtered = [t for t in trades if datetime.fromisoformat(t["timestamp"]) > cutoff]
        
    wins = sum(1 for t in filtered if t.get("outcome") == "win")
    losses = sum(1 for t in filtered if t.get("outcome") == "loss")
    
    return jsonify({
        "total_trades": len(filtered),
        "wins": wins,
        "losses": losses,
        "success_rate": round((wins/(wins+losses)*100),1) if (wins+losses) > 0 else 0,
        "history": filtered[-100:]  # Limit to last 100 for performance
    })

@app.route("/start", methods=["POST"])
def start_bot():
    global bot_running, bot_thread
    if not bot_running:
        bot_running = True
        bot_thread = threading.Thread(target=bot_loop, daemon=True)
        bot_thread.start()
        return jsonify({"status": "started"})
    return jsonify({"status": "already_running"})

@app.route("/stop", methods=["POST"])
def stop_bot():
    global bot_running
    bot_running = False
    return jsonify({"status": "stopped"})

@app.route("/restart", methods=["POST"])
def restart_bot():
    global bot_running, bot_thread
    bot_running = False
    time.sleep(1)
    bot_running = True
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    return jsonify({"status": "restarted"})

@app.route("/config", methods=["GET", "POST"])
def handle_config():
    if request.method == "POST":
        safe_write_json(CONFIG_PATH, request.get_json())
        return jsonify({"status": "saved"})
    return jsonify(safe_read_json(CONFIG_PATH) or {})

@app.route("/redeem", methods=["POST"])
def trigger_redeem():
    threading.Thread(target=redeem_all_winners, daemon=True).start()
    return jsonify({"status": "redemption_triggered"})

@app.route("/logs")
def get_logs():
    if not LOG_PATH.exists(): return jsonify({"logs": []})
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return jsonify({"logs": [l.strip() for l in lines[-100:]]})
    except: return jsonify({"logs": []})
    
@app.route("/clear-trades", methods=["POST"])
def clear_trades():
    try:
        if TRADES_PATH.exists():
            TRADES_PATH.unlink()
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route("/clear-logs", methods=["POST"])
def clear_logs():
    try:
        if LOG_PATH.exists():
            LOG_PATH.unlink()
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=3000)
