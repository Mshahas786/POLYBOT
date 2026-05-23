#!/usr/bin/env python3
"""
PolyBot v6.0 - Full Multi-Strategy Engine
Modules: Oracle Alignment, Entry Timing, Order Book Intelligence,
         Arbitrage & Hedge, Risk Management, Execution Infrastructure
"""

import csv
import io
import json
import math
import os
import ssl
import sqlite3
import time
import threading
import requests
import websocket
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path
from flask import Flask, jsonify, request, Response
from flask_cors import CORS
from dotenv import load_dotenv

from risk_manager import RiskManager

CHAIN_ID = 137
PRICE_BUFFER_SIZE = 300
SIGNAL_CHECK_INTERVAL = 10

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

API_KEY = os.getenv("POLYBOT_API_KEY", "")

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_KEY:
            return f(*args, **kwargs)
        provided = request.headers.get("X-API-Key", "")
        if provided != API_KEY:
            return jsonify({"status": "unauthorized", "message": "Invalid or missing X-API-Key header"}), 401
        return f(*args, **kwargs)
    return decorated

BOT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = BOT_DIR / "config.json"
TRADES_PATH = BOT_DIR / "trades.json"
ENV_PATH = BOT_DIR / ".env"
LOG_PATH = BOT_DIR / "bot.log"
DB_PATH = BOT_DIR / "bayesian.db"

start_time = datetime.now(timezone.utc)
bot_running = False
bot_thread = None
last_btc_price = 0.0
chainlink_price = 0.0
last_chainlink_update = 0.0
ob_bids = []
ob_asks = []
price_lock = threading.Lock()
strategy_lock = threading.Lock()
trades_lock = threading.Lock()
price_buffer = []
account_stats = {"balance": 0.0, "pnl": 0.0, "last_updated": 0}
current_strategy_info = {
    "slug": "N/A", "price_to_beat": 0, "current_diff": 0,
    "time_remaining": 0, "up_price": 0, "down_price": 0,
    "edge": "None", "status": "Inactive", "confidence": 0, "signals": {},
    "risk_status": "OK", "risk_reason": "",
    "phase": 0, "wall_ratio": 0, "lag_score": 0,
    "prioritized_signal": "None",
}
risk_manager = None  # type: Optional[RiskManager]

log_lock = threading.Lock()
LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
LOG_LEVEL = LOG_LEVELS.get(os.getenv("POLYBOT_LOG_LEVEL", "INFO").upper(), 20)

def log_to_file(msg, level="INFO"):
    level_num = LOG_LEVELS.get(level, 20)
    if level_num < LOG_LEVEL:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"{ts} [{level}] {msg}"
    print(full_msg)
    for _ in range(5):
        try:
            with log_lock:
                if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2 * 1024 * 1024:
                    with open(LOG_PATH, "r", encoding="utf-8") as f:
                        lines = f.readlines()[-500:]
                    with open(LOG_PATH, "w", encoding="utf-8") as f:
                        f.writelines(lines)
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(full_msg + "\n")
            return
        except PermissionError:
            time.sleep(0.1)

def safe_read_json(path):
    for _ in range(5):
        try:
            if not path.exists():
                return None
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

# ── SQLite Bayesian Tracker (Module 6) ──────────────────────────
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            slug TEXT,
            direction TEXT,
            entry_price REAL,
            btc_price_at_entry REAL,
            chainlink_price_at_entry REAL,
            ptb_at_entry REAL,
            wall_ratio REAL,
            lag_score REAL,
            momentum_delta REAL,
            acceleration REAL,
            phase INTEGER,
            signal_type TEXT,
            confidence REAL,
            outcome TEXT,
            pnl REAL,
            win INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bayesian_buckets (
            bucket TEXT PRIMARY KEY,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0.5,
            alpha REAL DEFAULT 1.0,
            beta REAL DEFAULT 1.0
        )
    """)
    conn.commit()
    conn.close()

def record_trade_db(slug, direction, entry_price, btc_price, chainlink_px,
                    ptb, wall_ratio, lag_score, mom_delta, accel,
                    phase, signal_type, confidence, outcome=None, pnl=None, win=None):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades
        (timestamp, slug, direction, entry_price, btc_price_at_entry,
         chainlink_price_at_entry, ptb_at_entry, wall_ratio, lag_score,
         momentum_delta, acceleration, phase, signal_type, confidence,
         outcome, pnl, win)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        slug, direction, entry_price, btc_price,
        chainlink_px, ptb, wall_ratio, lag_score,
        mom_delta, accel, phase, signal_type, confidence,
        outcome, pnl, win
    ))
    conn.commit()
    conn.close()

def update_trade_outcome_db(slug, outcome, pnl, win):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("""
        UPDATE trades SET outcome=?, pnl=?, win=?
        WHERE slug=? AND outcome IS NULL
    """, (outcome, pnl, win, slug))
    conn.commit()
    conn.close()

def update_bayesian_bucket(bucket, win):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("SELECT wins, losses, alpha, beta FROM bayesian_buckets WHERE bucket=?", (bucket,))
    row = c.fetchone()
    if row:
        wins, losses, alpha, beta = row
        if win:
            wins += 1
            alpha += 1
        else:
            losses += 1
            beta += 1
        total = wins + losses
        wr = wins / total if total > 0 else 0.5
        c.execute("""
            UPDATE bayesian_buckets SET wins=?, losses=?, total=?, win_rate=?, alpha=?, beta=?
            WHERE bucket=?
        """, (wins, losses, total, wr, alpha, beta, bucket))
    else:
        wins = 1 if win else 0
        losses = 0 if win else 1
        total = 1
        wr = 1.0 if win else 0.0
        alpha = 2.0 if win else 1.0
        beta = 1.0 if win else 2.0
        c.execute("""
            INSERT INTO bayesian_buckets (bucket, wins, losses, total, win_rate, alpha, beta)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (bucket, wins, losses, total, wr, alpha, beta))
    conn.commit()
    conn.close()

