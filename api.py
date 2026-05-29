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

# ── Config Cache (set at top of each bot_loop iteration) ──
_cfg_cache = {"dry_run": True}

def cfg(key, default=None):
    return _cfg_cache.get(key, default)

def strat(key, default=None):
    return _cfg_cache.get("strategy", {}).get(key, default)

def module_enabled(name):
    return _cfg_cache.get("modules", {}).get(name, True)

def guard_enabled(name):
    return _cfg_cache.get("guards", {}).get(name, True)

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
candle_buffer = []
current_candle = None
candle_lock = threading.Lock()
account_stats = {"balance": 0.0, "pnl": 0.0, "last_updated": 0}
current_strategy_info = {
    "slug": "N/A", "price_to_beat": 0, "current_diff": 0,
    "time_remaining": 0, "up_price": 0, "down_price": 0,
    "edge": "None", "status": "Inactive", "confidence": 0, "signals": {},
    "risk_status": "OK", "risk_reason": "",
    "phase": 0, "wall_ratio": 0, "lag_score": 0,
    "prioritized_signal": "None",
}
risk_manager: 'RiskManager | None' = None
last_signal_guard = {"direction": None, "timestamp": 0}
window_best_signal = {}
_traded_windows = set()
_failed_window_attempts = {}

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
    min_trades = strat("bayesian_min_trades", 3)
    if total < min_trades:
        return 0
    bh_wr = strat("bayesian_boost_high_wr", 0.58)
    bh_amt = strat("bayesian_boost_high_amt", 5)
    bl_wr = strat("bayesian_boost_low_wr", 0.55)
    bl_amt = strat("bayesian_boost_low_amt", 3)
    ph_wr = strat("bayesian_penalty_high_wr", 0.42)
    ph_amt = strat("bayesian_penalty_high_amt", -10)
    pl_wr = strat("bayesian_penalty_low_wr", 0.45)
    pl_amt = strat("bayesian_penalty_low_amt", -5)
    if wr > bh_wr:
        return bh_amt
    elif wr < ph_wr:
        return ph_amt
    elif wr > bl_wr:
        return bl_amt
    elif wr < pl_wr:
        return pl_amt
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
        qty = float(data['q'])
        is_sell = data.get('m', False)
        now = time.time()
        with price_lock:
            last_btc_price = price
            if now - self.last_buf >= 2:
                price_buffer.append((now, price))
                max_buf = strat("max_price_buffer_size", PRICE_BUFFER_SIZE)
                if len(price_buffer) > max_buf:
                    price_buffer = price_buffer[-max_buf:]
                self.last_buf = now
        with candle_lock:
            update_candle(price, qty, is_sell, now)

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
    Disabled: Polymarket RTDS WebSocket (rtds.polymarket.com) has been deprecated.
    Chainlink price is now fetched exclusively via Web3 fallback from Polygon.
    """
    def __init__(self):
        self.url = None
        self.ws = None
        self.thread = None

    def on_message(self, ws, message):
        pass

    def on_error(self, ws, error):
        pass

    def on_close(self, ws, code, msg):
        pass

    def on_open(self, ws):
        pass

    def run(self):
        pass

    def start(self):
        log_to_file("Chainlink WS disabled (rtds.polymarket.com deprecated). Using Web3 fallback only.", "INFO")

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
    # Cap ratio to prevent spoofed walls from dominating signals
    r_max = strat("wall_ratio_max", 5.0)
    r_min = strat("wall_ratio_min", 0.2)
    ratio = min(ratio, r_max) if ratio > 1.0 else max(ratio, r_min)
    return ratio, bid_wall, ask_wall

def wall_signal(ratio):
    up_thresh = strat("wall_ratio_up_threshold", 2.5)
    down_thresh = strat("wall_ratio_down_threshold", 0.4)
    if ratio > up_thresh:
        return "UP"
    elif ratio < down_thresh:
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
        stale_limit = strat("price_to_beat_stale_threshold", 120)
        if chainlink_price > 0 and chainlink_stale_seconds() < stale_limit:
            return chainlink_price
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
    p1 = strat("phase1_min_seconds", 250)
    p2 = strat("phase2_min_seconds", 100)
    p3 = strat("phase3_min_seconds", 30)
    p4 = strat("phase4_min_seconds", 5)
    if time_remaining > p1:
        return 1
    elif time_remaining > p2:
        return 2
    elif time_remaining > p3:
        return 3
    elif time_remaining > p4:
        return 4
    return 0

def calc_volatility():
    with price_lock:
        buf = list(price_buffer)
        current = last_btc_price
    min_samp = strat("volatility_min_samples", 10)
    window = strat("volatility_window_seconds", 60)
    if len(buf) < min_samp or current == 0:
        return 0
    now = time.time()
    recent = [(t, p) for t, p in buf if t >= now - window]
    if len(recent) < 5:
        return 0
    prices = [p for _, p in recent]
    spread_pct = (max(prices) - min(prices)) / current * 100
    return spread_pct

# ── Candle Builder (from Binance trade stream) ───────────
def update_candle(price, qty, is_sell, ts):
    global current_candle, candle_buffer
    minute_start = int(ts // 60) * 60
    if current_candle is None or current_candle["start_ts"] != minute_start:
        if current_candle is not None:
            candle_buffer.append(current_candle)
            if len(candle_buffer) > 20:
                candle_buffer = candle_buffer[-20:]
        current_candle = {
            "start_ts": minute_start,
            "o": price, "h": price, "l": price, "c": price,
            "v": qty, "buy_v": 0 if is_sell else qty, "sell_v": qty if is_sell else 0,
        }
    else:
        c = current_candle
        c["h"] = max(c["h"], price)
        c["l"] = min(c["l"], price)
        c["c"] = price
        c["v"] += qty
        if is_sell:
            c["sell_v"] += qty
        else:
            c["buy_v"] += qty

# ── Volatility Engine ──────────────────────────────────
def calc_realized_vol():
    with candle_lock:
        candles = list(candle_buffer)
    if len(candles) < 3:
        return None
    closes = [c["c"] for c in candles[-10:]]
    log_rets = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0 and closes[i] > 0:
            log_rets.append(math.log(closes[i] / closes[i-1]))
    if len(log_rets) < 2:
        return None
    mean = sum(log_rets) / len(log_rets)
    variance = sum((r - mean) ** 2 for r in log_rets) / len(log_rets)
    period_vol_1m = math.sqrt(variance)
    period_vol_5m = period_vol_1m * math.sqrt(5)
    return period_vol_5m

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def fair_prob_up(current_price, strike_price, period_vol):
    if period_vol <= 0 or current_price <= 0:
        return 0.5
    diff = (current_price - strike_price) / current_price
    d = diff / period_vol
    return norm_cdf(d)

def calc_vol_edge(current_price, strike_price, up_market_price, down_market_price):
    period_vol = calc_realized_vol()
    if period_vol is None or period_vol <= 0:
        return None, 0, {}
    fp_up = fair_prob_up(current_price, strike_price, period_vol)
    fp_down = 1.0 - fp_up
    edge_up = fp_up - up_market_price
    edge_down = fp_down - down_market_price
    threshold = strat("vol_edge_threshold", 0.05)
    max_conf = strat("vol_edge_max_conf", 95)
    base_conf = strat("vol_edge_base_conf", 50)
    if edge_up > threshold and edge_up >= edge_down:
        return "UP", min(max_conf, int(base_conf + edge_up * 100)), {
            "fair_prob": round(fp_up, 3), "market_prob": round(up_market_price, 3),
            "edge": round(edge_up, 3), "period_vol": round(period_vol, 5),
        }
    if edge_down > threshold:
        return "DOWN", min(max_conf, int(base_conf + edge_down * 100)), {
            "fair_prob": round(fp_down, 3), "market_prob": round(down_market_price, 3),
            "edge": round(edge_down, 3), "period_vol": round(period_vol, 5),
        }
    return None, 0, {
        "fair_prob_up": round(fp_up, 3), "fair_prob_down": round(fp_down, 3),
        "market_up": round(up_market_price, 3), "market_down": round(down_market_price, 3),
        "max_edge": round(max(edge_up, edge_down), 3), "period_vol": round(period_vol, 5),
    }

# ── Funding Rate ───────────────────────────────────────
def get_funding_rate():
    try:
        resp = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
            timeout=5
        )
        data = resp.json()
        return float(data.get("lastFundingRate", 0))
    except:
        return None

def get_funding_bias(funding_rate):
    if funding_rate is None:
        return 0
    if funding_rate > 0.0001:
        return -1
    elif funding_rate < -0.0001:
        return 1
    return 0

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

def get_trend_direction():
    """Returns 'UP', 'DOWN', or 'NEUTRAL' based on 3m price trend."""
    with price_lock:
        buf = list(price_buffer)
        current = last_btc_price
    if len(buf) < 30 or current == 0:
        return "NEUTRAL"
    now = time.time()
    recent_180 = [(t, p) for t, p in buf if t >= now - 180]
    if len(recent_180) < 15:
        return "NEUTRAL"
    prices = [p for _, p in recent_180]
    sma_short = sum(prices[-5:]) / 5
    sma_long = sum(prices) / len(prices)
    delta = (sma_short - sma_long) / sma_long * 100
    if delta > 0.03:
        return "UP"
    elif delta < -0.03:
        return "DOWN"
    return "NEUTRAL"

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
    strong = strat("momentum_delta_strengthening", 50)
    moderate = strat("momentum_delta_moderate", 25)
    if delta_price > strong and acceleration > 0:
        return "UP", "strengthening"
    elif delta_price < -strong and acceleration < 0:
        return "DOWN", "strengthening"
    elif delta_price > moderate:
        return "UP", "moderate"
    elif delta_price < -moderate:
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

# ── Latency Arb (Binance moves first, Polymarket lags) ──
def detect_latency_arb(current_price, up_price, down_price, time_remaining):
    with price_lock:
        buf = list(price_buffer)
    now = time.time()
    cutoff_30 = now - 30
    cutoff_60 = now - 60
    recent_30 = [(t, p) for t, p in buf if t >= cutoff_30]
    recent_60 = [(t, p) for t, p in buf if cutoff_60 <= t < cutoff_30]
    if len(recent_30) < 3 or len(recent_60) < 3:
        return None, 0
    p30 = recent_30[-1][1]
    p60 = recent_60[0][1] if recent_60 else recent_30[0][1]
    if p60 <= 0:
        return None, 0
    move_pct = (p30 - p60) / p60 * 100
    min_move = strat("latency_arb_move_pct", 0.3)
    max_token = strat("latency_arb_max_token_price", 0.70)
    base_conf = strat("latency_arb_base_conf", 60)
    conf_scale = strat("latency_arb_conf_per_pct", 50)
    if abs(move_pct) < min_move:
        return None, 0
    direction = "UP" if move_pct > 0 else "DOWN"
    opp_price = up_price if direction == "UP" else down_price
    if opp_price > max_token:
        return None, 0
    confidence = min(90, int(base_conf + abs(move_pct) * conf_scale))
    log_to_file(f"LATENCY_ARB: {direction} (BTC moved {move_pct:+.2f}%, token ${opp_price:.3f})", "INFO")
    return direction, confidence

# ── Window Delta Signal (trade gap from strike in final seconds) ──
def window_delta_signal(current_price, strike_price, up_price, down_price, time_remaining):
    t_max = strat("window_delta_time_max", 50)
    t_min = strat("window_delta_time_min", 10)
    min_delta = strat("window_delta_min_delta_pct", 0.2)
    max_token = strat("window_delta_max_token_price", 0.70)
    base_conf = strat("window_delta_base_conf", 50)
    conf_per = strat("window_delta_conf_per_pct", 15)
    if time_remaining > t_max or time_remaining < t_min:
        return None, 0
    if not strike_price or strike_price <= 0 or current_price <= 0:
        return None, 0
    delta_pct = abs(current_price - strike_price) / strike_price * 100
    if delta_pct < min_delta:
        return None, 0
    direction = "UP" if current_price > strike_price else "DOWN"
    opp_price = up_price if direction == "UP" else down_price
    if opp_price > max_token:
        return None, 0
    conf = min(85, int(base_conf + delta_pct * conf_per))
    log_to_file(f"WINDOW_DELTA: {direction} (delta={delta_pct:.2f}%, token ${opp_price:.3f})", "INFO")
    return direction, conf

# ── Cheap Side Reversal (buy the $0.15 side) ──
def cheap_side_reversal(up_price, down_price, delta_price, acceleration):
    threshold = strat("cheap_side_threshold", 0.85)
    accel_thresh = strat("cheap_side_accel_threshold", 10)
    conf_base = strat("cheap_side_conf_base", 60)
    conf_per = strat("cheap_side_conf_per_unit", 0.5)
    conf_max = strat("cheap_side_max_conf", 75)
    if up_price > threshold and delta_price > 0 and acceleration < -accel_thresh:
        conf = min(conf_max, int(conf_base + abs(acceleration) * conf_per))
        log_to_file(f"CHEAP_SIDE: DOWN at ${down_price:.3f} (UP=${up_price:.3f}, accel={acceleration:.1f})", "INFO")
        return "DOWN", conf
    if down_price > threshold and delta_price < 0 and acceleration > accel_thresh:
        conf = min(conf_max, int(conf_base + abs(acceleration) * conf_per))
        log_to_file(f"CHEAP_SIDE: UP at ${up_price:.3f} (DOWN=${down_price:.3f}, accel={acceleration:.1f})", "INFO")
        return "UP", conf
    return None, 0

# ── Volume Confirmation ──
def volume_confirmed():
    vol_ratio = strat("volume_ratio_threshold", 0.5)
    with price_lock:
        buf = list(price_buffer)
    now = time.time()
    recent = [(t, p) for t, p in buf if t >= now - 30]
    older = [(t, p) for t, p in buf if now - 120 <= t < now - 30]
    if len(older) < 5 or len(recent) < 3:
        return True
    vol_recent = len(recent)
    vol_older = len(older) / 3
    if vol_older <= 0:
        return True
    return vol_recent / vol_older >= vol_ratio

# ── Fee-Aware Edge Gate ──
def fee_aware_gate(direction, confidence, token_price):
    be_wr = token_price * 100
    pred_wr = confidence
    buffer = strat("fee_buffer_pp", 5)
    if pred_wr < be_wr + buffer:
        return False
    return True

# ── Signal Guard (120s same-direction cooldown) ──
def check_signal_guard(direction):
    global last_signal_guard
    now = time.time()
    cooldown = strat("signal_guard_cooldown", 120)
    if last_signal_guard["direction"] == direction and now - last_signal_guard["timestamp"] < cooldown:
        log_to_file(f"SIGNAL_GUARD: {direction} suppressed (same direction within {cooldown}s)", "INFO")
        return False
    return True

def update_signal_guard(direction):
    global last_signal_guard
    last_signal_guard["direction"] = direction
    last_signal_guard["timestamp"] = time.time()

# ── Module 4: Arbitrage Engine ────────────────────────────
def check_arb(up_ask, down_ask):
    if up_ask <= 0 or down_ask <= 0:
        return None, 0.0
    total_cost = up_ask + down_ask
    threshold = strat("arb_threshold", 0.985)
    if total_cost < threshold:
        profit = 1.0 - total_cost
        return "ARB", profit
    return None, 0.0

def check_hedge_leg2(leg1_entry_price, opposite_ask):
    if leg1_entry_price <= 0 or opposite_ask <= 0:
        return False
    threshold = strat("hedge_threshold", 0.99)
    return (leg1_entry_price + opposite_ask) <= threshold

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
                    f"?symbol=BTCUSDT&interval=1m&startTime={(wts + 300) * 1000}&limit=1",
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
                        f"?fsym=BTC&tsym=USD&limit=1&toTs={wts + 300}",
                        timeout=5
                    )
                    cc_data = resp.json()["Data"]["Data"]
                    if cc_data and len(cc_data) > 0:
                        resolve_open_price = float(cc_data[-1]["close"])
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
                risk_manager.release_position()

        if updated:
            safe_write_json(TRADES_PATH, trades)

    if risk_manager and bot_running:
        ok, reason = risk_manager.check_recent_win_rate(trades)
        if not ok:
            bot_running = False
            log_to_file(f"AUTO-STOP triggered: {reason}")

# ── Signal Priority Stack (Module 6) ────────────────────
def evaluate_signal_stack(price_now, price_to_beat, up_bid, up_ask, down_bid, down_ask,
                          window_ts, time_remaining, chainlink_px, already_traded,
                          up_price=0.5, down_price=0.5):
    """
    Priority order:
    1. ARB (yes_ask + no_ask < 0.985) -> risk-free
    2. Latency Arb (Binance moved >0.3%, Polymarket < $0.70) -> best entry
    3. Window Delta (T-50s to T-10s, delta > 0.2%, token < $0.70)
    4. Cheap Side (one side > $0.85, momentum decelerating)
    5. Vol Edge (fair prob vs market prob mismatch >5%)
    6. Wall + Momentum agree (phase 2-4)
    7. Wall bias alone (strong order book imbalance)
    8. Oracle lag edge
    9. Momentum alone (no wall)
    10. Mean Reversion (fade overextended moves)
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
    lag_min = strat("edge_lag_min", 0.15)
    if lag_score > lag_min and chainlink_px > 0:
        lag_diff = (price_now - chainlink_px) / chainlink_px * 100
        if lag_diff > lag_min:
            bin_dir = "UP"
        elif lag_diff < -lag_min:
            bin_dir = "DOWN"

    priority = 0
    decision = None
    confidence = 0
    signal_type = "NONE"
    vol_info = {}

    # Priority 1: Arb (always take)
    if module_enabled("signal_arb"):
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

    # Priority 1.5: Window Delta Override (T-10s, BTC > threshold from strike)
    if module_enabled("signal_delta_override") and time_remaining <= 10 and price_to_beat and price_to_beat > 0 and price_now > 0 and up_price > 0 and down_price > 0:
        delta_thresh = strat("delta_override_delta_threshold", 0.10)
        max_token = strat("delta_override_max_token_price", 0.80)
        base_conf = strat("delta_override_base_conf", 60)
        conf_scale = strat("delta_override_conf_scale", 50)
        delta_pct = abs(price_now - price_to_beat) / price_to_beat * 100
        if delta_pct > delta_thresh:
            override_dir = "UP" if price_now > price_to_beat else "DOWN"
            override_price = up_price if override_dir == "UP" else down_price
            if override_price > max_token:
                log_to_file(f"DELTA_OVERRIDE SKIP: {override_dir} at ${override_price:.3f} too expensive (need ≤${max_token:.2f})", "INFO")
            else:
                override_conf = min(85, int(base_conf + delta_pct * conf_scale))
                log_to_file(f"DELTA_OVERRIDE: {override_dir} (delta={delta_pct:.2f}%, token=${override_price:.3f})", "INFO")
                return override_dir, override_conf, {
                    "phase": phase, "wall_ratio": round(wall_ratio, 2),
                    "lag_score": round(lag_score, 2), "delta_price": round(delta_price, 2),
                    "acceleration": round(acceleration, 2), "signal_type": "DELTA_OVERRIDE",
                    "priority": 1.5, "delta_pct": round(delta_pct, 2),
                }, "DELTA_OVERRIDE", 1.5

    # Priority 2: Latency Arb (Binance moves first, Polymarket lags)
    if module_enabled("signal_latency_arb") and up_price > 0 and down_price > 0:
        lat_dir, lat_conf = detect_latency_arb(price_now, up_price, down_price, time_remaining)
        if lat_dir:
            avg_price = up_price if lat_dir == "UP" else down_price
            if fee_aware_gate(lat_dir, lat_conf, avg_price):
                log_to_file(f"LATENCY_ARB WIN: {lat_dir} conf={lat_conf}% at ${avg_price:.3f}", "INFO")
                return lat_dir, lat_conf, {
                    "phase": phase, "wall_ratio": round(wall_ratio, 2),
                    "lag_score": round(lag_score, 2), "delta_price": round(delta_price, 2),
                    "acceleration": round(acceleration, 2), "signal_type": "LATENCY_ARB",
                    "priority": 2, "mom_dir": mom_dir, "wall_dir": wall_dir,
                    "bin_dir": bin_dir, "mom_strength": mom_strength,
                }, "LATENCY_ARB", 2

    # Priority 3: Window Delta (trade gap from strike in final seconds)
    if module_enabled("signal_window_delta") and up_price > 0 and down_price > 0:
        wd_dir, wd_conf = window_delta_signal(price_now, price_to_beat, up_price, down_price, time_remaining)
        if wd_dir:
            avg_price = up_price if wd_dir == "UP" else down_price
            if fee_aware_gate(wd_dir, wd_conf, avg_price):
                log_to_file(f"WINDOW_DELTA WIN: {wd_dir} conf={wd_conf}% at ${avg_price:.3f}", "INFO")
                return wd_dir, wd_conf, {
                    "phase": phase, "wall_ratio": round(wall_ratio, 2),
                    "lag_score": round(lag_score, 2), "delta_price": round(delta_price, 2),
                    "acceleration": round(acceleration, 2), "signal_type": "WINDOW_DELTA",
                    "priority": 3, "mom_dir": mom_dir, "wall_dir": wall_dir,
                    "bin_dir": bin_dir, "mom_strength": mom_strength,
                }, "WINDOW_DELTA", 3

    # Priority 4: Cheap Side Reversal (buy the $0.15 side)
    if module_enabled("signal_cheap_side"):
        cs_dir, cs_conf = cheap_side_reversal(up_price, down_price, delta_price, acceleration)
        if cs_dir:
            avg_price = up_price if cs_dir == "UP" else down_price
            if fee_aware_gate(cs_dir, cs_conf, avg_price):
                log_to_file(f"CHEAP_SIDE WIN: {cs_dir} conf={cs_conf}% at ${avg_price:.3f}", "INFO")
                return cs_dir, cs_conf, {
                    "phase": phase, "wall_ratio": round(wall_ratio, 2),
                    "lag_score": round(lag_score, 2), "delta_price": round(delta_price, 2),
                    "acceleration": round(acceleration, 2), "signal_type": "CHEAP_SIDE",
                    "priority": 4, "mom_dir": mom_dir, "wall_dir": wall_dir,
                    "bin_dir": bin_dir, "mom_strength": mom_strength,
                }, "CHEAP_SIDE", 4

    # Priority 5: Vol Edge (systematic mispricing via realized vol)
    if priority < 5 and module_enabled("signal_vol_edge"):
        up_mid = (up_bid + up_ask) / 2
        down_mid = (down_bid + down_ask) / 2
        vol_dir, vol_conf, vol_info = calc_vol_edge(price_now, price_to_beat, up_mid, down_mid)
        if vol_dir:
            priority = 5
            decision = vol_dir
            confidence = vol_conf
            signal_type = "VOL_EDGE"

    # Priority 6: Wall + Momentum agree (any phase 2-4)
    if priority < 6 and phase >= 2 and wall_dir != "NEUTRAL" and mom_dir == wall_dir and module_enabled("signal_wall_momentum"):
        if time_remaining > 30:
            wall_strength = abs(wall_ratio - 1.0)
            ptb_diff = abs(price_now - price_to_beat) if price_to_beat else 0
            if phase >= 3 and ptb_diff > 30 and wall_strength > 1.0:
                priority = 6
                decision = mom_dir
                confidence = 80
                signal_type = "CORE_SNIPER"
            elif phase == 2 and ptb_diff > 15 and wall_strength > 0.5:
                priority = 6
                decision = mom_dir
                confidence = 75
                signal_type = "WALL_MOMENTUM"

    # Priority 7: Wall bias alone (strong order book, no momentum confirmation yet)
    if priority < 7 and phase >= 2 and wall_dir != "NEUTRAL" and time_remaining > 30 and module_enabled("signal_wall_bias"):
        wall_strength = abs(wall_ratio - 1.0)
        if wall_strength > 1.5:
            ptb_diff = abs(price_now - price_to_beat) if price_to_beat else 0
            if ptb_diff < 100:
                momentum_opposes = (mom_dir != "NEUTRAL" and mom_dir != wall_dir)
                if momentum_opposes:
                    accel_supports_wall = (wall_dir == "UP" and acceleration > 0) or \
                                          (wall_dir == "DOWN" and acceleration < 0)
                    if not accel_supports_wall:
                        log_to_file(f"WALL BIAS SKIPPED: {wall_dir} vs {mom_dir} momentum (accel={acceleration:.1f})", "INFO")
                    else:
                        priority = 7
                        decision = wall_dir
                        confidence = 70
                        signal_type = "WALL_BIAS"
                        log_to_file(f"WALL BIAS REVERSAL: {decision} (ratio={wall_ratio:.2f}, accel={acceleration:.1f})", "INFO")
                else:
                    priority = 7
                    decision = wall_dir
                    confidence = 75
                    signal_type = "WALL_BIAS"
                    log_to_file(f"WALL BIAS: {decision} (ratio={wall_ratio:.2f})", "INFO")

    # Priority 8: Oracle lag edge
    if priority < 8 and bin_dir and module_enabled("signal_oracle_lag"):
        edge_lag_max = strat("edge_lag_max", 2.0)
        if lag_min < lag_score <= edge_lag_max:
            priority = 8
            decision = bin_dir
            confidence = 75
            signal_type = "ORACLE_LAG"

    # Priority 9: Momentum alone (no wall)
    if priority < 8 and phase >= 2 and mom_dir != "NEUTRAL" and time_remaining > 30 and module_enabled("signal_momentum_only"):
        if mom_strength == "strengthening" or abs(delta_price) > 50:
            priority = 9
            decision = mom_dir
            confidence = 70
            signal_type = "MOMENTUM_ONLY"

    # Priority 10: Mean Reversion (fade overextended moves)
    if priority < 9 and phase >= 2 and abs(delta_price) > 80 and time_remaining > 45 and module_enabled("signal_mean_reversion"):
        reversion_dir = "DOWN" if delta_price > 80 else "UP"
        reversion_conf = min(75, 55 + abs(delta_price) * 0.15)
        if mom_strength == "weakening" or abs(acceleration) > 30:
            reversion_conf += 5
        if wall_dir == reversion_dir:
            reversion_conf += 5
        if phase >= 3:
            reversion_conf += 5
        priority = 10
        decision = reversion_dir
        confidence = min(85, int(reversion_conf))
        signal_type = "MEAN_REVERSION"

    if decision:
        # Trend alignment filter: reduce confidence if trading against the trend
        trend_penalty = strat("trend_mismatch_penalty", 15)
        trend_bonus = strat("trend_match_bonus", 5)
        trend_dir = get_trend_direction()
        if trend_dir != "NEUTRAL" and decision != trend_dir:
            confidence = max(50, confidence - trend_penalty)
            log_to_file(f"TREND_MISMATCH: {decision} vs trend {trend_dir} — confidence reduced", "INFO")
        elif trend_dir == decision:
            confidence = min(99, confidence + trend_bonus)
        # Momentum opposition filter: don't trade against strong momentum
        mom_opp_delta = strat("momentum_opposition_delta", 30)
        mom_opp_cap = strat("momentum_opposition_cap", 65)
        if (decision == "UP" and delta_price < -mom_opp_delta) or (decision == "DOWN" and delta_price > mom_opp_delta):
            confidence = max(50, min(confidence, mom_opp_cap))
            log_to_file(f"MOMENTUM_BLOCK: {decision} but delta={delta_price:.1f} — confidence capped", "INFO")

        bayes_mod = get_bayesian_confidence_modifier(signal_type, decision, phase)
        confidence = max(50, min(99, confidence + bayes_mod))
        return decision, confidence, {
            "phase": phase, "wall_ratio": round(wall_ratio, 2),
            "lag_score": round(lag_score, 2), "delta_price": round(delta_price, 2),
            "acceleration": round(acceleration, 2), "signal_type": signal_type,
            "priority": priority, "mom_dir": mom_dir, "wall_dir": wall_dir,
            "bin_dir": bin_dir, "mom_strength": mom_strength,
            "bayes_mod": bayes_mod,
        } | (vol_info if signal_type == "VOL_EDGE" else {}), signal_type, priority

    return None, 0, {
        "phase": phase, "wall_ratio": round(wall_ratio, 2),
        "lag_score": round(lag_score, 2), "delta_price": round(delta_price, 2),
        "acceleration": round(acceleration, 2), "signal_type": "NONE",
        "priority": 0, "mom_dir": mom_dir, "wall_dir": wall_dir,
        "bin_dir": bin_dir, "mom_strength": mom_strength,
    } | vol_info, "NONE", 0

