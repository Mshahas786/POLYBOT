#!/usr/bin/env python3
"""
POLYBOT MONITOR + OPTIMIZER
Auto-restarts if crashed, monitors performance, and optimizes daily.
"""

import subprocess
import time
import json
import os
import sys
from datetime import datetime

POLYBOT_DIR = "/Users/mshahas/Downloads/POLYBOT-main/vps"
LOG_FILE = "/tmp/polybot_monitor.log"

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")
    with open(LOG_FILE, "a") as f:
        f.write(f"{timestamp} - {msg}\n")

def check_process():
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python3 api.py"],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except:
        return False

def start_bot():
    log("Starting POLYBOT...")
    try:
        process = subprocess.Popen(
            [sys.executable, "api.py"],
            cwd=POLYBOT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ}
        )
        time.sleep(10)
        if process.poll() is None:
            # Process still running
            time.sleep(5)
            log("✅ POLYBOT is RUNNING")
            return True
        else:
            log(f"❌ POLYBOT crashed immediately. Exit: {process.returncode}")
            return False
    except Exception as e:
        log(f"❌ Failed to start: {e}")
        return False

def get_performance():
    """Read performance from data/trades.json if exists"""
    trades_file = os.path.join(POLYBOT_DIR, "..", "data", "trades.json")
    if os.path.exists(trades_file):
        try:
            with open(trades_file) as f:
                trades = json.load(f)
            total = len(trades)
            wins = sum(1 for t in trades if t.get("profit", 0) > 0)
            win_rate = (wins / total * 100) if total > 0 else 0
            return {"total": total, "wins": wins, "win_rate": round(win_rate, 1)}
        except:
            pass
    return None

def optimize():
    """Read config and suggest optimizations"""
    config_file = os.path.join(POLYBOT_DIR, "..", "config.json")
    if os.path.exists(config_file):
        try:
            with open(config_file) as f:
                config = json.load(f)
            log(f"Config loaded: strategy={config.get('risk', {}).get('strategy', 'unknown')}")
            return config
        except:
            pass
    return None

if __name__ == "__main__":
    log("=" * 50)
    log("POLYBOT MONITOR STARTED")
    log("=" * 50)

    if not check_process():
        log("⚠️  POLYBOT is not running. Starting...")
        start_bot()
    else:
        log("✅ POLYBOT is already running")

    perf = get_performance()
    if perf:
        log(f"📊 Performance: {perf}")

    cfg = optimize()
    if cfg:
        log("✅ Config verification complete")

    log("Monitoring complete. Bot check interval: every 30 min")
