#!/usr/bin/env python3
"""
PolyBot API Server v2.1
Flask management API with Statistics & Analytics endpoints.
"""

import json
import os
import subprocess
import signal
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Paths ──────────────────────────────────────────────────
BOT_DIR = Path(os.path.expanduser("~/polybot"))
BOT_SCRIPT = BOT_DIR / "bot.py"
CONFIG_PATH = BOT_DIR / "config.json"
TRADES_PATH = BOT_DIR / "trades.json"
LOG_PATH = BOT_DIR / "bot.log"
PID_PATH = BOT_DIR / "bot.pid"

bot_process = None
start_time = datetime.now(timezone.utc)

# ── Helpers ────────────────────────────────────────────────
def get_bot_pid():
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
            os.kill(pid, 0)
            return pid
        except:
            PID_PATH.unlink(missing_ok=True)
    return None

def is_bot_running():
    return get_bot_pid() is not None

def load_trades():
    if TRADES_PATH.exists():
        try:
            with open(TRADES_PATH) as f:
                return json.load(f)
        except:
            return []
    return []

# ── Routes ─────────────────────────────────────────────────

@app.route("/stats")
def stats():
    period = request.args.get("period", "24h")
    trades = load_trades()
    now = datetime.now(timezone.utc)
    
    # Filter by period
    filtered = []
    if period == "all":
        filtered = trades
    else:
        # e.g. 1h, 6h, 24h, 30d
        try:
            unit = period[-1]
            val = int(period[:-1])
            delta = timedelta(hours=val) if unit == "h" else timedelta(days=val) if unit == "d" else timedelta(minutes=val) if unit == "m" else timedelta(hours=24)
            
            for t in trades:
                t_time = datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00"))
                if now - t_time <= delta:
                    filtered.append(t)
        except:
            filtered = trades

    wins = sum(1 for t in filtered if t.get("outcome") == "win")
    losses = sum(1 for t in filtered if t.get("outcome") == "loss")
    skips = sum(1 for t in filtered if t.get("direction") == "SKIP")
    total = len(filtered) - skips
    
    success_rate = (wins / total * 100) if total > 0 else 0
    
    return jsonify({
        "period": period,
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "success_rate": round(success_rate, 1),
        "history": filtered[-50:]
    })

@app.route("/status")
def status():
    running = is_bot_running()
    trades = load_trades()
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = sum(1 for t in trades if t.get("outcome") == "loss")
    
    return jsonify({
        "running": running,
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "success_rate": round((wins / (wins+losses) * 100), 1) if (wins+losses) > 0 else 0,
        "uptime": str(datetime.now(timezone.utc) - start_time).split(".")[0]
    })

@app.route("/start", methods=["POST"])
def start_bot():
    if is_bot_running(): return jsonify({"status": "already_running"})
    subprocess.Popen(["python3", str(BOT_SCRIPT)], cwd=str(BOT_DIR))
    return jsonify({"status": "started"})

@app.route("/stop", methods=["POST"])
def stop_bot():
    pid = get_bot_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        return jsonify({"status": "stopped"})
    return jsonify({"status": "not_running"})

@app.route("/config", methods=["GET", "POST"])
def handle_config():
    if request.method == "POST":
        with open(CONFIG_PATH, "w") as f:
            json.dump(request.get_json(), f, indent=2)
        return jsonify({"status": "saved"})
    
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return jsonify(json.load(f))
    return jsonify({})

@app.route("/logs")
def get_logs():
    n = int(request.args.get("n", 100))
    if LOG_PATH.exists():
        try:
            with open(LOG_PATH, "r") as f:
                lines = f.readlines()
                return jsonify({"logs": [l.strip() for l in lines[-n:]]})
        except:
            return jsonify({"logs": []})
    return jsonify({"logs": []})

@app.route("/restart", methods=["POST"])
def restart_bot():
    stop_bot()
    time.sleep(1)
    return start_bot()

@app.route("/clear-logs", methods=["POST"])
def clear_logs():
    if LOG_PATH.exists():
        LOG_PATH.write_text("")
    return jsonify({"status": "cleared"})

@app.route("/clear-trades", methods=["POST"])
def clear_trades():
    if TRADES_PATH.exists():
        TRADES_PATH.write_text("[]")
    return jsonify({"status": "cleared"})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