# ── Trade Execution v6.0 (LIMIT orders, Module 6) ─────────
def execute_trade(direction, token_id, token_price, btc_price, slug,
                  window_ts, confidence, signals, cfg, client=None, market=None, price_to_beat=0):
    global risk_manager, _traded_windows, _failed_window_attempts
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

    final_bet = base_bet

    # Cap bet to actual balance (leave $0.01 buffer)
    real_balance = account_stats.get("balance", 0)
    if not is_dry and real_balance > 0:
        max_afford = max(0.5, real_balance - 0.01)
        final_bet = min(final_bet, max_afford)
        if final_bet < 0.5:
            log_to_file(f"BALANCE_BLOCK: balance=${real_balance:.2f} too low for $0.50 min bet", "WARN")
            return {"blocked": True, "reason": f"balance ${real_balance:.2f} too low"}

    if not is_dry and client:
        try:
            from py_clob_client_v2.clob_types import OrderType
            log_to_file(f"LIVE ORDER: {direction} ${final_bet} @ ${token_price:.3f}")

            markup = strat("order_price_markup", 1.05)
            max_price = strat("max_order_price", 0.99)
            order_price = round(min(token_price * markup, max_price), 2)

            if is_arb:
                # FOK for arb trades
                from py_clob_client_v2.clob_types import MarketOrderArgs
                log_to_file(f"ARB FOK ORDER: Both sides @ ${token_price:.3f}", "INFO")
                order_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=final_bet,
                    side="BUY",
                    price=order_price,
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
                    price=order_price,
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
        # Track in-memory to prevent duplicate entries
        _traded_windows.add(window_ts)
        _failed_window_attempts.pop(window_ts, None)
    else:
        # Track failure count per window to limit retry spam
        _failed_window_attempts[window_ts] = _failed_window_attempts.get(window_ts, 0) + 1
    return {"blocked": False, "status": status, "order_id": order_id}

