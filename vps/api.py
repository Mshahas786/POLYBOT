#!/usr/bin/env python3
"""
PolyBot Unified Backend v2.5
Combined Flask API + Trading Engine with Background Threading.
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

# ── Bot Logic ─────────────────────────────────────────────
def get_btc_price():
    try:
        resp = requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=5)
        return float(resp.json()["price"])
    except: return None

def get_historical_price(ts_ms):
    try:
        resp = requests.get("https://api.binance.com/api/v3/klines", 
                            params={"symbol": "BTCUSDT", "interval": "1m", "startTime": ts_ms, "limit": 1}, timeout=5)
        return float(resp.json()[0][4])
    except: return None

def check_outcomes():
    trades = safe_read_json(TRADES_PATH) or []
    updated = False
    now = datetime.now(timezone.utc).timestamp()
    
    for t in trades:
        if t.get("outcome") is None:
            t_time = datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00")).timestamp()
            if now > t_time + 310: # 5m window
                close = get_historical_price(int(t_time * 1000) + 300000)
                if close:
                    entry = float(t["btc_price"])
                    win = (t["direction"] == "UP" and close > entry) or (t["direction"] == "DOWN" and close < entry)
                    t["outcome"] = "win" if win else "loss"
                    t["close_price"] = close
                    updated = True
                    log_to_file(f"🎯 Outcome: {t['outcome'].upper()} | Entry: {entry} | Close: {close}")
    if updated: safe_write_json(TRADES_PATH, trades)

def bot_loop():
    global bot_running, price_history, env_keys
    log_to_file("🤖 POLYBOT ENGINE STARTING...")
    last_outcome_check = 0
    
    while bot_running:
        try:
            # Load dynamic config
            cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True, "bet_size": 2.0}
            
            # Live Outcome Check
            now = time.time()
            if now - last_outcome_check > 120:
                check_outcomes()
                last_outcome_check = now
            
            # Strategy
            price = get_btc_price()
            if price:
                price_history.append(price)
                if len(price_history) > 20: price_history.pop(0)
                
                # Simple RSI Logic
                if len(price_history) >= 10:
                    diffs = [price_history[i] - price_history[i-1] for i in range(1, len(price_history))]
                    gains = sum(d for d in diffs if d > 0)
                    losses = abs(sum(d for d in diffs if d < 0))
                    rsi = 100 - (100 / (1 + (gains/losses))) if losses > 0 else 100
                    
                    if rsi < 30: # Oversold -> UP
                        execute_trade("UP", 85.0, price, cfg)
                    elif rsi > 70: # Overbought -> DOWN
                        execute_trade("DOWN", 85.0, price, cfg)
            
            time.sleep(cfg.get("price_poll_seconds", 10))
        except Exception as e:
            log_to_file(f"⚠️ Bot Loop Error: {e}")
            time.sleep(5)

def execute_trade(direction, confidence, price, cfg):
    is_dry = cfg.get("dry_run", True)
    status = "simulated" if is_dry else "placed"
    
    if not is_dry:
        # Load env for live keys
        env = {}
        if ENV_PATH.exists():
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        env[k] = v
        
        if not env.get("POLY_PRIVATE_KEY"):
            log_to_file("❌ CANNOT TRADE LIVE: No POLY_PRIVATE_KEY found in .env!")
            status = "failed (no key)"
        else:
            log_to_file(f"💰 PLACING LIVE {direction} ORDER on Polymarket!")

    trade = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "direction": direction,
        "confidence": confidence,
        "btc_price": price,
        "bet_size": cfg.get("bet_size", 2.0),
        "dry_run": is_dry,
        "status": status,
        "outcome": None
    }
    
    log_to_file(f"🚀 {status.upper()}: {direction} @ {price}")
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
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "success_rate": round((wins / (wins+losses) * 100), 1) if (wins+losses) > 0 else 0,
        "uptime": str(datetime.now(timezone.utc) - start_time).split(".")[0]
    })

@app.route("/stats")
def get_stats():
    trades = safe_read_json(TRADES_PATH) or []
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = sum(1 for t in trades if t.get("outcome") == "loss")
    return jsonify({
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "success_rate": round((wins / (wins+losses) * 100), 1) if (wins+losses) > 0 else 0,
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

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=3000)
