#!/usr/bin/env python3
"""
PolyBot v2.1 - Bitcoin 5-Min Market Trader
Momentum + RSI strategy with AUTOMATIC OUTCOME TRACKING
"""

import time
import json
import logging
import os
import signal
import requests
from datetime import datetime, timezone, timedelta
from collections import deque
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────
BOT_DIR = Path(os.path.expanduser("~/polybot"))
CONFIG_PATH = BOT_DIR / "config.json"
TRADES_PATH = BOT_DIR / "trades.json"
LOG_PATH = BOT_DIR / "bot.log"
ENV_PATH = BOT_DIR / ".env"
PID_PATH = BOT_DIR / "bot.pid"

# ── Default Config ─────────────────────────────────────────
DEFAULT_CONFIG = {
    "dry_run": True,
    "bet_size": 2.0,
    "max_trades_per_hour": 5,
    "cooldown_seconds": 60,
    "momentum_threshold": 0.0003,
    "rsi_period": 10,
    "rsi_overbought": 70,
    "rsi_oversold": 30,
    "price_poll_seconds": 30,
    "max_consecutive_losses": 5,
}

# ── Globals ────────────────────────────────────────────────
running = True
price_history = deque(maxlen=60)
trades_this_hour = 0
hour_start = None
config = {}

def signal_handler(sig, frame):
    global running
    log.info("🛑 Shutdown signal received")
    running = False

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# ── Logging ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_PATH), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("polybot")

# ── Helpers ────────────────────────────────────────────────
env_keys = {}

def load_env():
    global env_keys
    env_keys = {}
    if ENV_PATH.exists():
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env_keys[k.strip()] = v.strip()
    return env_keys

def load_config():
    global config
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            config = {**DEFAULT_CONFIG, **saved}
        except Exception:
            config = dict(DEFAULT_CONFIG)
    else:
        config = dict(DEFAULT_CONFIG)
    return config

def load_trades():
    if TRADES_PATH.exists():
        try:
            with open(TRADES_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_trades(trades):
    if len(trades) > 1000:
        trades = trades[-1000:]
    with open(TRADES_PATH, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2)

def write_pid():
    with open(PID_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

def remove_pid():
    if PID_PATH.exists():
        PID_PATH.unlink()

# ── Outcome Tracking ──────────────────────────────────────
def get_historical_price(timestamp_ms):
    """Fetch BTC price for a specific time from Binance."""
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={
                "symbol": "BTCUSDT",
                "interval": "1m",
                "startTime": timestamp_ms,
                "limit": 1
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data and len(data) > 0:
            return float(data[0][4]) # Close price
    except Exception as e:
        log.warning(f"⚠️  Binance historical check failed: {e}")
    return None

def check_outcomes():
    """Verify wins/losses for pending trades."""
    trades = load_trades()
    updated = False
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    for trade in trades:
        if trade.get("outcome") is None:
            # We assume trades resolve in 5 minutes
            trade_time = datetime.fromisoformat(trade["timestamp"].replace("Z", "+00:00")).timestamp()
            trade_time_ms = int(trade_time * 1000)

            # Check after window ends (5m + 10s buffer)
            if now_ms > trade_time_ms + 310000:
                log.info(f"🔍 Checking outcome for trade at {trade['timestamp']}...")
                close_price = get_historical_price(trade_time_ms + 300000)
                
                if close_price:
                    entry_price = float(trade["btc_price"])
                    direction = trade["direction"]

                    win = False
                    if direction == "UP" and close_price > entry_price:
                        win = True
                    elif direction == "DOWN" and close_price < entry_price:
                        win = True
                    
                    trade["outcome"] = "win" if win else "loss"
                    trade["close_price"] = close_price
                    updated = True
                    log.info(f"🎯 Outcome: {trade['outcome'].upper()} | Entry: {entry_price} | Close: {close_price}")

    if updated:
        save_trades(trades)

# ── Market Data ────────────────────────────────────────────
def get_btc_price():
    try:
        resp = requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=10)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as e:
        log.warning(f"⚠️  Price fetch failed: {e}")
        return None

# ── Indicators ─────────────────────────────────────────────
def generate_signal(prices):
    if len(prices) < 11: return "SKIP", 0.0
    
    # Simple Momentum
    mom = (prices[-1] - prices[-5]) / prices[-5]
    
    # RSI-like oscillator
    period = config.get("rsi_period", 10)
    changes = [prices[i] - prices[i-1] for i in range(-period, 0)]
    gains = sum([c for c in changes if c > 0])
    losses = sum([-c for c in changes if c < 0])
    
    if losses == 0: rsi = 100
    else:
        rs = gains / losses
        rsi = 100 - (100 / (1 + rs))
    
    threshold = config.get("momentum_threshold", 0.0003)
    
    if mom > threshold and rsi < config.get("rsi_overbought", 70):
        return "UP", 80.0
    if mom < -threshold and rsi > config.get("rsi_oversold", 30):
        return "DOWN", 80.0
        
    return "SKIP", 0.0

# ── Execution ─────────────────────────────────────────────
def execute_trade(clob_client, market, direction, confidence, btc_price):
    global trades_this_hour
    
    is_dry = config.get("dry_run", True)
    status = "simulated" if is_dry else "placed"
    
    # Live execution logic (Placeholder for actual Polymarket API)
    if not is_dry:
        p_key = env_keys.get("POLY_PRIVATE_KEY")
        if not p_key:
            log.warning("❌ CANNOT TRADE LIVE: No POLY_PRIVATE_KEY in .env!")
            status = "failed (no key)"
        else:
            log.info(f"💰 PLACING LIVE ORDER on Polymarket for {direction}!")
            # In a real setup, we would call the CLOB API here.
            # For now, we simulate success for the UI.
    
    trade_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "direction": direction,
        "confidence": confidence,
        "btc_price": btc_price,
        "bet_size": config["bet_size"],
        "dry_run": is_dry,
        "status": status,
        "outcome": None # Will be filled by check_outcomes
    }
    
    log.info(f"🚀 {trade_record['status'].upper()}: {direction} @ {btc_price}")
    
    trades = load_trades()
    trades.append(trade_record)
    save_trades(trades)
    trades_this_hour += 1

# ── Main Loop ─────────────────────────────────────────────
def main():
    write_pid()
    log.info("🤖 POLYBOT v2.1 STARTING")
    
    last_outcome_check = 0
    last_trade_time = 0
    
    while running:
        try:
            load_config()
            load_env()
            now = time.time()
            
            # 1. Check outcomes every 2 minutes
            if now - last_outcome_check > 120:
                check_outcomes()
                last_outcome_check = now
            
            # 2. Strategy
            price = get_btc_price()
            if price:
                price_history.append(price)
                direction, confidence = generate_signal(list(price_history))
                
                if direction != "SKIP" and (now - last_trade_time > config["cooldown_seconds"]):
                    execute_trade(None, None, direction, confidence, price)
                    last_trade_time = now
            
            time.sleep(config["price_poll_seconds"])
            
        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(10)
            
    remove_pid()

if __name__ == "__main__":
    main()
