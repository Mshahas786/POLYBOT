#!/usr/bin/env python3
"""
PolyBot API Server - Flask management API for dashboard control.
Runs on port 3000, manages bot.py lifecycle.
"""

import json
import os
import subprocess
import signal
import time
from datetime import datetime, timezone
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
    """Get bot PID from file, verify it's running."""
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
            # Check if process exists
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            PID_PATH.unlink(missing_ok=True)
    return None


def is_bot_running():
    global bot_process
    # Check subprocess first
    if bot_process and bot_process.poll() is None:
        return True
    # Check PID file
    return get_bot_pid() is not None


def read_log_lines(n=100):
    if LOG_PATH.exists():
        try:
            with open(LOG_PATH) as f:
                lines = f.readlines()
                return [l.strip() for l in lines[-n:]]
        except Exception:
            return []
    return []


def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def load_trades():
    if TRADES_PATH.exists():
        try:
            with open(TRADES_PATH) as f:
                return json.load(f)
        except Exception:
            return []
    return []


# ── Routes ─────────────────────────────────────────────────

@app.route("/status")
def status():
    running = is_bot_running()
    logs = read_log_lines(20)
    cfg = load_config()
    trades = load_trades()

    # Calculate stats
    total_trades = len(trades)
    simulated = sum(1 for t in trades if t.get("dry_run"))
    live = total_trades - simulated

    # Uptime
    uptime_seconds = (datetime.now(timezone.utc) - start_time).total_seconds()
    hours = int(uptime_seconds // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    uptime_str = f"{hours}h {minutes}m"

    # Recent trade stats
    recent = trades[-20:] if trades else []
    up_trades = sum(1 for t in recent if t.get("direction") == "UP")
    down_trades = sum(1 for t in recent if t.get("direction") == "DOWN")

    return jsonify({
        "running": running,
        "logs": logs,
        "dry_run": cfg.get("dry_run", True),
        "bet_size": cfg.get("bet_size", 2.0),
        "uptime": uptime_str,
        "total_trades": total_trades,
        "simulated_trades": simulated,
        "live_trades": live,
        "up_trades": up_trades,
        "down_trades": down_trades,
        "pid": get_bot_pid(),
        "server_uptime": uptime_str,
    })


@app.route("/start", methods=["POST"])
def start_bot():
    global bot_process

    if is_bot_running():
        return jsonify({"status": "already_running", "message": "Bot is already running"})

    try:
        bot_process = subprocess.Popen(
            ["python3", str(BOT_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(BOT_DIR),
        )
        time.sleep(1)

        if bot_process.poll() is None:
            return jsonify({"status": "started", "pid": bot_process.pid})
        else:
            return jsonify({"status": "error", "message": "Bot exited immediately"}), 500

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/stop", methods=["POST"])
def stop_bot():
    global bot_process

    pid = get_bot_pid()

    # Try subprocess first
    if bot_process and bot_process.poll() is None:
        bot_process.terminate()
        try:
            bot_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bot_process.kill()
        bot_process = None
        PID_PATH.unlink(missing_ok=True)
        return jsonify({"status": "stopped"})

    # Try PID file
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
            # Check if still running
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            PID_PATH.unlink(missing_ok=True)
            return jsonify({"status": "stopped"})
        except ProcessLookupError:
            PID_PATH.unlink(missing_ok=True)
            return jsonify({"status": "not_running"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "not_running", "message": "Bot is not running"})


@app.route("/restart", methods=["POST"])
def restart_bot():
    stop_bot()
    time.sleep(2)
    return start_bot()


@app.route("/logs")
def logs():
    n = request.args.get("n", 100, type=int)
    lines = read_log_lines(min(n, 500))
    return jsonify({"logs": lines, "count": len(lines)})


@app.route("/trades")
def trades():
    all_trades = load_trades()
    limit = request.args.get("limit", 50, type=int)
    return jsonify({
        "trades": all_trades[-limit:],
        "total": len(all_trades),
    })


@app.route("/config", methods=["GET"])
def get_config():
    cfg = load_config()
    return jsonify(cfg)


@app.route("/config", methods=["POST"])
def update_config():
    try:
        new_cfg = request.get_json()
        if not new_cfg:
            return jsonify({"status": "error", "message": "No config provided"}), 400

        current = load_config()
        current.update(new_cfg)
        save_config(current)

        return jsonify({"status": "saved", "config": current})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/clear-logs", methods=["POST"])
def clear_logs():
    try:
        with open(LOG_PATH, "w") as f:
            f.write("")
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/clear-trades", methods=["POST"])
def clear_trades():
    try:
        with open(TRADES_PATH, "w") as f:
            json.dump([], f)
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    print("🚀 PolyBot API Server starting on port 3000...")
    app.run(host="0.0.0.0", port=3000, debug=False)