def get_bucket_win_rate(bucket):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("SELECT win_rate, total FROM bayesian_buckets WHERE bucket=?", (bucket,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return 0.5, 0

def get_bayesian_confidence_modifier(signal_type, direction, phase):
    bucket = f"phase{phase}_{direction}_{signal_type}"
    wr, total = get_bucket_win_rate(bucket)
    if total < 3:
        return 0
    if wr > 0.58:
        return 5
    elif wr < 0.42:
        return -10
    elif wr > 0.55:
        return 3
    elif wr < 0.45:
        return -5
    return 0

# ── Binance WebSocket (trade + depth20 combined) ──────────
class BinanceWS:
    def __init__(self):
        self.url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        self.ws = None
        self.thread = None
        self.last_buf = 0
        self._reconnect_delay = 5
        self._max_reconnect_delay = 60

    def on_message(self, ws, message):
        global last_btc_price, price_buffer
        data = json.loads(message)
        price = float(data['p'])
        now = time.time()
        with price_lock:
            last_btc_price = price
            if now - self.last_buf >= 2:
                price_buffer.append((now, price))
                if len(price_buffer) > PRICE_BUFFER_SIZE:
                    price_buffer = price_buffer[-PRICE_BUFFER_SIZE:]
                self.last_buf = now

    def on_error(self, ws, error):
        log_to_file(f"Binance WS error: {error}", "WARN")

    def on_close(self, ws, code, msg):
        log_to_file(f"Binance WS closed (code={code}). Reconnecting...", "WARN")
        time.sleep(self._reconnect_delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
        self.run()

    def on_open(self, ws):
        log_to_file("Binance WS connected", "INFO")
        self._reconnect_delay = 5

    def run(self):
        self.ws = websocket.WebSocketApp(
            self.url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open
        )
        sslopt = {"cert_reqs": ssl.CERT_NONE}
        self.ws.run_forever(ping_interval=30, ping_timeout=10, sslopt=sslopt)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

# ── Chainlink RTDS WebSocket (Module 1) ───────────────────────
class ChainlinkWS:
    """
    Connects to Polymarket RTDS for Chainlink BTC/USD feed.
    The EXACT feed used for Polymarket settlement resolution.
    """
    def __init__(self):
        self.url = os.getenv("POLY_RTDS_WS_URL", "wss://rtds.polymarket.com/ws")
        self.ws = None
        self.thread = None
        self._reconnect_delay = 5
        self._max_reconnect_delay = 60

    def on_message(self, ws, message):
        global chainlink_price, last_chainlink_update
        try:
            data = json.loads(message)
            if data.get("topic") == "crypto_prices_chainlink":
                prices = data.get("data", {})
                btc_key = None
                for k in prices:
                    if "btc" in k.lower() or "BTC" in k:
                        btc_key = k
                        break
                if btc_key:
                    px = float(prices[btc_key])
                    with price_lock:
                        chainlink_price = px
                        last_chainlink_update = time.time()
        except Exception as e:
            pass

    def on_error(self, ws, error):
        log_to_file(f"Chainlink WS error: {error}", "WARN")

    def on_close(self, ws, code, msg):
        log_to_file(f"Chainlink WS closed (code={code}). Reconnecting...", "WARN")
        time.sleep(self._reconnect_delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
        self.run()

    def on_open(self, ws):
        log_to_file("Chainlink RTDS WS connected", "INFO")
        self._reconnect_delay = 5
        subscribe = json.dumps({
            "type": "subscribe",
            "topic": "crypto_prices_chainlink",
            "symbols": ["btc/usd"]
        })
        ws.send(subscribe)
        log_to_file("Subscribed to crypto_prices_chainlink btc/usd", "INFO")

    def run(self):
        self.ws = websocket.WebSocketApp(
            self.url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open
        )
        sslopt = {"cert_reqs": ssl.CERT_NONE}
        self.ws.run_forever(ping_interval=30, ping_timeout=10, sslopt=sslopt)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

# ── Order Book WebSocket (Module 3) ──────────────────────────
class OrderBookWS:
    """
    Binance depth20 stream for wall detection.
    """
    def __init__(self):
        self.url = "wss://stream.binance.com:9443/ws/btcusdt@depth20@100ms"
        self.ws = None
        self.thread = None
        self._reconnect_delay = 5
        self._max_reconnect_delay = 60
        self.last_update = 0

    def on_message(self, ws, message):
        global ob_bids, ob_asks
        try:
            data = json.loads(message)
            bids = [(float(b[0]), float(b[1])) for b in data.get("bids", [])]
            asks = [(float(a[0]), float(a[1])) for a in data.get("asks", [])]
            with price_lock:
                ob_bids = bids
                ob_asks = asks
                self.last_update = time.time()
        except Exception as e:
            pass

    def on_error(self, ws, error):
        log_to_file(f"OrderBook WS error: {error}", "WARN")

    def on_close(self, ws, code, msg):
        log_to_file(f"OrderBook WS closed (code={code}). Reconnecting...", "WARN")
        time.sleep(self._reconnect_delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
        self.run()

    def on_open(self, ws):
        log_to_file("OrderBook WS connected", "INFO")
        self._reconnect_delay = 5

    def run(self):
        self.ws = websocket.WebSocketApp(
            self.url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open
        )
        sslopt = {"cert_reqs": ssl.CERT_NONE}
        self.ws.run_forever(ping_interval=30, ping_timeout=10, sslopt=sslopt)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

# ── Module 3: Wall Detection ──────────────────────────────────
def calc_wall_ratio():
    with price_lock:
        bids = list(ob_bids)
        asks = list(ob_asks)
    if not bids or not asks:
        return 1.0, 0, 0
    top_bids = bids[:5]
    top_asks = asks[:5]
    bid_wall = max(s for _, s in top_bids) if top_bids else 0
    ask_wall = max(s for _, s in top_asks) if top_asks else 0
    if ask_wall == 0:
        return 3.0, bid_wall, 0
    ratio = bid_wall / ask_wall
    return ratio, bid_wall, ask_wall

def wall_signal(ratio):
    if ratio > 2.5:
        return "UP"
    elif ratio < 0.4:
        return "DOWN"
    return "NEUTRAL"

# ── Module 1: Oracle Monitor ──────────────────────────────────
def fetch_chainlink_fallback():
    """Fallback: read Chainlink BTC/USD from Polygon via web3."""
    global chainlink_price, last_chainlink_update
    try:
        from web3 import Web3
        rpc = os.getenv("POLYGON_RPC", "https://polygon.drpc.org")
        w3 = Web3(Web3.HTTPProvider(rpc))
        oracle_addr = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
        abi = '[{"inputs":[],"name":"latestRoundData","outputs":[{"internalType":"uint80","name":"roundId","type":"uint80"},{"internalType":"int256","name":"answer","type":"int256"},{"internalType":"uint256","name":"startedAt","type":"uint256"},{"internalType":"uint256","name":"updatedAt","type":"uint256"},{"internalType":"uint80","name":"answeredInRound","type":"uint80"}],"stateMutability":"view","type":"function"}]'
        contract = w3.eth.contract(address=Web3.to_checksum_address(oracle_addr), abi=json.loads(abi))
        _, answer, _, updated_at, _ = contract.functions.latestRoundData().call()
        px = answer / 1e8
        with price_lock:
            chainlink_price = float(px)
            last_chainlink_update = time.time()
        log_to_file(f"Chainlink fallback: BTC/USD = ${px:.2f}", "DEBUG")
    except Exception as e:
        log_to_file(f"Chainlink fallback failed: {e}", "WARN")

def calc_lag_score(bin_px, chain_px):
    if chain_px <= 0 or bin_px <= 0:
        return 0.0
    return abs(bin_px - chain_px) / chain_px * 100

def chainlink_stale_seconds():
    if last_chainlink_update == 0:
        return 999
    return time.time() - last_chainlink_update

# ── Module 1: Price-To-Beat ──────────────────────────────────
def get_price_to_beat(window_ts, condition_id=None):
    if condition_id:
        try:
            resp = requests.get(
                f"https://clob.polymarket.com/markets/{condition_id}", timeout=5
            )
            data = resp.json()
            if "line" in data:
                return float(data["line"])
        except:
            pass
    slug = f"btc-updown-5m-{window_ts}"
    try:
        resp = requests.get(
            f"https://polymarket.com/api/equity/price-to-beat/{slug}", timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            if "price" in data:
                return float(data["price"])
    except:
        pass
    try:
        resp = requests.get(
            f"https://api.binance.com/api/v3/klines"
            f"?symbol=BTCUSDT&interval=1m&startTime={window_ts * 1000}&limit=1",
            timeout=5
        )
        data = resp.json()
        if data and len(data) > 0:
            return float(data[0][1])
    except:
        pass
    try:
        resp = requests.get(
            f"https://min-api.cryptocompare.com/data/v2/histominute"
            f"?fsym=BTC&tsym=USD&limit=1&toTs={window_ts}",
            timeout=5
        )
        return float(resp.json()["Data"]["Data"][-1]["close"])
    except:
        with price_lock:
            return last_btc_price

def get_current_5min_ts():
    return (int(time.time()) // 300) * 300

def get_polymarket_market(slug):
    try:
        resp = requests.get(
            f"https://gamma-api.polymarket.com/markets?slug={slug}", timeout=5
        )
        data = resp.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
        return None
    except Exception as e:
        log_to_file(f"Gamma API Error: {e}")
        return None

def fetch_account_stats(address):
    global account_stats
    if not address:
        return
    try:
        resp = requests.get(
            f"https://gamma-api.polymarket.com/balances?address={address}", timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            for item in data:
                if item.get("asset") == "USDC" or "USDC" in str(item.get("asset", "")):
                    account_stats["balance"] = round(float(item.get("balance", 0)), 2)
                    break
        account_stats["last_updated"] = time.time()
    except Exception as e:
        log_to_file(f"Stats Fetch Error: {e}")

# ── Module 2: Entry Engine ────────────────────────────────────
def get_window_phase(time_remaining):
    if time_remaining > 250:
        return 1
    elif time_remaining > 100:
        return 2
    elif time_remaining > 30:
        return 3
    elif time_remaining > 5:
        return 4
    return 0

def calc_volatility():
    with price_lock:
        buf = list(price_buffer)
        current = last_btc_price
    if len(buf) < 10 or current == 0:
        return 0
    now = time.time()
    recent = [(t, p) for t, p in buf if t >= now - 60]
    if len(recent) < 5:
        return 0
    prices = [p for _, p in recent]
    spread_pct = (max(prices) - min(prices)) / current * 100
    return spread_pct

def calc_trend_bias():
    with price_lock:
        buf = list(price_buffer)
        current = last_btc_price
    if len(buf) < 30 or current == 0:
        return 0
    now = time.time()
    recent_120 = [(t, p) for t, p in buf if t >= now - 120]
    if len(recent_120) < 10:
        return 0
    avg_120 = sum(p for _, p in recent_120) / len(recent_120)
    return (current - avg_120) / avg_120 * 100

def calc_momentum_and_accel():
    with price_lock:
        buf = list(price_buffer)
        current = last_btc_price
    if len(buf) < 20 or current == 0:
        return 0, 0, 0
    prices = [p for _, p in buf]
    now = time.time()
    cutoff_60s = now - 60
    cutoff_30s = now - 30
    recent_60 = [(t, p) for t, p in buf if t >= cutoff_60s]
    recent_30 = [(t, p) for t, p in buf if t >= cutoff_30s]
    prev_30 = [(t, p) for t, p in buf if cutoff_60s <= t < cutoff_30s]
    if len(recent_60) < 2:
        return 0, 0, current
    price_60s_ago = recent_60[0][1] if recent_60 else current
    delta_price = current - price_60s_ago
    if len(recent_30) >= 2 and len(prev_30) >= 2:
        delta_recent_30 = recent_30[-1][1] - recent_30[0][1] if len(recent_30) >= 2 else 0
        delta_prev_30 = prev_30[-1][1] - prev_30[0][1] if len(prev_30) >= 2 else 0
        acceleration = delta_recent_30 - delta_prev_30
    else:
        acceleration = 0
    return delta_price, acceleration, current

def get_momentum_direction(delta_price, acceleration):
    if delta_price > 50 and acceleration > 0:
        return "UP", "strengthening"
    elif delta_price < -50 and acceleration < 0:
        return "DOWN", "strengthening"
    elif delta_price > 25:
        return "UP", "moderate"
    elif delta_price < -25:
        return "DOWN", "moderate"
    return "NEUTRAL", "weak"

# ── Legacy Signal Engine (kept for compatibility) ──────────
def calc_momentum_score(prices, seconds=60):
    if len(prices) < 10:
        return 0
    n = min(seconds // 2, len(prices))
    recent = prices[-n:]
    if len(recent) < 4:
        return 0
    half = len(recent) // 2
    first_half_avg = sum(recent[:half]) / half
    second_half_avg = sum(recent[half:]) / (len(recent) - half)
    velocity = (second_half_avg - first_half_avg) / first_half_avg * 100
    q = len(recent) // 4
    if q > 0:
        q2_avg = sum(recent[q:q*2]) / q if q*2 <= len(recent) else second_half_avg
        q3_avg = sum(recent[q*2:q*3]) / q if q*3 <= len(recent) else second_half_avg
        q4_avg = sum(recent[q*3:]) / (len(recent) - q*3) if len(recent) > q*3 else second_half_avg
        accel_late = q4_avg - q3_avg
        accel_early = q3_avg - q2_avg
        accel = (accel_late - accel_early) / first_half_avg * 100
    else:
        accel = 0
    score = (velocity * 3 + accel) / 4
    return max(-100, min(100, score * 500))

def calc_odds_edge(current_btc, price_to_beat, up_price, down_price):
    if not price_to_beat or not current_btc:
        return None, 0, "no_data"
    diff_pct = (current_btc - price_to_beat) / price_to_beat * 100
    true_prob_up = 1 / (1 + math.exp(-diff_pct * 8))
    true_prob_down = 1 - true_prob_up
    market_prob_up = up_price
    market_prob_down = down_price
    edge_up = true_prob_up - market_prob_up
    edge_down = true_prob_down - market_prob_down
    if edge_up > 0.04 and edge_up > edge_down:
        return "UP", round(edge_up * 100, 1), f"BTC {diff_pct:+.3f}% vs strike, UP mispriced by {edge_up:.3f}"
    elif edge_down > 0.04:
        return "DOWN", round(edge_down * 100, 1), f"BTC {diff_pct:+.3f}% vs strike, DOWN mispriced by {edge_down:.3f}"
    return None, 0, f"no_edge (diff={diff_pct:+.3f}%, edge_up={edge_up:.3f}, edge_down={edge_down:.3f})"

# ── Module 4: Arbitrage Engine ────────────────────────────
def check_arb(up_ask, down_ask):
    if up_ask <= 0 or down_ask <= 0:
        return None, 0.0
    total_cost = up_ask + down_ask
    if total_cost < 0.985:
        profit = 1.0 - total_cost
        return "ARB", profit
    return None, 0.0

def check_hedge_leg2(leg1_entry_price, opposite_ask):
    if leg1_entry_price <= 0 or opposite_ask <= 0:
        return False
    return (leg1_entry_price + opposite_ask) <= 0.99

# ── Outcome Tracking ──────────────────────────────────────
def check_outcomes(baselines):
    global bot_running, risk_manager
    with trades_lock:
        trades = safe_read_json(TRADES_PATH) or []
        updated = False
        now = time.time()
        for t in trades:
            if t.get("outcome") in ("win", "loss"):
                continue
            wts = t.get("window_ts", 0)
            resolve_ts = wts + 300
            if now < resolve_ts + 60:
                continue
            base = baselines.get(wts) or t.get("price_to_beat") or get_price_to_beat(wts)
            if not base:
                log_to_file(f"No baseline for window {wts}, skipping outcome check")
                continue
            resolve_open_price = None
            try:
                resp = requests.get(
                    f"https://api.binance.com/api/v3/klines"
                    f"?symbol=BTCUSDT&interval=1m&startTime={wts * 1000}&limit=1",
                    timeout=5
                )
                data = resp.json()
                if data and len(data) > 0:
                    resolve_open_price = float(data[0][1])
            except:
                pass
            if not resolve_open_price:
                try:
                    resp = requests.get(
                        f"https://min-api.cryptocompare.com/data/v2/histominute"
                        f"?fsym=BTC&tsym=USD&limit=1&toTs={wts + 60}",
                        timeout=5
                    )
                    cc_data = resp.json()["Data"]["Data"]
                    if cc_data and len(cc_data) > 0:
                        resolve_open_price = float(cc_data[-1]["open"])
                except:
                    continue
            if not resolve_open_price:
                continue
            direction = t.get("direction", "UP")
            win = (direction == "UP" and resolve_open_price >= base) or \
                  (direction == "DOWN" and resolve_open_price < base)
            t["outcome"] = "win" if win else "loss"
            t["resolve_price"] = resolve_open_price
            t["price_to_beat"] = base
            bet = float(t.get("bet_size", 2.0))
            token_price = float(t.get("token_price", 0.5))
            if win:
                pnl = round(bet * (1.0 / token_price - 1.0), 2)
            else:
                pnl = round(-bet, 2)
            t["pnl"] = pnl
            t["bet_usdc"] = bet
            t["contracts"] = round(bet / token_price, 4)
            updated = True
            log_to_file(
                f"{'WIN' if win else 'LOSS'}: {direction} | "
                f"Strike: ${base:.2f} -> Resolve: ${resolve_open_price:.2f} | "
                f"P&L: ${pnl:+.2f}"
            )

            # Update SQLite bayesian tracker
            slug = t.get("market_slug", "")
            signals = t.get("signals", {})
            bucket = f"phase{signals.get('phase', 'unknown')}_{direction}_{signals.get('signal_type', 'unknown')}"
            update_trade_outcome_db(slug, "win" if win else "loss", pnl, 1 if win else 0)
            update_bayesian_bucket(bucket, win)

            if risk_manager:
                risk_manager.record_outcome(win, bet, token_price)

        if updated:
            safe_write_json(TRADES_PATH, trades)

    if risk_manager and bot_running:
        ok, reason = risk_manager.check_recent_win_rate(trades)
        if not ok:
            bot_running = False
            log_to_file(f"AUTO-STOP triggered: {reason}")

# ── Signal Priority Stack (Module 6) ────────────────────
def evaluate_signal_stack(price_now, price_to_beat, up_bid, up_ask, down_bid, down_ask,
                          window_ts, time_remaining, chainlink_px, already_traded):
    """
    Priority order:
    1. Arb opportunity (yes_ask + no_ask < 0.985) -> ALWAYS take
    2. Hedge Leg 2 -> Execute if condition met
    3. Wall + Momentum agree (any phase 2-4) -> Core directional
    4. Wall bias alone (strong order book imbalance) -> Directional
    5. Oracle lag edge -> Trade divergence direction
    6. Momentum alone (no wall) -> Lower confidence
    7. No signal -> DO NOTHING
    """
    if already_traded:
        return None, 0, {}, "ALREADY_TRADED", 0

    delta_price, acceleration, _ = calc_momentum_and_accel()
    wall_ratio, bid_wall, ask_wall = calc_wall_ratio()
    wall_dir = wall_signal(wall_ratio)
    mom_dir, mom_strength = get_momentum_direction(delta_price, acceleration)
    phase = get_window_phase(time_remaining)
    lag_score = calc_lag_score(price_now, chainlink_px)

    bin_dir = None
    if lag_score > 0.15 and chainlink_px > 0:
        lag_diff = (price_now - chainlink_px) / chainlink_px * 100
        if lag_diff > 0.15:
            bin_dir = "UP"
        elif lag_diff < -0.15:
            bin_dir = "DOWN"

    priority = 0
    decision = None
    confidence = 0
    signal_type = "NONE"

    # Priority 1: Arb (always take)
    arb_dir, arb_profit = check_arb(up_ask, down_ask)
    if arb_dir:
        return "ARB", 100, {
            "arb_profit": round(arb_profit * 100, 2),
            "total_cost": round(up_ask + down_ask, 4),
            "phase": phase, "wall_ratio": round(wall_ratio, 2),
            "lag_score": round(lag_score, 2), "delta_price": round(delta_price, 2),
            "acceleration": round(acceleration, 2), "signal_type": "ARB",
            "priority": 1,
        }, "ARB", 5.0

    # Priority 2: Hedge Leg 2 (handled separately in bot loop)

    # Priority 3: Wall + Momentum agree (any phase 2-4)
    if priority < 3 and phase >= 2 and wall_dir != "NEUTRAL" and mom_dir == wall_dir:
        if time_remaining > 30:
            wall_strength = abs(wall_ratio - 1.0)
            ptb_diff = abs(price_now - price_to_beat) if price_to_beat else 0
            if phase >= 3 and ptb_diff > 30 and wall_strength > 1.0:
                priority = 3
                decision = mom_dir
                confidence = 80
                signal_type = "CORE_SNIPER"
            elif phase == 2 and ptb_diff > 15 and wall_strength > 0.5:
                priority = 3
                decision = mom_dir
                confidence = 75
                signal_type = "WALL_MOMENTUM"

    # Priority 4: Wall bias alone (strong order book, no momentum confirmation yet)
    if priority < 4 and phase >= 2 and wall_dir != "NEUTRAL" and time_remaining > 30:
        wall_strength = abs(wall_ratio - 1.0)
        if wall_strength > 1.5:
            ptb_diff = abs(price_now - price_to_beat) if price_to_beat else 0
            if ptb_diff < 100:
                # Don't fight strong momentum unless it's decelerating (reversal setup)
                momentum_opposes = (mom_dir != "NEUTRAL" and mom_dir != wall_dir)
                if momentum_opposes:
                    accel_supports_wall = (wall_dir == "UP" and acceleration > 0) or \
                                          (wall_dir == "DOWN" and acceleration < 0)
                    if not accel_supports_wall:
                        log_to_file(f"WALL BIAS SKIPPED: {wall_dir} vs {mom_dir} momentum (accel={acceleration:.1f})", "INFO")
                    else:
                        priority = 4
                        decision = wall_dir
                        confidence = 70
                        signal_type = "WALL_BIAS"
                        log_to_file(f"WALL BIAS REVERSAL: {decision} (ratio={wall_ratio:.2f}, accel={acceleration:.1f})", "INFO")
                else:
                    priority = 4
                    decision = wall_dir
                    confidence = 75
                    signal_type = "WALL_BIAS"
                    log_to_file(f"WALL BIAS: {decision} (ratio={wall_ratio:.2f})", "INFO")

    # Priority 5: Oracle lag edge
    if priority < 4 and bin_dir and 0.15 < lag_score <= 2.0:
        priority = 5
        decision = bin_dir
        confidence = 75
        signal_type = "ORACLE_LAG"

    # Priority 6: Momentum alone (no wall)
    if priority < 5 and phase >= 2 and mom_dir != "NEUTRAL" and time_remaining > 30:
        if mom_strength == "strengthening" or abs(delta_price) > 50:
            priority = 6
            decision = mom_dir
            confidence = 70
            signal_type = "MOMENTUM_ONLY"

    # Priority 7: Mean Reversion (fade overextended moves)
    if priority < 6 and phase >= 2 and abs(delta_price) > 80 and time_remaining > 45:
        reversion_dir = "DOWN" if delta_price > 80 else "UP"
        reversion_conf = min(75, 55 + abs(delta_price) * 0.15)
        if mom_strength == "weakening" or abs(acceleration) > 30:
            reversion_conf += 5
        if wall_dir == reversion_dir:
            reversion_conf += 5
        if phase >= 3:
            reversion_conf += 5
        priority = 7
        decision = reversion_dir
        confidence = min(85, int(reversion_conf))
        signal_type = "MEAN_REVERSION"

    if decision:
        bayes_mod = get_bayesian_confidence_modifier(signal_type, decision, phase)
        confidence = max(50, min(99, confidence + bayes_mod))
        return decision, confidence, {
            "phase": phase, "wall_ratio": round(wall_ratio, 2),
            "lag_score": round(lag_score, 2), "delta_price": round(delta_price, 2),
            "acceleration": round(acceleration, 2), "signal_type": signal_type,
            "priority": priority, "mom_dir": mom_dir, "wall_dir": wall_dir,
            "bin_dir": bin_dir, "mom_strength": mom_strength,
            "bayes_mod": bayes_mod,
        }, signal_type, priority

    return None, 0, {
        "phase": phase, "wall_ratio": round(wall_ratio, 2),
        "lag_score": round(lag_score, 2), "delta_price": round(delta_price, 2),
        "acceleration": round(acceleration, 2), "signal_type": "NONE",
        "priority": 0, "mom_dir": mom_dir, "wall_dir": wall_dir,
        "bin_dir": bin_dir, "mom_strength": mom_strength,
    }, "NONE", 0

# ── Trade Execution v6.0 (LIMIT orders, Module 6) ─────────
def execute_trade(direction, token_id, token_price, btc_price, slug,
                  window_ts, confidence, signals, cfg, client=None, market=None, price_to_beat=0):
    global risk_manager
    is_dry = cfg.get("dry_run", True)
    status = "simulated"
    order_id = "N/A"
    condition_id = market.get("conditionId") if market else None
    signal_type = signals.get("signal_type", "UNKNOWN")
    is_arb = signal_type == "ARB"
    is_high_conf = signal_type in ("CORE_SNIPER", "LATE_SNIPER", "ORACLE_LAG")

    base_bet = float(cfg.get("bet_size", 2.0))

    if risk_manager:
        allowed, risk_reason = risk_manager.can_trade(token_price)
        if not allowed:
            log_to_file(f"RISK_BLOCK: {risk_reason}")
            return {"blocked": True, "reason": risk_reason}
        final_bet = risk_manager.get_bet_size(base_bet, signals.get("edge", 0) / 100.0,
                                               confidence, is_high_conf, is_arb)
    else:
        final_bet = base_bet
    final_bet = max(final_bet, 1.0)  # Polymarket min order size

    if not is_dry and client:
        try:
            from py_clob_client_v2.clob_types import OrderType
            log_to_file(f"LIVE ORDER: {direction} ${final_bet} @ ${token_price:.3f}")

            if is_arb:
                # FOK for arb trades
                from py_clob_client_v2.clob_types import MarketOrderArgs
                log_to_file(f"ARB FOK ORDER: Both sides @ ${token_price:.3f}", "INFO")
                order_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=final_bet,
                    side="BUY",
                    price=0,
                )
                signed_order = client.create_market_order(order_args)
                resp = client.post_order(signed_order, OrderType.FOK)
                if resp and (isinstance(resp, dict) and "orderID" in resp):
                    order_id = resp.get("orderID", "N/A")
                    status = "placed_arb"
                    log_to_file(f"ARB ORDER PLACED (FOK): {direction} | ID: {order_id}")
                else:
                    status = "failed"
                    log_to_file(f"Arb order failed: {resp}")
            else:
                # FAK market order for directional trades
                from py_clob_client_v2.clob_types import MarketOrderArgs
                order_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=final_bet,
                    side="BUY",
                    price=0,
                )
                signed_order = client.create_market_order(order_args)
                resp = client.post_order(signed_order, OrderType.FAK)
                if resp and (isinstance(resp, dict) and "orderID" in resp):
                    order_id = resp.get("orderID", "N/A")
                    status = "placed"
                    trade_ids = resp.get("tradeIDs", [])
                    fill_log = f"ORDER PLACED (FAK): {direction} @ ${token_price:.3f} | ID: {order_id}"
                    if trade_ids:
                        fill_log += f" | fills: {len(trade_ids)}"
                    log_to_file(fill_log)
                else:
                    status = "failed"
                    log_to_file(f"FAK order failed: {resp}")

        except Exception as e:
            status = "error"
            log_to_file(f"Execution Error: {e}")

    delta_price, acceleration, _ = calc_momentum_and_accel()
    lag_score = calc_lag_score(btc_price, chainlink_price)
    wall_ratio, _, _ = calc_wall_ratio()
    phase = get_window_phase(300 - (int(time.time()) % 300))

    trade = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "window_ts": window_ts,
        "market_slug": slug,
        "direction": direction,
        "token_id": token_id,
        "token_price": token_price,
        "btc_price": btc_price,
        "price_to_beat": price_to_beat,
        "chainlink_price": chainlink_price,
        "confidence": confidence,
        "order_id": order_id,
        "signals": {
            **signals,
            "lag_score": round(lag_score, 2),
            "wall_ratio": round(wall_ratio, 2),
            "delta_price": round(delta_price, 2),
            "acceleration": round(acceleration, 2),
            "phase": phase,
            "signal_type": signal_type,
        },
        "bet_size": final_bet,
        "dry_run": is_dry,
        "status": status,
        "outcome": None,
        "pnl": None,
        "condition_id": condition_id
    }
    if is_dry:
        log_to_file(
            f"DRY TRADE: {direction} conf={confidence:.1f}% | "
            f"BTC ${btc_price:.2f} vs strike ${price_to_beat:.2f} | "
            f"token=${token_price:.3f} | bet=${final_bet:.2f} | type={signal_type}"
        )
    if status in ("placed", "placed_arb", "simulated"):
        with trades_lock:
            trades = safe_read_json(TRADES_PATH) or []
            trades.append(trade)
            safe_write_json(TRADES_PATH, trades)
        if risk_manager:
            risk_manager.record_trade(direction, final_bet, token_price)

        # Record to SQLite
        record_trade_db(
            slug, direction, token_price, btc_price, chainlink_price,
            price_to_beat, wall_ratio, lag_score, delta_price, acceleration,
            phase, signal_type, confidence
        )
    return {"blocked": False, "status": status, "order_id": order_id}

# ── Bot Main Loop v6.0 ───────────────────────────────────
def bot_loop():
    global bot_running, current_strategy_info, risk_manager, chainlink_price, last_chainlink_update
    load_dotenv(ENV_PATH)

    addr = os.getenv("POLY_WALLET_ADDRESS", "")
    if addr:
        fetch_account_stats(addr)

    cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True}
    risk_manager = RiskManager(cfg, BOT_DIR)
    if risk_manager.state.get("circuit_breaker_tripped"):
        log_to_file("WARNING: Circuit breaker is tripped. Reset via dashboard to trade.")

    init_db()
    log_to_file("PolyBot v6.0 ENGINE STARTING (All 6 Modules Active)")

    market_baselines = {}
    last_market_fetch = 0
    cached_market = None
    last_signal_check = 0
    last_outcome_check = time.time()
    last_chainlink_fallback = 0
    last_arb_check = time.time()
    hedge_state = {}
    cached_client = None
    cached_client_cfg = None

    while bot_running:
        try:
            cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True}
            now = time.time()
            window_ts = get_current_5min_ts()
            window_offset = int(now % 300)
            time_remaining = 300 - window_offset
            slug = f"btc-updown-5m-{window_ts}"

            if risk_manager:
                risk_manager.cfg = cfg

            # Chainlink fallback poll every 30s if WS not updating
            if chainlink_price == 0 or (now - last_chainlink_fallback > 30):
                last_chainlink_fallback = now
                if chainlink_price == 0 or (now - last_chainlink_update > 15):
                    threading.Thread(target=fetch_chainlink_fallback, daemon=True).start()

            if now - last_outcome_check > 120:
                threading.Thread(
                    target=check_outcomes, args=(dict(market_baselines),), daemon=True
                ).start()
                last_outcome_check = now

            if now - last_market_fetch > 30:
                market = get_polymarket_market(slug)
                last_market_fetch = now
                cached_market = market
            else:
                market = cached_market

            if not market:
                with strategy_lock:
                    current_strategy_info["status"] = "SCANNING for market..."
                time.sleep(5)
                continue

            outcomes = market.get("outcomePrices", [])
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except:
                    outcomes = [x.strip() for x in outcomes.strip("[]").split(",")]
            if len(outcomes) < 2:
                time.sleep(5)
                continue

            up_price = float(outcomes[0])
            down_price = float(outcomes[1])

            if window_ts not in market_baselines:
                line = get_price_to_beat(window_ts, market.get("conditionId"))
                market_baselines[window_ts] = line
                if line:
                    log_to_file(f"Strike Price Synced: ${line:.2f} | Up: {up_price:.3f} Down: {down_price:.3f}")

            price_to_beat = market_baselines.get(window_ts, 0)
            with price_lock:
                price_now = last_btc_price
                chainlink_px = chainlink_price

            # Initialize CLOB client
            client = None
            if not cfg.get("dry_run", True):
                cfg_key = json.dumps(cfg, sort_keys=True)
                if cached_client is None or cfg_key != cached_client_cfg:
                    try:
                        pk = os.getenv("POLY_PRIVATE_KEY")
                        addr = os.getenv("POLY_WALLET_ADDRESS")
                        if addr and (time.time() - account_stats["last_updated"] > 120):
                            threading.Thread(
                                target=fetch_account_stats, args=(addr,), daemon=True
                            ).start()
                        if pk and addr:
                            from py_clob_client_v2.client import ClobClient
                            from py_clob_client_v2.clob_types import ApiCreds
                            sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "3"))
                            deposit_addr = os.getenv("POLY_DEPOSIT_WALLET_ADDRESS") or addr
                            api_key = os.getenv("POLY_API_KEY")
                            api_secret = os.getenv("POLY_API_SECRET")
                            api_passphrase = os.getenv("POLY_API_PASSPHRASE")
                            if api_key and api_secret and api_passphrase:
                                creds = ApiCreds(
                                    api_key=api_key,
                                    api_secret=api_secret,
                                    api_passphrase=api_passphrase
                                )
                            else:
                                temp = ClobClient(
                                    host="https://clob.polymarket.com",
                                    chain_id=CHAIN_ID,
                                    key=pk,
                                    signature_type=sig_type,
                                    funder=deposit_addr,
                                )
                                creds = temp.create_or_derive_api_key()
                            cached_client = ClobClient(
                                host="https://clob.polymarket.com",
                                chain_id=CHAIN_ID,
                                key=pk,
                                creds=creds,
                                signature_type=sig_type,
                                funder=deposit_addr,
                            )
                            cached_client_cfg = cfg_key
                            log_to_file("ClobClient initialized")
                    except Exception as e:
                        log_to_file(f"Client init error: {e}")
                        cached_client = None
                client = cached_client
            else:
                cached_client = None
                cached_client_cfg = None

            with trades_lock:
                trades = safe_read_json(TRADES_PATH) or []
            already_traded = any(t.get("window_ts") == window_ts for t in trades)
            diff_pct = (price_now - price_to_beat) / price_to_beat * 100 if price_to_beat else 0
            delta_price, acceleration, _ = calc_momentum_and_accel()
            wall_ratio, bid_wall, ask_wall = calc_wall_ratio()
            lag_score = calc_lag_score(price_now, chainlink_px)
            stale_sec = chainlink_stale_seconds()
            phase = get_window_phase(time_remaining)

            # Get ask/bid prices from market for arb check
            up_bid = up_price * 0.995
            up_ask = up_price * 1.005
            down_bid = down_price * 0.995
            down_ask = down_price * 1.005
            try:
                tokens = market.get("tokens", [])
                if len(tokens) >= 2:
                    up_bid = float(tokens[0].get("best_bid", tokens[0].get("bid", up_bid)))
                    up_ask = float(tokens[0].get("best_ask", tokens[0].get("ask", up_ask)))
                    down_bid = float(tokens[1].get("best_bid", tokens[1].get("bid", down_bid)))
                    down_ask = float(tokens[1].get("best_ask", tokens[1].get("ask", down_ask)))
            except:
                pass

            if already_traded:
                with strategy_lock:
                    current_strategy_info.update({
                        "slug": slug, "price_to_beat": price_to_beat,
                        "current_diff": round(diff_pct, 3),
                        "time_remaining": time_remaining,
                        "up_price": up_price, "down_price": down_price,
                        "status": f"Traded ✓ | Waiting ({time_remaining}s)",
                        "confidence": 0, "signals": {},
                        "risk_status": "OK", "risk_reason": "",
                        "phase": phase, "wall_ratio": round(wall_ratio, 2),
                        "lag_score": round(lag_score, 2),
                    })
                time.sleep(2)
                continue

            # Phase 1: Observe only
            if phase == 1:
                with strategy_lock:
                    current_strategy_info.update({
                        "slug": slug, "price_to_beat": price_to_beat,
                        "current_diff": round(diff_pct, 3),
                        "time_remaining": time_remaining,
                        "up_price": up_price, "down_price": down_price,
                        "status": f"Phase 1: OBSERVE ({time_remaining}s remaining)",
                        "confidence": 0, "signals": {},
                        "risk_status": "OK", "risk_reason": "",
                        "phase": phase, "wall_ratio": round(wall_ratio, 2),
                        "lag_score": round(lag_score, 2),
                    })
                time.sleep(5)
                continue

            if now - last_signal_check >= SIGNAL_CHECK_INTERVAL:
                last_signal_check = now

                # ── Module 4: Arb check every 10s ─────────
                arb_dir, arb_profit = check_arb(up_ask, down_ask)

                # ── Module 4: Hedge Leg 2 check ──────────
                hedge_trigger = False
                for h_key, h_state in list(hedge_state.items()):
                    if time.time() - h_state.get("time", 0) > 280:
                        hedge_state.pop(h_key, None)
                        continue
                    if check_hedge_leg2(h_state["entry_price"], down_ask if h_state["direction"] == "UP" else up_ask):
                        hedge_trigger = True
                        log_to_file(f"HEDGE LEG 2 TRIGGERED for {h_key}", "INFO")

                # ── Signal Priority Stack ────────────────
                direction, confidence, signals, signal_type, priority = evaluate_signal_stack(
                    price_now, price_to_beat, up_bid, up_ask, down_bid, down_ask,
                    window_ts, time_remaining, chainlink_px, already_traded
                )

                # Risk check
                risk_status = "OK"
                risk_reason = ""
                if risk_manager:
                    target_price = up_price if direction == "UP" else down_price if direction else 0.5
                    _allowed, risk_reason = risk_manager.can_trade(
                        target_price, lag_score, stale_sec
                    )
                    if not _allowed and direction is not None:
                        risk_status = "BLOCKED"
                        log_to_file(f"RISK_BLOCK: {risk_reason}")
                        direction = None

                vol_pct = calc_volatility()
                trend_pct = calc_trend_bias()
                with strategy_lock:
                    current_strategy_info.update({
                        "slug": slug, "price_to_beat": price_to_beat,
                        "current_diff": round(diff_pct, 3),
                        "time_remaining": time_remaining,
                        "up_price": up_price, "down_price": down_price,
                        "edge": f"v6.0 | {direction or 'NONE'} {confidence:.1f}%",
                        "confidence": confidence,
                        "signals": signals,
                        "risk_status": risk_status,
                        "risk_reason": risk_reason,
                        "phase": phase,
                        "wall_ratio": round(wall_ratio, 2),
                        "lag_score": round(lag_score, 2),
                        "prioritized_signal": signal_type,
                        "volatility_pct": round(vol_pct, 2),
                        "trend_pct": round(trend_pct, 3),
                    })
                if int(now) % 60 < SIGNAL_CHECK_INTERVAL or signal_type != "NONE":
                    log_to_file(
                        f"BTC: ${price_now:.2f} | CL: ${chainlink_px:.2f} | "
                        f"Strike: ${price_to_beat:.2f} | Diff: {diff_pct:+.3f}% | "
                        f"Up: {up_price:.3f} Down: {down_price:.3f} | "
                        f"Phase: {phase} | {time_remaining}s | "
                        f"Wall: {wall_ratio:.2f} | Lag: {lag_score:.2f}% | "
                        f"Vol: {vol_pct:.2f}% | Trend: {trend_pct:+.3f}% | "
                        f"Signal: {signal_type} | Dir: {direction or 'NONE'} | "
                        f"Conf: {confidence:.1f}% | Risk: {risk_status} | "
                        f"Stale: {stale_sec:.0f}s"
                    )

                # ── Consecutive loss guard ────────────────
                if direction:
                    recent_same_dir = [t for t in trades
                                       if t.get("direction") == direction
                                       and t.get("window_ts", 0) < window_ts
                                       and t.get("window_ts", 0) >= window_ts - 900]
                    if recent_same_dir:
                        last_same = max(recent_same_dir, key=lambda t: t.get("window_ts", 0))
                        if last_same.get("outcome") is None and time.time() > last_same["window_ts"] + 360:
                            cl = last_same.get("chainlink_price", 0)
                            base = last_same.get("price_to_beat", 0)
                            if cl > 0 and base > 0:
                                last_dir = last_same["direction"]
                                won = (last_dir == "UP" and cl >= base) or (last_dir == "DOWN" and cl < base)
                                last_same["outcome"] = "win" if won else "loss"
                                last_same["resolve_price"] = cl
                                safe_write_json(TRADES_PATH, trades)
                        if last_same.get("outcome") == "loss":
                            log_to_file(f"CONSECUTIVE LOSS BLOCK: {direction} skipped (last {direction} was a loss)")
                            direction = None

                # ── Momentum consistency guard ──────────
                if direction:
                    if direction == "UP" and delta_price < -50:
                        log_to_file(f"MOMENTUM BLOCK: UP skipped (delta_price={delta_price:.1f} < -50)")
                        direction = None
                    elif direction == "DOWN" and delta_price > 50:
                        log_to_file(f"MOMENTUM BLOCK: DOWN skipped (delta_price={delta_price:.1f} > 50)")
                        direction = None

                # ── Volatility guard ─────────────────────
                if direction:
                    vol_pct = calc_volatility()
                    if vol_pct > 0.5:
                        log_to_file(f"VOLATILITY BLOCK: {direction} skipped (spread={vol_pct:.2f}% > 0.5%)")
                        direction = None

                # ── Trend bias filter ────────────────────
                if direction:
                    trend_pct = calc_trend_bias()
                    if direction == "UP" and trend_pct < -0.15:
                        log_to_file(f"TREND BLOCK: UP skipped (trend={trend_pct:.3f}% bearish)")
                        direction = None
                    elif direction == "DOWN" and trend_pct > 0.15:
                        log_to_file(f"TREND BLOCK: DOWN skipped (trend={trend_pct:.3f}% bullish)")
                        direction = None

                # ── Resolution Hunting (last 30s, extreme token prices) ──
                if not direction and not already_traded and time_remaining <= 30:
                    res_hunt_dir = None
                    if up_price <= 0.08:
                        res_hunt_dir = "UP"
                    elif down_price <= 0.08:
                        res_hunt_dir = "DOWN"
                    if res_hunt_dir:
                        direction = res_hunt_dir
                        confidence = 90
                        signal_type = "RESOLUTION_HUNT"
                        signals = {"signal_type": signal_type, "priority": 0, "phase": phase,
                                   "wall_ratio": round(wall_ratio, 2), "lag_score": round(lag_score, 2),
                                   "delta_price": round(delta_price, 2), "acceleration": round(acceleration, 2)}
                        log_to_file(f"RESOLUTION HUNT: {direction} at token ${up_price if direction=='UP' else down_price:.3f}", "INFO")

                # ── Execute Trade ────────────────────────
                if direction and risk_status == "OK" and not already_traded:
                    min_conf = float(cfg.get("min_confidence", 70))
                    if confidence < min_conf and signal_type != "ARB" and signal_type != "RESOLUTION_HUNT":
                        direction = None
                        with strategy_lock:
                            current_strategy_info["status"] = f"Low conf ({confidence:.0f}% < {min_conf:.0f}%)"
                    elif signal_type == "ARB":
                        clob_ids = market.get("clobTokenIds", "[]")
                        if isinstance(clob_ids, str):
                            try:
                                clob_ids = json.loads(clob_ids)
                            except:
                                clob_ids = []
                        tokens_list = market.get("tokens", [])
                        up_token_id = (clob_ids[0] if len(clob_ids) > 0
                                       else (tokens_list[0].get("tokenId") if tokens_list else None))
                        down_token_id = (clob_ids[1] if len(clob_ids) > 1
                                         else (tokens_list[1].get("tokenId") if len(tokens_list) > 1 else None))
                        log_to_file(f"ARB: Buying both sides @ up=${up_ask:.4f} down=${down_ask:.4f}", "INFO")
                        if up_token_id:
                            execute_trade(
                                "UP", up_token_id, up_ask, price_now,
                                slug, window_ts, 100, signals, cfg, client, market, price_to_beat
                            )
                        if down_token_id:
                            execute_trade(
                                "DOWN", down_token_id, down_ask, price_now,
                                slug, window_ts, 100, signals, cfg, client, market, price_to_beat
                            )
                    else:
                        clob_ids = market.get("clobTokenIds", "[]")
                        if isinstance(clob_ids, str):
                            try:
                                clob_ids = json.loads(clob_ids)
                            except:
                                clob_ids = []
                        tokens_list = market.get("tokens", [])
                        up_token_id = (clob_ids[0] if len(clob_ids) > 0
                                       else (tokens_list[0].get("tokenId") if tokens_list else None))
                        down_token_id = (clob_ids[1] if len(clob_ids) > 1
                                         else (tokens_list[1].get("tokenId") if len(tokens_list) > 1 else None))
                        target_token_id = up_token_id if direction == "UP" else down_token_id
                        target_price = up_price if direction == "UP" else down_price

                        if 0.48 < target_price < 0.52:
                            log_to_file(f"EDGE BLOCK: {direction} at ${target_price:.3f} too close to 0.50")
                            with strategy_lock:
                                current_strategy_info["status"] = f"No edge at ${target_price:.3f}"
                            direction = None
                        else:
                            edge_pct = abs(target_price - 0.50) * 100
                            log_to_file(f"EDGE: {direction} at ${target_price:.3f} ({edge_pct:.0f}% skew)", "INFO")

                        one_hour = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                        recent_trades = [t for t in trades if t.get("timestamp", "") > one_hour]
                        max_hour = cfg.get("max_trades_per_hour", 12)

                        if not direction:
                            with strategy_lock:
                                current_strategy_info["status"] = f"No edge at ${target_price:.3f}"
                        elif len(recent_trades) >= max_hour:
                            with strategy_lock:
                                current_strategy_info["status"] = f"Hourly limit ({len(recent_trades)}/{max_hour})"
                        elif not target_token_id:
                            with strategy_lock:
                                current_strategy_info["status"] = "No token ID found"
                        elif not client and not cfg.get("dry_run", True):
                            with strategy_lock:
                                current_strategy_info["status"] = "Waiting for live client..."
                        else:
                            with strategy_lock:
                                current_strategy_info["status"] = (
                                    f"ENTERING: {direction} conf={confidence:.1f}% | "
                                    f"type={signal_type} | token=${target_price:.3f} | "
                                    f"{time_remaining}s left"
                                )
                            log_to_file(
                                f"TRADE ENTRY: {direction} conf={confidence:.1f}% | "
                                f"type={signal_type} | BTC ${price_now:.2f} vs strike ${price_to_beat:.2f} | "
                                f"token=${target_price:.3f} | {time_remaining}s remaining"
                            )
                            execute_trade(
                                direction, target_token_id, target_price, price_now,
                                slug, window_ts, confidence, signals, cfg, client, market, price_to_beat
                            )
                else:
                    reasons = []
                    if not direction:
                        reasons.append(f"no_signal ({signal_type})")
                    elif risk_status != "OK":
                        reasons.append(risk_reason)
                    with strategy_lock:
                        current_strategy_info["status"] = f"Analyzing... ({'; '.join(reasons) or 'ok'})"

            if len(market_baselines) > 20:
                cutoff = window_ts - 3600
                market_baselines = {k: v for k, v in market_baselines.items() if k > cutoff}

            time.sleep(1)
        except Exception as e:
            log_to_file(f"Bot Loop Error: {e}")
            import traceback
            log_to_file(traceback.format_exc())
            time.sleep(3)

# ── API Routes (v6.0) ─────────────────────────────────
@app.route("/")
def index():
    dashboard_path = BOT_DIR / "index.html"
    if dashboard_path.exists():
        return dashboard_path.read_text(encoding="utf-8")
    return jsonify({"message": "PolyBot API v6.0", "endpoints": ["/status", "/health", "/stats", "/logs"]})

@app.route("/status")
def get_status():
    trades = safe_read_json(TRADES_PATH) or []
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = sum(1 for t in trades if t.get("outcome") == "loss")
    total_pnl = sum(float(t.get("pnl", 0) or 0) for t in trades if t.get("pnl") is not None)
    cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True}
    with price_lock:
        live_price = last_btc_price
        cl_px = chainlink_price
    with strategy_lock:
        info_snapshot = dict(current_strategy_info)
    wall_ratio, bid_wall, ask_wall = calc_wall_ratio()
    lag_score = calc_lag_score(live_price, cl_px)
    stale_sec = chainlink_stale_seconds()
    delta_price, acceleration, _ = calc_momentum_and_accel()
    phase = get_window_phase(info_snapshot.get("time_remaining", 0))
    resp = {
        "running": bot_running,
        "dry_run": cfg.get("dry_run", True),
        "btc_price": live_price,
        "chainlink_price": cl_px,
        "chainlink_stale_seconds": round(stale_sec, 1),
        "strategy": "PolyBot v6.0 (All 6 Modules)",
        "info": info_snapshot,
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "total_pnl": round(total_pnl, 2),
        "success_rate": round((wins / (wins + losses) * 100), 1) if (wins + losses) > 0 else 0,
        "uptime": str(datetime.now(timezone.utc) - start_time).split(".")[0],
        "account": dict(account_stats),
        "bet_size": float(cfg.get("bet_size", 2.0)),
        "wall_ratio": round(wall_ratio, 2),
        "bid_wall": bid_wall,
        "ask_wall": ask_wall,
        "lag_score": round(lag_score, 2),
        "phase": phase,
        "delta_price": round(delta_price, 2),
        "acceleration": round(acceleration, 2),
        "module_status": {
            "oracle_alignment": "ACTIVE" if cl_px > 0 else "INACTIVE",
            "entry_timing": f"Phase {phase}",
            "order_book": "ACTIVE" if len(ob_bids) > 0 else "INACTIVE",
            "arb_hedge": "ACTIVE",
            "risk_management": "ACTIVE" if risk_manager and risk_manager.enabled else "DISABLED",
            "execution": "LIVE" if not cfg.get("dry_run", True) else "DRY_RUN",
        }
    }
    if risk_manager:
        resp["risk"] = risk_manager.get_stats()
    return jsonify(resp)

@app.route("/stats")
def get_stats():
    trades = safe_read_json(TRADES_PATH) or []
    period = request.args.get("period", "all")
    now = datetime.now(timezone.utc)
    filtered = trades
    if period == "30m":
        cutoff = (now - timedelta(minutes=30)).isoformat()
        filtered = [t for t in trades if t.get("timestamp", "") > cutoff]
    elif period == "1h":
        cutoff = (now - timedelta(hours=1)).isoformat()
        filtered = [t for t in trades if t.get("timestamp", "") > cutoff]
    elif period == "24h":
        cutoff = (now - timedelta(hours=24)).isoformat()
        filtered = [t for t in trades if t.get("timestamp", "") > cutoff]
    wins = sum(1 for t in filtered if t.get("outcome") == "win")
    losses = sum(1 for t in filtered if t.get("outcome") == "loss")
    pnl = sum(float(t.get("pnl", 0) or 0) for t in filtered if t.get("pnl") is not None)
    return jsonify({
        "total_trades": len(filtered),
        "wins": wins,
        "losses": losses,
        "pnl": round(pnl, 2),
        "success_rate": round((wins / (wins + losses) * 100), 1) if (wins + losses) > 0 else 0,
        "history": filtered[-100:]
    })

@app.route("/health")
def health():
    with price_lock:
        price_ok = last_btc_price > 0
        cl_ok = chainlink_price > 0
    ws_ok = ws_client.thread is not None and ws_client.thread.is_alive()
    ob_ok = len(ob_bids) > 0
    healthy = price_ok and ws_ok
    return jsonify({
        "status": "healthy" if healthy else "degraded",
        "btc_feed": "up" if price_ok else "down",
        "chainlink_feed": "up" if cl_ok else "down",
        "orderbook_feed": "up" if ob_ok else "down",
        "websocket": "up" if ws_ok else "down",
        "bot_running": bot_running,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }), 200 if healthy else 503

@app.route("/risk")
def risk_status():
    if not risk_manager:
        return jsonify({"error": "Risk manager not initialized"}), 503
    return jsonify(risk_manager.get_stats())

@app.route("/export-trades")
def export_trades():
    fmt = request.args.get("format", "json").lower()
    trades = safe_read_json(TRADES_PATH) or []
    if fmt == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "timestamp", "window_ts", "market_slug", "direction", "token_price",
            "btc_price", "price_to_beat", "chainlink_price", "confidence",
            "bet_size", "dry_run", "status", "outcome", "pnl", "signal_type",
            "wall_ratio", "lag_score", "phase", "order_id"
        ])
        for t in trades:
            sigs = t.get("signals", {})
            writer.writerow([
                t.get("timestamp", ""),
                t.get("window_ts", ""),
                t.get("market_slug", ""),
                t.get("direction", ""),
                t.get("token_price", ""),
                t.get("btc_price", ""),
                t.get("price_to_beat", ""),
                t.get("chainlink_price", ""),
                t.get("confidence", ""),
                t.get("bet_size", ""),
                t.get("dry_run", ""),
                t.get("status", ""),
                t.get("outcome", ""),
                t.get("pnl", ""),
                sigs.get("signal_type", ""),
                sigs.get("wall_ratio", ""),
                sigs.get("lag_score", ""),
                sigs.get("phase", ""),
                t.get("order_id", "")
            ])
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=polybot_trades.csv"}
        )
    return jsonify({"trades": trades, "count": len(trades)})

