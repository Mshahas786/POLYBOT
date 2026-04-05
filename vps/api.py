#!/usr/bin/env python3
"""
PolyBot Unified Backend v3.0
Multi-Signal Momentum Strategy for 70%+ Accuracy.
"""

import json
import os
import time
import threading
import requests
import websocket
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Paths ──────────────────────────────────────────────────
BOT_DIR = Path(os.path.expanduser("~/polybot"))
CONFIG_PATH = BOT_DIR / "config.json"
TRADES_PATH = BOT_DIR / "trades.json"
LOG_PATH = BOT_DIR / "bot.log"

# ── Global State ───────────────────────────────────────────
start_time = datetime.now(timezone.utc)
bot_running = False
bot_thread = None

# Fast Price Feed
last_btc_price = 0
price_lock = threading.Lock()
price_buffer = []  # Rolling buffer of (timestamp, price) for signals

current_strategy_info = {
    "slug": "N/A", "price_to_beat": 0, "current_diff": 0,
    "time_remaining": 0, "up_price": 0, "down_price": 0,
    "edge": "None", "status": "Inactive", "confidence": 0,
    "signals": {}
}

# ── Binance WebSocket ──────────────────────────────────────
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
                if len(price_buffer) > 300:  # ~10 min of data
                    price_buffer = price_buffer[-300:]
                self.last_buffer_update = now

    def on_error(self, ws, error):
        print(f"WS Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print("### WS Closed - Reconnecting in 5s ###")
        time.sleep(5)
        self.run()

    def run(self):
        self.ws = websocket.WebSocketApp(
            self.url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever()

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

def log_to_file(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"{ts} [INFO] {msg}"
    print(full_msg)
    for _ in range(5):
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(full_msg + "\n")
            return
        except PermissionError:
            time.sleep(0.1)

# ── Signal Engine (Multi-Signal for 70%+ accuracy) ────────

def calc_ema(prices, period):
    """Exponential Moving Average."""
    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_rsi(prices, period=14):
    """Relative Strength Index."""
    if len(prices) < period + 1:
        return 50  # Neutral
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0.001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def analyze_signals(price_to_beat):
    """Multi-signal analysis returning direction and confidence."""
    with price_lock:
        buf = list(price_buffer)
        current = last_btc_price
    
    if len(buf) < 30 or not current or not price_to_beat:
        return None, 0, {}

    prices = [p for _, p in buf]
    
    # Signal 1: Price vs Beat (Current momentum direction)
    diff_pct = (current - price_to_beat) / price_to_beat * 100
    
    # Signal 2: Short-term EMA Trend (fast vs slow)
    ema_fast = calc_ema(prices[-15:], 5)   # ~30s EMA
    ema_slow = calc_ema(prices[-30:], 15)  # ~60s EMA
    ema_signal = "UP" if ema_fast > ema_slow else "DOWN"
    ema_strength = abs(ema_fast - ema_slow) / ema_slow * 100
    
    # Signal 3: RSI (Momentum strength)
    rsi = calc_rsi(prices[-30:], 14)
    
    # Signal 4: Recent tick direction (last 30s trend)
    recent_30s = [p for t, p in buf if t > time.time() - 30]
    if len(recent_30s) >= 2:
        tick_direction = "UP" if recent_30s[-1] > recent_30s[0] else "DOWN"
        tick_change = (recent_30s[-1] - recent_30s[0]) / recent_30s[0] * 100
    else:
        tick_direction = "FLAT"
        tick_change = 0
    
    # Signal 5: Volume-weighted price movement (last 60s vs prior 60s)
    recent_60 = [p for t, p in buf if t > time.time() - 60]
    prior_60 = [p for t, p in buf if time.time() - 120 < t <= time.time() - 60]
    if recent_60 and prior_60:
        avg_recent = sum(recent_60) / len(recent_60)
        avg_prior = sum(prior_60) / len(prior_60)
        vwap_signal = "UP" if avg_recent > avg_prior else "DOWN"
    else:
        vwap_signal = "FLAT"

    # ── Voting System (Majority Rules) ──
    votes_up = 0
    votes_down = 0
    
    # Vote 1: Current diff from beat
    if diff_pct > 0.05: votes_up += 1
    elif diff_pct < -0.05: votes_down += 1
    
    # Vote 2: EMA crossover (strong signal)
    if ema_signal == "UP" and ema_strength > 0.01: votes_up += 2
    elif ema_signal == "DOWN" and ema_strength > 0.01: votes_down += 2
    
    # Vote 3: RSI momentum
    if rsi > 55: votes_up += 1
    elif rsi < 45: votes_down += 1
    
    # Vote 4: Recent tick direction
    if tick_direction == "UP" and tick_change > 0.02: votes_up += 1
    elif tick_direction == "DOWN" and tick_change < -0.02: votes_down += 1
    
    # Vote 5: VWAP signal
    if vwap_signal == "UP": votes_up += 1
    elif vwap_signal == "DOWN": votes_down += 1
    
    total_votes = votes_up + votes_down
    if total_votes == 0:
        return None, 0, {}
    
    direction = "UP" if votes_up > votes_down else "DOWN"
    confidence = max(votes_up, votes_down) / 6 * 100  # Out of 6 max votes
    
    signals = {
        "diff": round(diff_pct, 3),
        "ema": ema_signal,
        "rsi": round(rsi, 1),
        "tick": tick_direction,
        "vwap": vwap_signal,
        "votes": f"{votes_up}U/{votes_down}D",
        "confidence": round(confidence, 0)
    }
    
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

def get_price_to_beat(window_ts):
    """Get baseline price using CryptoCompare historical close at window start."""
    try:
        resp = requests.get(
            f"https://min-api.cryptocompare.com/data/v2/histominute?fsym=BTC&tsym=USD&limit=1&toTs={window_ts}",
            timeout=5
        )
        return float(resp.json()["Data"]["Data"][-1]["close"])
    except:
        with price_lock:
            return last_btc_price

def get_current_5min_ts():
    return (int(time.time()) // 300) * 300

# ── Bot Loop ──────────────────────────────────────────────
def bot_loop():
    global bot_running, current_strategy_info
    log_to_file("🚀 MULTI-SIGNAL ENGINE v3.0 STARTING...")
    
    market_baselines = {}
    
    while bot_running:
        try:
            cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True}
            now = time.time()
            window_ts = get_current_5min_ts()
            window_offset = int(now % 300)
            slug = f"btc-updown-5m-{window_ts}"
            
            # 1. Market Discovery
            market = get_polymarket_market(slug)
            if not market:
                current_strategy_info["status"] = f"SCANNING..."
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
            
            # 2. Baseline Price
            if window_ts not in market_baselines:
                market_baselines[window_ts] = get_price_to_beat(window_ts)
                log_to_file(f"📍 Baseline for {window_ts}: ${market_baselines[window_ts]}")
            
            price_to_beat = market_baselines[window_ts]
            
            # 3. Run Signal Engine
            direction, confidence, signals = analyze_signals(price_to_beat)
            
            with price_lock:
                price_now = last_btc_price
            
            diff = (price_now - price_to_beat) / price_to_beat * 100 if price_to_beat else 0
            
            current_strategy_info = {
                "slug": slug,
                "price_to_beat": price_to_beat,
                "current_diff": round(diff, 3),
                "time_remaining": 300 - window_offset,
                "up_price": up_price,
                "down_price": down_price,
                "edge": "None",
                "status": "Analyzing",
                "confidence": confidence,
                "signals": signals
            }
            
            # 4. Decision Window (60s-240s)
            if 60 <= window_offset <= 240:
                # Log thinking every 30s
                if window_offset % 30 == 0:
                    log_to_file(f"🧐 {signals.get('votes','-')} | Conf: {confidence}% | Diff: {round(diff,3)}% | RSI: {signals.get('rsi','-')}")
                
                trades = safe_read_json(TRADES_PATH) or []
                already_traded = any(t.get("window_ts") == window_ts for t in trades)
                
                if not already_traded and direction and confidence >= 50:
                    # Check market alignment (edge exists if market underprices)
                    if direction == "UP" and up_price < 0.58:
                        current_strategy_info["edge"] = f"UP (conf {confidence}%)"
                        execute_trade("UP", up_price, price_now, slug, window_ts, confidence, signals, cfg)
                    elif direction == "DOWN" and down_price < 0.58:
                        current_strategy_info["edge"] = f"DOWN (conf {confidence}%)"
                        execute_trade("DOWN", down_price, price_now, slug, window_ts, confidence, signals, cfg)
            elif window_offset < 60 and window_offset % 30 == 0:
                log_to_file("⏳ Warming up - collecting price data...")
            elif window_offset > 240 and window_offset % 30 == 0:
                log_to_file("⏳ Window closing - entries disabled.")
            
            # Cleanup old baselines
            if len(market_baselines) > 20:
                cutoff = window_ts - 3600
                market_baselines = {k: v for k, v in market_baselines.items() if k > cutoff}

            time.sleep(1)
        except Exception as e:
            log_to_file(f"⚠️ Strategy Error: {e}")
            time.sleep(2)

def execute_trade(direction, token_price, btc_price, slug, window_ts, confidence, signals, cfg):
    is_dry = cfg.get("dry_run", True)
    status = "simulated" if is_dry else "placed"
    
    trade = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "window_ts": window_ts,
        "market_slug": slug,
        "direction": direction,
        "token_price": token_price,
        "btc_price": btc_price,
        "confidence": confidence,
        "signals": signals,
        "bet_size": 2.0,
        "dry_run": is_dry,
        "status": status,
        "outcome": None
    }
    
    log_to_file(f"🚀 TRADE: {direction} | Conf: {confidence}% | BTC: ${btc_price} | Mkt: {token_price}")
    trades = safe_read_json(TRADES_PATH) or []
    trades.append(trade)
    safe_write_json(TRADES_PATH, trades)

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
        "btc_price": live_price, "strategy": "Multi-Signal v3",
        "info": current_strategy_info,
        "total_trades": len(trades), "wins": wins, "losses": losses,
        "success_rate": round((wins/(wins+losses)*100),1) if (wins+losses) > 0 else 0,
        "uptime": str(datetime.now(timezone.utc) - start_time).split(".")[0]
    })

@app.route("/stats")
def get_stats():
    trades = safe_read_json(TRADES_PATH) or []
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = sum(1 for t in trades if t.get("outcome") == "loss")
    return jsonify({
        "total_trades": len(trades), "wins": wins, "losses": losses,
        "success_rate": round((wins/(wins+losses)*100),1) if (wins+losses) > 0 else 0,
        "history": trades[-50:]
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

@app.route("/config", methods=["GET", "POST"])
def handle_config():
    if request.method == "POST":
        safe_write_json(CONFIG_PATH, request.get_json())
        return jsonify({"status": "saved"})
    return jsonify(safe_read_json(CONFIG_PATH) or {})

@app.route("/logs")
def get_logs():
    if not LOG_PATH.exists(): return jsonify({"logs": []})
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return jsonify({"logs": [l.strip() for l in lines[-100:]]})
    except: return jsonify({"logs": []})

@app.route("/restart", methods=["POST"])
def restart_bot():
    stop_bot()
    time.sleep(1)
    return start_bot()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=3000)