# ── Bot Main Loop v6.0 ───────────────────────────────────
def bot_loop():
    global bot_running, current_strategy_info, risk_manager, chainlink_price, last_chainlink_update
    global _traded_windows, _failed_window_attempts, _cfg_cache
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
            _cfg_cache = cfg
            now = time.time()
            window_ts = get_current_5min_ts()
            window_offset = int(now % 300)
            time_remaining = 300 - window_offset
            slug = f"btc-updown-5m-{window_ts}"

            if risk_manager:
                risk_manager.cfg = cfg
                risk_manager._load_cfg()

            # Chainlink fallback poll every 10s (WS disabled, this is the only source)
            cl_fb_int = strat("chainlink_fallback_interval", 10)
            cl_fb_stale = strat("chainlink_fallback_stale_threshold", 10)
            outcome_int = strat("outcome_check_interval", 120)
            market_int = strat("market_fetch_interval", 30)
            balance_int = strat("balance_fetch_interval", 30)

            if chainlink_price == 0 or (now - last_chainlink_fallback > cl_fb_int):
                last_chainlink_fallback = now
                if chainlink_price == 0 or (now - last_chainlink_update > cl_fb_stale):
                    threading.Thread(target=fetch_chainlink_fallback, daemon=True).start()

            if now - last_outcome_check > outcome_int:
                threading.Thread(
                    target=check_outcomes, args=(dict(market_baselines),), daemon=True
                ).start()
                last_outcome_check = now

            if now - last_market_fetch > market_int:
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
                window_best_signal.clear()
                line = get_price_to_beat(window_ts, market.get("conditionId"))
                market_baselines[window_ts] = line
                if line:
                    log_to_file(f"Strike Price Synced: ${line:.2f} | Up: {up_price:.3f} Down: {down_price:.3f}")

            price_to_beat = market_baselines.get(window_ts, 0)
            with price_lock:
                price_now = last_btc_price
                chainlink_px = chainlink_price

            # Fetch account balance periodically
            addr = os.getenv("POLY_WALLET_ADDRESS", "")
            bal_int = strat("balance_fetch_interval", 30)
            if addr and (time.time() - account_stats["last_updated"] > bal_int):
                threading.Thread(
                    target=fetch_account_stats, args=(addr,), daemon=True
                ).start()

            # Initialize CLOB client
            client = None
            if not cfg.get("dry_run", True):
                cfg_key = json.dumps(cfg, sort_keys=True)
                if cached_client is None or cfg_key != cached_client_cfg:
                    try:
                        pk = os.getenv("POLY_PRIVATE_KEY")
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
            already_traded = any(
                t.get("window_ts") == window_ts and t.get("market_slug") == slug
                for t in trades
            ) or window_ts in _traded_windows

            # Limit retry spam: skip if we've already failed this window repeatedly
            if not already_traded:
                late_thresh = strat("retry_spam_late_threshold", 10)
                max_attempts_late = strat("max_retry_attempts_late", 5)
                max_attempts_normal = strat("max_retry_attempts_normal", 2)
                max_attempts = max_attempts_late if time_remaining <= late_thresh else max_attempts_normal
                if _failed_window_attempts.get(window_ts, 0) >= max_attempts:
                    already_traded = True
            diff_pct = (price_now - price_to_beat) / price_to_beat * 100 if price_to_beat else 0
            delta_price, acceleration, _ = calc_momentum_and_accel()
            wall_ratio, bid_wall, ask_wall = calc_wall_ratio()
            lag_score = calc_lag_score(price_now, chainlink_px)
            stale_sec = chainlink_stale_seconds()
            phase = get_window_phase(time_remaining)

            # Get ask/bid prices from market for arb check
            arb_bid_markup = 0.995
            arb_ask_markup = 1.005
            up_bid = up_price * arb_bid_markup
            up_ask = up_price * arb_ask_markup
            down_bid = down_price * arb_bid_markup
            down_ask = down_price * arb_ask_markup
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

            check_int_late = strat("signal_check_interval_late", 2)
            check_int_mid = strat("signal_check_interval_mid", 5)
            check_interval = check_int_late if time_remaining <= 10 else (check_int_mid if time_remaining <= 30 else SIGNAL_CHECK_INTERVAL)
            if time_remaining > 5 and now - last_signal_check >= check_interval:
                last_signal_check = now
            elif time_remaining <= 5:
                pass  # T-5s hard deadline below handles this

            # ── Module 4: Arb check every 10s ─────────
            arb_dir, arb_profit = check_arb(up_ask, down_ask)

            # ── Module 4: Hedge Leg 2 check ──────────
            hedge_trigger = False
            hedge_max_age = strat("market_baseline_max_age", 280)
            for h_key, h_state in list(hedge_state.items()):
                if time.time() - h_state.get("time", 0) > hedge_max_age:
                    hedge_state.pop(h_key, None)
                    continue
                if check_hedge_leg2(h_state["entry_price"], down_ask if h_state["direction"] == "UP" else up_ask):
                    hedge_trigger = True
                    log_to_file(f"HEDGE LEG 2 TRIGGERED for {h_key}", "INFO")

            # ── Signal Priority Stack ────────────────
            direction, confidence, signals, signal_type, priority = evaluate_signal_stack(
                price_now, price_to_beat, up_bid, up_ask, down_bid, down_ask,
                window_ts, time_remaining, chainlink_px, already_traded,
                up_price=up_price, down_price=down_price
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

            # ── Real balance vs tracked bankroll check ──
            if risk_manager:
                real_balance = account_stats.get("balance", 0)
                if real_balance > 0:
                    tracked = risk_manager.state["current_bankroll"]
                    if abs(tracked - real_balance) > 1.0:
                        log_to_file(f"BALANCE_SYNC: tracked=${tracked:.2f} -> actual=${real_balance:.2f}", "WARN")
                        risk_manager.state["current_bankroll"] = real_balance
                        risk_manager.state["peak_bankroll"] = max(risk_manager.state["peak_bankroll"], real_balance)
                        risk_manager._save_state()

            # ── Consecutive loss guard ────────────────
            if direction and guard_enabled("consecutive_loss_guard"):
                lookback = strat("consecutive_same_dir_lookback", 900)
                resolve_thresh = strat("consecutive_same_dir_resolve_threshold", 360)
                recent_same_dir = [t for t in trades
                                   if t.get("direction") == direction
                                   and t.get("window_ts", 0) < window_ts
                                   and t.get("window_ts", 0) >= window_ts - lookback]
                if recent_same_dir:
                    last_same = max(recent_same_dir, key=lambda t: t.get("window_ts", 0))
                    if last_same.get("outcome") is None and time.time() > last_same["window_ts"] + resolve_thresh:
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
            if direction and guard_enabled("momentum_consistency"):
                mum_block = strat("momentum_block_delta", 20)
                if direction == "UP" and delta_price < -mum_block:
                    log_to_file(f"MOMENTUM BLOCK: UP skipped (delta_price={delta_price:.1f} < -{mum_block})")
                    direction = None
                elif direction == "DOWN" and delta_price > mum_block:
                    log_to_file(f"MOMENTUM BLOCK: DOWN skipped (delta_price={delta_price:.1f} > {mum_block})")
                    direction = None

            # ── Volatility guard ─────────────────────
            if direction and guard_enabled("volatility_guard"):
                vol_block = strat("volatility_block_spread_pct", 0.5)
                vol_pct = calc_volatility()
                if vol_pct > vol_block:
                    log_to_file(f"VOLATILITY BLOCK: {direction} skipped (spread={vol_pct:.2f}% > {vol_block:.2f}%)")
                    direction = None

            # ── Volume confirmation ────────────────────
            if direction and signal_type not in ("ARB", "RESOLUTION_HUNT", "DELTA_OVERRIDE") and time_remaining > 5 and guard_enabled("volume_confirmation"):
                if not volume_confirmed():
                    log_to_file(f"VOLUME_BLOCK: {direction} skipped (volume too low)", "INFO")
                    direction = None

            # ── Signal guard (same-direction cooldown) ──
            if direction and signal_type not in ("ARB", "RESOLUTION_HUNT", "DELTA_OVERRIDE") and time_remaining > 5 and guard_enabled("signal_guard"):
                if not check_signal_guard(direction):
                    direction = None

            # ── Stale feed guard (double-check) ──────
            if direction and time_remaining > 5 and guard_enabled("stale_feed_guard"):
                stale_sec = chainlink_stale_seconds()
                stale_limit = _cfg_cache.get("risk_management", {}).get("stale_feed_seconds", 60)
                if stale_sec > stale_limit:
                    log_to_file(f"STALE_BLOCK: {direction} skipped (chainlink stale {stale_sec:.0f}s > {stale_limit}s)")
                    direction = None

            # ── Trend bias filter ────────────────────
            if direction and time_remaining > 5 and guard_enabled("trend_bias_filter"):
                trend_thresh = strat("trend_bias_threshold", 0.15)
                trend_pct = calc_trend_bias()
                if direction == "UP" and trend_pct < -trend_thresh:
                    log_to_file(f"TREND BLOCK: UP skipped (trend={trend_pct:.3f}% bearish)")
                    direction = None
                elif direction == "DOWN" and trend_pct > trend_thresh:
                    log_to_file(f"TREND BLOCK: DOWN skipped (trend={trend_pct:.3f}% bullish)")
                    direction = None

            # ── Best signal tracking ──────────────────────
            if direction and signal_type not in ("DELTA_OVERRIDE", "ARB", "RESOLUTION_HUNT"):
                tracked_priority = 999 if signal_type in ("DELTA_OVERRIDE", "ARB") else (priority or 0)
                track_key = f"{direction}_{signal_type}"
                existing = window_best_signal.get(track_key, {})
                if tracked_priority > existing.get("priority", -1):
                    window_best_signal[track_key] = {
                        "direction": direction, "confidence": confidence,
                        "signal_type": signal_type, "signals": signals,
                        "priority": tracked_priority,
                    }

            # ── Resolution Hunting (last 30s, extreme token prices) ──
            if not direction and not already_traded and time_remaining <= 30 and module_enabled("resolution_hunting"):
                res_hunt_dir = None
                t3_thresh = strat("resolution_hunt_t3_threshold", 0.04)
                t3_conf = strat("resolution_hunt_t3_confidence", 97)
                t5_thresh = strat("resolution_hunt_t5_threshold", 0.04)
                t5_conf = strat("resolution_hunt_t5_confidence", 95)
                t10_thresh = strat("resolution_hunt_t10_threshold", 0.03)
                t10_conf = strat("resolution_hunt_t10_confidence", 92)
                t20_thresh = strat("resolution_hunt_t20_threshold", 0.05)
                t20_conf = strat("resolution_hunt_t20_confidence", 90)
                base_thresh = strat("resolution_hunt_base_threshold", 0.08)
                base_conf = strat("resolution_hunt_base_confidence", 85)
                if time_remaining <= 3:
                    res_threshold, res_confidence = t3_thresh, t3_conf
                elif time_remaining <= 5:
                    res_threshold, res_confidence = t5_thresh, t5_conf
                elif time_remaining <= 10:
                    res_threshold, res_confidence = t10_thresh, t10_conf
                elif time_remaining <= 20:
                    res_threshold, res_confidence = t20_thresh, t20_conf
                else:
                    res_threshold, res_confidence = base_thresh, base_conf
                if up_price <= res_threshold:
                    res_hunt_dir = "UP"
                elif down_price <= res_threshold:
                    res_hunt_dir = "DOWN"
                if res_hunt_dir:
                    direction = res_hunt_dir
                    confidence = res_confidence
                    signal_type = "RESOLUTION_HUNT"
                    risk_status = "OK"
                    signals = {"signal_type": signal_type, "priority": 0, "phase": phase,
                               "wall_ratio": round(wall_ratio, 2), "lag_score": round(lag_score, 2),
                               "delta_price": round(delta_price, 2), "acceleration": round(acceleration, 2)}
                    log_to_file(f"RESOLUTION HUNT: {direction} at ${up_price if direction=='UP' else down_price:.3f} (threshold=${res_threshold:.2f})", "INFO")

            # ── T-5s Hard Deadline ──
            if not direction and not already_traded and time_remaining <= 5 and window_best_signal and module_enabled("hard_deadline"):
                best = max(window_best_signal.values(), key=lambda x: x["priority"])
                log_to_file(f"HARD_DEADLINE: firing {best['direction']} via {best['signal_type']} (conf={best['confidence']:.0f}%)", "INFO")
                direction = best["direction"]
                confidence = best["confidence"]
                signal_type = "HARD_DEADLINE"
                signals = best["signals"]
                risk_status = "OK"

            # ── Execute Trade ────────────────────────
            if direction and risk_status == "OK" and not already_traded:
                min_conf = float(cfg.get("min_confidence", 70))
                if confidence < min_conf and signal_type not in ("ARB", "RESOLUTION_HUNT", "DELTA_OVERRIDE", "HARD_DEADLINE"):
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
                    # Pick the cheaper side (single directional bet, not both)
                    arb_side = "DOWN" if up_price >= down_price else "UP"
                    arb_token_id = down_token_id if up_price >= down_price else up_token_id
                    arb_price = down_price if up_price >= down_price else up_price
                    log_to_file(f"ARB: Single bet on {arb_side} @ ${arb_price:.4f} (cheaper side)", "INFO")
                    if arb_token_id:
                        direction = arb_side
                        execute_trade(
                            arb_side, arb_token_id, arb_price, price_now,
                            slug, window_ts, 100, signals, cfg, client, market, price_to_beat
                        )
                else:
                    target_price = up_price if direction == "UP" else down_price
                    if signal_type not in ("ARB", "RESOLUTION_HUNT", "DELTA_OVERRIDE") and guard_enabled("fee_aware_gate") and not fee_aware_gate(direction, confidence, target_price):
                        fee_buffer = strat("fee_buffer_pp", 5)
                        log_to_file(f"FEE_BLOCK: {direction} at ${target_price:.3f} (conf={confidence:.0f}%, BE={target_price*100:.0f}%+{fee_buffer}pp)")
                        with strategy_lock:
                            current_strategy_info["status"] = f"Fee gate: ${target_price:.3f} too expensive"
                        direction = None
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

                    if guard_enabled("edge_block"):
                        edge_low = strat("edge_token_mid_low", 0.47)
                        edge_high = strat("edge_token_mid_high", 0.53)
                        if edge_low < target_price < edge_high:
                            log_to_file(f"EDGE BLOCK: {direction} at ${target_price:.3f} too close to 0.50")
                            with strategy_lock:
                                current_strategy_info["status"] = f"No edge at ${target_price:.3f}"
                            direction = None
                    if direction:
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
                        update_signal_guard(direction)
            else:
                    reasons = []
                    if not direction:
                        reasons.append(f"no_signal ({signal_type})")
                    elif risk_status != "OK":
                        reasons.append(risk_reason)
                    with strategy_lock:
                        current_strategy_info["status"] = f"Analyzing... ({'; '.join(reasons) or 'ok'})"

            max_baselines = strat("max_daily_market_baselines", 20)
            baseline_prune = strat("baseline_prune_window", 3600)
            if len(market_baselines) > max_baselines:
                cutoff = window_ts - baseline_prune
                market_baselines = {k: v for k, v in market_baselines.items() if k > cutoff}
                _traded_windows.difference_update({k for k in _traded_windows if k < cutoff})
                _failed_window_attempts = {k: v for k, v in _failed_window_attempts.items() if k >= cutoff}

            time.sleep(1)
        except Exception as e:
            log_to_file(f"Bot Loop Error: {e}")
            import traceback
            log_to_file(traceback.format_exc())
            time.sleep(3)

# ── API Routes (v6.0) ─────────────────────────────────
@app.route("/")
def index():
    # Try Vite-built dashboard first, then fallback to legacy index.html
    vite_path = BOT_DIR / "dashboard" / "dist" / "index.html"
    if vite_path.exists():
        return vite_path.read_text(encoding="utf-8")
    dashboard_path = BOT_DIR / "index.html"
    if dashboard_path.exists():
        return dashboard_path.read_text(encoding="utf-8")
    return jsonify({"message": "PolyBot API v6.0", "endpoints": ["/status", "/health", "/stats", "/logs"]})

@app.route("/assets/<path:filename>")
def serve_assets(filename):
    assets_dir = BOT_DIR / "dashboard" / "dist" / "assets"
    file_path = assets_dir / filename
    if file_path.exists():
        mime = {"js": "application/javascript", "css": "text/css", "map": "application/json"}.get(
            filename.rsplit(".", 1)[-1], "application/octet-stream"
        )
        return Response(file_path.read_bytes(), mimetype=mime)
    return jsonify({"error": "not found"}), 404

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
            "arb_hedge": "ACTIVE" if module_enabled("arb_hedge") else "DISABLED",
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
    elif period.endswith("d"):
        try:
            days = int(period[:-1])
            cutoff = (now - timedelta(days=days)).isoformat()
            filtered = [t for t in trades if t.get("timestamp", "") > cutoff]
        except ValueError:
            pass
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
        # Deep merge so dashboard saves don't erase manually set keys
        old_cfg.update({k: v for k, v in data.items() if k not in ("risk_management", "strategy", "modules", "guards")})
        for section in ("risk_management", "strategy", "modules", "guards"):
            if section in data and isinstance(data[section], dict):
                old_sec = old_cfg.get(section, {})
                if isinstance(old_sec, dict):
                    old_sec.update(data[section])
                    old_cfg[section] = old_sec
        safe_write_json(CONFIG_PATH, old_cfg)
        return jsonify({"status": "saved"})
    cfg = safe_read_json(CONFIG_PATH) or {}
    defaults = {
        "strategy": {},
        "modules": {},
        "guards": {},
    }
    for key, val in defaults.items():
        if key not in cfg:
            cfg[key] = val
    return jsonify(cfg)

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

# ── API Credentials Endpoint ────────────────────────────
@app.route("/env", methods=["GET", "POST"])
@require_api_key
def manage_env():
    if request.method == "GET":
        env_vars = {}
        if ENV_PATH.exists():
            with open(ENV_PATH, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k in ("POLY_PRIVATE_KEY", "POLY_API_SECRET") and v:
                            v = v[:6] + "..." + v[-4:] if len(v) > 12 else "***"
                        env_vars[k] = v
        defaults = {
            "POLY_PRIVATE_KEY": "",
            "POLY_WALLET_ADDRESS": "",
            "RELAYER_API_KEY_ADDRESS": "",
            "POLY_API_KEY": "",
            "POLY_API_SECRET": "",
            "POLY_API_PASSPHRASE": "",
            "POLYGON_RPC": "https://polygon.drpc.org",
            "POLY_SIGNATURE_TYPE": "1",
            "POLY_DEPOSIT_WALLET_ADDRESS": "",
            "POLY_RTDS_WS_URL": "",
            "POLYBOT_API_KEY": "",
            "POLYBOT_LOG_LEVEL": "INFO",
        }
        for k, v in defaults.items():
            env_vars.setdefault(k, v)
        return jsonify(env_vars)

    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No data provided"}), 400

    import re
    updated_keys = set()
    if ENV_PATH.exists():
        with open(ENV_PATH, "r") as f:
            lines = f.readlines()
    else:
        lines = []

    new_lines = []
    seen = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            seen[k] = True
            if k in data:
                val = str(data[k]).strip()
                if k in ("POLY_PRIVATE_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE", "POLYBOT_API_KEY"):
                    val = '"' + val + '"'
                new_lines.append(f"{k}={val}\n".replace("='", "=\"").replace("'\"", "\"\""))
                updated_keys.add(k)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    for k, v in data.items():
        if k not in seen and v:
            val = str(v).strip()
            if k in ("POLY_PRIVATE_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE", "POLYBOT_API_KEY"):
                val = '"' + val + '"'
            new_lines.append(f"{k}={val}\n")
            updated_keys.add(k)

    ENV_PATH.write_text("".join(new_lines), encoding="utf-8")
    load_dotenv(ENV_PATH, override=True)
    return jsonify({"status": "ok", "updated": list(updated_keys)})

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