@app.route("/bayesian")
def bayesian_stats():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("SELECT * FROM bayesian_buckets ORDER BY total DESC")
    rows = c.fetchall()
    conn.close()
    buckets = []
    for r in rows:
        buckets.append({
            "bucket": r[0], "wins": r[1], "losses": r[2],
            "total": r[3], "win_rate": round(r[4] * 100, 1),
            "alpha": r[5], "beta": r[6],
        })
    return jsonify({"buckets": buckets})

@app.route("/db-trades")
def db_trades():
    period = request.args.get("period", "all")
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    if period == "24h":
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        c.execute("SELECT * FROM trades WHERE timestamp > ? ORDER BY id DESC LIMIT 200", (cutoff,))
    elif period == "7d":
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        c.execute("SELECT * FROM trades WHERE timestamp > ? ORDER BY id DESC LIMIT 500", (cutoff,))
    else:
        c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 200")
    rows = c.fetchall()
    conn.close()
    columns = ["id", "timestamp", "slug", "direction", "entry_price", "btc_price_at_entry",
               "chainlink_price_at_entry", "ptb_at_entry", "wall_ratio", "lag_score",
               "momentum_delta", "acceleration", "phase", "signal_type", "confidence",
               "outcome", "pnl", "win"]
    trades_list = [dict(zip(columns, r)) for r in rows]
    return jsonify({"trades": trades_list, "count": len(trades_list)})

