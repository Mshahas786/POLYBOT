#!/usr/bin/env python3
"""
PolyBot Unified Backend v2.7
Advanced "Edge" Strategy with Binance WebSockets & Odds Analysis.
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

app = Flask(__name__)
CORS(app)

# ── Paths ──────────────────────────────────────────────────
BOT_DIR = Path(os.path.expanduser("~/polybot"))
CONFIG_PATH = BOT_DIR / "config.json"
TRADES_PATH = BOT_DIR / "trades.json"
LOG_PATH = BOT_DIR / "bot.log"
ENV_PATH = BOT_DIR / ".env"

# ── Global State ───────────────────────────────────────────
start_time = datetime.now(timezone.utc)
bot_running = False
bot_thread = None
env_keys = {}

# Fast Price Feed State
last_btc_price = 0
price_lock = threading.Lock()

current_strategy_info = {
    "slug": "N/A",
    "price_to_beat": 0,
    "current_diff": 0,
    "time_remaining": 0,
    "up_price": 0,
    "down_price": 0,
    "edge": "None"
}

# ── Binance WebSocket Client ───────────────────────────────
class BinanceWS:
    def __init__(self):
        self.url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        self.ws = None
        self.thread = None

    def on_message(self, ws, message):
        global last_btc_price
        data = json.loads(message)
        with price_lock:
            last_btc_price = float(data['p'])

    def on_error(self, ws, error):
        print(f"WS Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print("### WS Closed ###")

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

# Start WebSocket immediately
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

# ── Data Sources ──────────────────────────────────────────
def get_polymarket_market(slug):
    """Fetch market details from Gamma API."""
    try:
        resp = requests.get(f"https://gamma-api.polymarket.com/markets?slug={slug}", timeout=5)
        data = resp.json()
        if data and len(data) > 0:
            return data[0]
    except: return None

def get_current_5min_ts():
    return (int(time.time()) // 300) * 300

# ── Bot Loop & Strategy ───────────────────────────────────
def bot_loop():
    global bot_running, current_strategy_info
    log_to_file("🚀 ADVANCED EDGE ENGINE STARTING...")
    
    market_start_prices = {} # window_ts -> price_to_beat
    
    while bot_running:
        try:
            cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True}
            now = time.time()
            window_ts = get_current_5min_ts()
            window_offset = int(now % 300)
            slug = f"btc-updown-5m-{window_ts}"
            
            # 1. Fetch Market Details
            market = get_polymarket_market(slug)
            if not market:
                time.sleep(5)
                continue
            
            # Extract Tokens (0: UP, 1: DOWN)
            tokens = market.get("tokens", [])
            if len(tokens) < 2: continue
            
            up_price = float(tokens[0].get("price", 0.5))
            down_price = float(tokens[1].get("price", 0.5))
            
            # 2. Get Price to Beat (Start Price)
            # Use CryptoCompare for the "Resolution Start" baseline if possible, or Binance at window start.
            if window_ts not in market_start_prices:
                try:
                    # Fetching the price at the interval start
                    resp = requests.get(f"https://min-api.cryptocompare.com/data/pricehistorical?fsym=BTC&tsyms=USD&ts={window_ts}", timeout=5)
                    market_start_prices[window_ts] = float(resp.json()["BTC"]["USD"])
                except:
                    with price_lock:
                        market_start_prices[window_ts] = last_btc_price
                log_to_file(f"📍 New Window {window_ts} | Price to Beat: {market_start_prices[window_ts]}")

            price_to_beat = market_start_prices[window_ts]
            with price_lock:
                price_now = last_btc_price
            
            if price_now and price_to_beat:
                diff = (price_now - price_to_beat) / price_to_beat * 100
                
                # Update Info
                current_strategy_info = {
                    "slug": slug,
                    "price_to_beat": price_to_beat,
                    "current_diff": round(diff, 3),
                    "time_remaining": 300 - window_offset,
                    "up_price": up_price,
                    "down_price": down_price,
                    "edge": "None"
                }

                # 3. Timing Rules (60-240 Range)
                if 60 <= window_offset <= 240:
                    # Check if already traded in this window
                    trades = safe_read_json(TRADES_PATH) or []
                    already_traded = any(t.get("window_ts") == window_ts for t in trades)
                    
                    if not already_traded:
                        # EDGE LOGIC
                        if diff > 0.3 and up_price < 0.55:
                            current_strategy_info["edge"] = "UP triggered"
                            execute_trade("UP", up_price, price_now, slug, window_ts, cfg)
                        elif diff < -0.3 and down_price < 0.55:
                            current_strategy_info["edge"] = "DOWN triggered"
                            execute_trade("DOWN", down_price, price_now, slug, window_ts, cfg)
            
            # 4. Success Tracking (Log win/loss of previous window)
            prev_window = window_ts - 300
            if prev_window in market_start_prices:
                # We can check outcome here by calling CryptoCompare for window_ts price
                # Or just let check_outcomes handle it.
                pass

            time.sleep(2) # High speed strategy checks
        except Exception as e:
            log_to_file(f"⚠️ Edge Strategy Error: {e}")
            time.sleep(2)

def execute_trade(direction, token_price, btc_price, slug, window_ts, cfg):
    is_dry = cfg.get("dry_run", True)
    status = "simulated" if is_dry else "placed"
    
    trade = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "window_ts": window_ts,
        "market_slug": slug,
        "direction": direction,
        "token_price": token_price,
        "btc_price": btc_price,
        "bet_size": 2.0,
        "dry_run": is_dry,
        "status": status,
        "outcome": None
    }
    
    log_to_file(f"🎯 EDGE TRIGGERED: {direction} | BTC {btc_price} | Price: {token_price}")
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
    
    with price_lock:
        live_price = last_btc_price

    return jsonify({
        "running": bot_running,
        "dry_run": cfg.get("dry_run", True),
        "btc_price": live_price,
        "strategy": "Advanced Edge (WS)",
        "info": current_strategy_info,
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "success_rate": round((wins / (wins+losses) * 100), 1) if (wins+losses) > 0 else 0,
        "uptime": str(datetime.now(timezone.utc) - start_time).split(".")[0]
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
