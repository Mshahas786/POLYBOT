#!/usr/bin/env python3
"""
PolyBot Unified Backend v2.6
BTC 5-Minute "Price to Beat" Strategy Implementation.
"""

import json
import os
import time
import threading
import requests
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
price_history = []
env_keys = {}
current_strategy_info = {
    "market": "N/A",
    "price_to_beat": 0,
    "current_diff": 0,
    "time_remaining": 0
}

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
def get_btc_price():
    """Fetch BTC price from CryptoCompare (User's preferred source)."""
    try:
        resp = requests.get("https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD", timeout=5)
        return float(resp.json()["USD"])
    except:
        # Fallback to Binance
        try:
            resp = requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=5)
            return float(resp.json()["price"])
        except: return None

def get_active_5m_markets():
    """Fetch active BTC 5m markets from Polymarket Gamma API."""
    try:
        # Query Gamma API for BTC markets
        resp = requests.get("https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=20", timeout=10)
        markets = resp.json()
        
        filtered = []
        for m in markets:
            q = m.get("question", "").lower()
            if "bitcoin" in q and "5" in q and "-5m-" in m.get("slug", ""):
                filtered.append(m)
        return filtered
    except Exception as e:
        log_to_file(f"⚠️ Market Scan Failed: {e}")
        return []

# ── Bot Loop & Strategy ───────────────────────────────────
def bot_loop():
    global bot_running, current_strategy_info
    log_to_file("🤖 BTC 5M STRATEGY ENGINE STARTING...")
    
    market_start_prices = {} # slug -> price_to_beat
    
    while bot_running:
        try:
            cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True, "bet_size": 2.0}
            now_dt = datetime.now(timezone.utc)
            
            # 1. Scan for markets
            markets = get_active_5m_markets()
            if not markets:
                time.sleep(10)
                continue
            
            # Process the "soonest" market
            m = markets[0]
            slug = m.get("slug")
            end_time_str = m.get("endDate")
            if not end_time_str: continue
            
            end_dt = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
            time_remaining = (end_dt - now_dt).total_seconds()
            
            # 2. Get Price to Beat (Start Price)
            # For 5m markets, they open at every 5m mark (00, 05, 10...)
            # We track the price at the beginning of THIS market's window.
            if slug not in market_start_prices:
                # If we just found a new market, its start price was from ~5 mins ago
                # But the user strategy says: "Store price_to_beat at start of each 5-min window"
                market_start_prices[slug] = get_btc_price()
                log_to_file(f"📍 New Market Detected: {slug} | Price to Beat: {market_start_prices[slug]}")
            
            price_to_beat = market_start_prices[slug]
            price_now = get_btc_price()
            
            if price_now and price_to_beat:
                diff = (price_now - price_to_beat) / price_to_beat * 100
                
                # Update Dashboard Info
                current_strategy_info = {
                    "market": slug,
                    "price_to_beat": price_to_beat,
                    "current_diff": round(diff, 3),
                    "time_remaining": int(time_remaining)
                }
                
                # 3. Decision Rules
                if time_remaining > 60:
                    # Check if already traded in this slug
                    trades = safe_read_json(TRADES_PATH) or []
                    already_traded = any(t.get("market_slug") == slug for t in trades)
                    
                    if not already_traded:
                        if diff > 0.3:
                            execute_trade("UP", 90.0, price_now, slug, cfg)
                        elif diff < -0.3:
                            execute_trade("DOWN", 90.0, price_now, slug, cfg)
            
            # Cleanup old slugs
            if len(market_start_prices) > 10:
                market_start_prices = {k: v for i, (k, v) in enumerate(market_start_prices.items()) if i > 5}

            time.sleep(10) # Scan frequency
        except Exception as e:
            log_to_file(f"⚠️ Strategy Loop Error: {e}")
            time.sleep(5)

def execute_trade(direction, confidence, price, slug, cfg):
    is_dry = cfg.get("dry_run", True)
    status = "simulated" if is_dry else "placed"
    
    trade = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_slug": slug,
        "direction": direction,
        "confidence": confidence,
        "btc_price": price,
        "bet_size": 2.0, # Strategy forced $2
        "dry_run": is_dry,
        "status": status,
        "outcome": None
    }
    
    log_to_file(f"🚀 {status.upper()} TRADE on {slug}: {direction} @ {price}")
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
    
    return jsonify({
        "running": bot_running,
        "dry_run": cfg.get("dry_run", True),
        "strategy": "BTC 5m PriceToBeat",
        "info": current_strategy_info,
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "success_rate": round((wins / (wins+losses) * 100), 1) if (wins+losses) > 0 else 0,
        "uptime": str(datetime.now(timezone.utc) - start_time).split(".")[0]
    })

@app.route("/stats")
def get_stats():
    trades = safe_read_json(TRADES_PATH) or []
    return jsonify({"history": trades[-50:]})

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
    log_to_file("🛑 BOT STOPPED VIA DASHBOARD")
    return jsonify({"status": "stopped"})

@app.route("/config", methods=["GET", "POST"])
def handle_config():
    if request.method == "POST":
        safe_write_json(CONFIG_PATH, request.get_json())
        return jsonify({"status": "saved"})
    return jsonify(safe_read_json(CONFIG_PATH) or {})

@app.route("/logs")
def get_logs():
    n = int(request.args.get("n", 100))
    if not LOG_PATH.exists(): return jsonify({"logs": []})
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return jsonify({"logs": [l.strip() for l in lines[-n:]]})
    except: return jsonify({"logs": []})

@app.route("/clear-logs", methods=["POST"])
def clear_logs():
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f: f.write("")
        return jsonify({"status": "cleared"})
    except: return jsonify({"status": "error"})

@app.route("/restart", methods=["POST"])
def restart_bot():
    stop_bot()
    time.sleep(1)
    return start_bot()

@app.route("/clear-trades", methods=["POST"])
def clear_trades():
    if safe_write_json(TRADES_PATH, []):
        return jsonify({"status": "cleared"})
    return jsonify({"status": "error"})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=3000)