@app.route("/start", methods=["POST"])
@require_api_key
def start_bot():
    global bot_running, bot_thread
    if not bot_running:
        bot_running = True
        bot_thread = threading.Thread(target=bot_loop, daemon=True)
        bot_thread.start()
        return jsonify({"status": "started"})
    return jsonify({"status": "already_running"})

@app.route("/stop", methods=["POST"])
@require_api_key
def stop_bot():
    global bot_running
    bot_running = False
    return jsonify({"status": "stopped"})

@app.route("/restart", methods=["POST"])
@require_api_key
def restart_bot():
    global bot_running, bot_thread
    if not bot_running:
        return jsonify({"status": "not_running"})
    bot_running = False
    old_thread = bot_thread
    timeout = time.time() + 10
    while old_thread and old_thread.is_alive() and time.time() < timeout:
        time.sleep(0.2)
    bot_running = True
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    return jsonify({"status": "restarted"})

@app.route("/reset-risk", methods=["POST"])
@require_api_key
def reset_risk():
    if risk_manager:
        risk_manager.reset_all_blocks()
        log_to_file("Risk state fully reset via API (consecutive losses, win_rate_reduced, position_count)")
        return jsonify({"status": "reset", "message": "All risk blocks cleared"})
    return jsonify({"status": "no_risk_manager"}), 503

@app.route("/hard-reset", methods=["POST"])
@require_api_key
def hard_reset():
    if risk_manager:
        risk_manager.reset_all_blocks()
        if risk_manager.state_path.exists():
            risk_manager.state_path.unlink()
        risk_manager._load_state()
    trades_path = BOT_DIR / "trades.json"
    if trades_path.exists():
        safe_write_json(trades_path, [])
    log_to_file("HARD RESET: risk state + trades cleared via dashboard", "WARN")
    return jsonify({"status": "ok", "message": "Risk state and trades reset"})

@app.route("/config", methods=["GET", "POST"])
@require_api_key
def handle_config():
    if request.method == "POST":
        data = request.get_json()
        old_cfg = safe_read_json(CONFIG_PATH) or {}
        if old_cfg.get("dry_run", True) and not data.get("dry_run", True):
            if not data.get("_confirm_live", False):
                return jsonify({
                    "status": "blocked",
                    "message": "Set '_confirm_live': true to switch from dry_run to live trading"
                }), 403
        safe_write_json(CONFIG_PATH, data)
        return jsonify({"status": "saved"})
    return jsonify(safe_read_json(CONFIG_PATH) or {})

@app.route("/clear-trades", methods=["POST"])
@require_api_key
def clear_trades():
    try:
        if TRADES_PATH.exists():
            TRADES_PATH.unlink()
        # Also clear SQLite
        conn = sqlite3.connect(str(DB_PATH))
        c = conn.cursor()
        c.execute("DELETE FROM trades")
        c.execute("DELETE FROM bayesian_buckets")
        conn.commit()
        conn.close()
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route("/clear-logs", methods=["POST"])
@require_api_key
def clear_logs():
    try:
        if LOG_PATH.exists():
            LOG_PATH.unlink()
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route("/logs")
def get_logs():
    if not LOG_PATH.exists():
        return jsonify({"logs": []})
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return jsonify({"logs": [l.strip() for l in lines[-200:]]})
    except:
        return jsonify({"logs": []})

# ── WebSocket Clients ────────────────────────────────────
ws_client = BinanceWS()
ws_client.start()

cl_ws = ChainlinkWS()
cl_ws.start()

ob_ws = OrderBookWS()
ob_ws.start()

# ── Main ─────────────────────────────────────────────────
if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    app.run(host=host, port=3000)
