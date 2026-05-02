#!/usr/bin/env python3
"""
PolyBot v5.1 - Profit-Focused Trading Engine
Upgrades: Risk management, P&L tracking, circuit breakers, health checks,
          session-based auto-stop, slippage guards, structured logging.
"""

import csv
import io
import json
import os
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

# ── Risk Manager ─────────────────────────────────────────────────
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

# ── API Key Middleware ─────────────────────────────────────────────────
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

# ── Paths ───────────────────────────────────────────────────────────────
BOT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = BOT_DIR / "config.json"
TRADES_PATH = BOT_DIR / "trades.json"
ENV_PATH = BOT_DIR / ".env"
LOG_PATH = BOT_DIR / "bot.log"

# ── Global State ────────────────────────────────────────────────────────
start_time = datetime.now(timezone.utc)
bot_running = False
bot_thread = None
last_btc_price = 0
price_lock = threading.Lock()
strategy_lock = threading.Lock()
trades_lock = threading.Lock()
price_buffer = []
account_stats = {"balance": 0.0, "pnl": 0.0, "last_updated": 0}
current_strategy_info = {
    "slug": "N/A", "price_to_beat": 0, "current_diff": 0,
    "time_remaining": 0, "up_price": 0, "down_price": 0,
    "edge": "None", "status": "Inactive", "confidence": 0, "signals": {},
    "risk_status": "OK", "risk_reason": ""
}
risk_manager = None  # type: Optional[RiskManager]

# ── Binance WebSocket ──────────────────────────────────────────────────
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
        log_to_file(f"Binance WS closed (code={code}). Reconnecting in {self._reconnect_delay}s...", "WARN")
        time.sleep(self._reconnect_delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
        self.run()

    def on_open(self, ws):
        log_to_file("Binance WS connected", "INFO")
        self._reconnect_delay = 5  # Reset backoff on successful connection

    def run(self):
        self.ws = websocket.WebSocketApp(
            self.url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open
        )
        self.ws.run_forever(ping_interval=30, ping_timeout=10)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

ws_client = BinanceWS()
ws_client.start()

# ── File Helpers ──────────────────────────────────────────────────────────
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

# ── Signal Engine v5.1 (unchanged core logic) ──────────────────────────
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
    import math
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

def analyze_signals(price_to_beat, up_price, down_price):
    with price_lock:
        buf = list(price_buffer)
        current = last_btc_price
    if not current or not price_to_beat or len(buf) < 20:
        return None, 0, {}
    prices = [p for _, p in buf]
    now = time.time()
    window_offset = int(now % 300)
    time_remaining = 300 - window_offset
    odds_dir, odds_edge, odds_reason = calc_odds_edge(current, price_to_beat, up_price, down_price)
    mom_60 = calc_momentum_score(prices, 60)
    mom_dir_60 = "UP" if mom_60 > 15 else ("DOWN" if mom_60 < -15 else "NEUTRAL")
    mom_20 = calc_momentum_score(prices, 20)
    mom_dir_20 = "UP" if mom_20 > 20 else ("DOWN" if mom_20 < -20 else "NEUTRAL")
    diff_pct = (current - price_to_beat) / price_to_beat * 100
    if diff_pct > 0.15:
        locked_dir = "UP"
        locked_strength = min(diff_pct / 0.5 * 100, 100)
    elif diff_pct < -0.15:
        locked_dir = "DOWN"
        locked_strength = min(abs(diff_pct) / 0.5 * 100, 100)
    else:
        locked_dir = "NEUTRAL"
        locked_strength = 0
    target_price = up_price if (odds_dir == "UP") else down_price if odds_dir else 0.5
    expected_payout = 1.0 - target_price
    expected_loss = target_price
    breakeven_winrate = expected_loss / (expected_loss + expected_payout) if (expected_loss + expected_payout) > 0 else 0.5
    signals_agree = 0
    total_signals = 0
    direction_votes = {"UP": 0, "DOWN": 0}
    if odds_dir:
        direction_votes[odds_dir] += 3
        signals_agree += 3
        total_signals += 3
    if locked_dir != "NEUTRAL":
        direction_votes[locked_dir] += 2
        total_signals += 2
    else:
        total_signals += 2
    if mom_dir_60 != "NEUTRAL":
        direction_votes[mom_dir_60] += 2
        total_signals += 2
    else:
        total_signals += 2
    if mom_dir_20 != "NEUTRAL":
        direction_votes[mom_dir_20] += 1
        total_signals += 1
    else:
        total_signals += 1
    if total_signals == 0:
        return None, 0, {}
    final_dir = max(direction_votes, key=direction_votes.get)
    agreement_score = direction_votes[final_dir] / total_signals
    if not odds_dir or odds_dir != final_dir:
        confidence = 0
        final_dir = None
    else:
        base_conf = agreement_score * 80
        edge_boost = min(odds_edge * 2, 20)
        roi_penalty = 0
        if breakeven_winrate > 0.65:
            roi_penalty = (breakeven_winrate - 0.65) * 200
        confidence = max(0, base_conf + edge_boost - roi_penalty)
    signals = {
        "odds_dir": odds_dir, "odds_edge": odds_edge, "odds_reason": odds_reason,
        "diff_pct": round(diff_pct, 4), "locked_dir": locked_dir, "locked_strength": round(locked_strength, 1),
        "mom_60s": round(mom_60, 1), "mom_60_dir": mom_dir_60,
        "mom_20s": round(mom_20, 1), "mom_20_dir": mom_dir_20,
        "token_price": round(target_price, 3), "breakeven_wr": round(breakeven_winrate * 100, 1),
        "agreement": round(agreement_score * 100, 1), "confidence": round(confidence, 1)
    }
    if confidence > 30:
        log_to_file(
            f"SIGNAL v5.1: {final_dir} conf={confidence:.1f}% | edge={odds_edge:.1f}¢ | "
            f"diff={diff_pct:+.3f}% | mom60={mom_60:.1f} | token=${target_price:.3f} | "
            f"breakeven={breakeven_winrate*100:.1f}% | {odds_reason}"
        )
    return (final_dir, round(confidence, 1), signals) if final_dir else (None, 0, signals)

# ── Market Data ──────────────────────────────────────────────────────────────
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

def fetch_account_stats(address):
    global account_stats
    if not address: return
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

# ── Outcome Tracking v5.1 (with P&L + Risk Integration) ──────────────────────
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
                    f"?symbol=BTCUSDT&interval=1m&startTime={resolve_ts * 1000}&limit=1",
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
                        f"?fsym=BTC&tsym=USD&limit=1&toTs={resolve_ts + 60}",
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
            # v5.1: Calculate actual P&L
            bet = float(t.get("bet_size", 2.0))
            token_price = float(t.get("token_price", 0.5))
            if win:
                t["pnl"] = round((1.0 - token_price) * bet, 2)
            else:
                t["pnl"] = round(-token_price * bet, 2)
            updated = True
            log_to_file(
                f"{'WIN' if win else 'LOSS'}: {direction} | "
                f"Strike: ${base:.2f} → Resolve: ${resolve_open_price:.2f} | "
                f"P&L: ${t['pnl']:+.2f} | Diff: {(resolve_open_price-base)/base*100:+.3f}%"
            )
            if risk_manager:
                risk_manager.record_outcome(win, bet, token_price)
        if updated:
            safe_write_json(TRADES_PATH, trades)
    # v5.1: Session-based auto-stop via RiskManager
    if risk_manager and bot_running:
        ok, reason = risk_manager.check_recent_win_rate(trades)
        if not ok:
            bot_running = False
            log_to_file(f"AUTO-STOP triggered: {reason}")

# ── Trade Execution v5.1 (Risk Guarded) ──────────────────────────────
def execute_trade(direction, token_id, token_price, btc_price, slug,
                  window_ts, confidence, signals, cfg, client=None, market=None, price_to_beat=0):
    global risk_manager
    is_dry = cfg.get("dry_run", True)
    status = "simulated"
    order_id = "N/A"
    condition_id = market.get("conditionId") if market else None
    base_bet = float(cfg.get("bet_size", 2.0))

    # v5.1: Risk check before ANY execution (even simulated)
    if risk_manager:
        allowed, risk_reason = risk_manager.can_trade(token_price)
        if not allowed:
            log_to_file(f"RISK_BLOCK: {risk_reason}")
            return {"blocked": True, "reason": risk_reason}
        final_bet = risk_manager.get_bet_size(base_bet, signals.get("odds_edge", 0) / 100.0, confidence)
    else:
        final_bet = base_bet

    if not is_dry and client:
        try:
            from py_clob_client.clob_types import OrderType, BalanceAllowanceParams, AssetType
            try:
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                balance_data = client.get_balance_allowance(params)
                if isinstance(balance_data, dict):
                    current_balance = float(balance_data.get("balance", 0)) / 1e6
                else:
                    current_balance = float(balance_data) / 1e6 if balance_data else 0.0
                if current_balance < final_bet:
                    log_to_file(f"BALANCE: Need ${final_bet:.2f}, have ${current_balance:.2f}")
                    return {"blocked": True, "reason": "insufficient_balance"}
            except Exception as be:
                log_to_file(f"Balance check failed: {be}")

            log_to_file(f"LIVE ORDER: {direction} ${final_bet} @ ${token_price:.3f}")
            from py_clob_client.clob_types import MarketOrderArgs
            # v5.1: Slippage guard — cap price with configurable bps
            slippage_bps = cfg.get("risk_management", {}).get("slippage_bps", 100)
            max_price = min(token_price + (slippage_bps / 10000.0), 0.90)
            capped_price = round(max_price, 2)
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=final_bet,
                side="BUY",
                price=capped_price,
            )
            signed_order = client.create_market_order(order_args)
            resp = client.post_order(signed_order, OrderType.GTC)
            if resp and (hasattr(resp, "orderID") or
                         (isinstance(resp, dict) and "orderID" in resp)):
                order_id = getattr(resp, "orderID", resp.get("orderID") if isinstance(resp, dict) else "N/A")
                status = "placed"
                log_to_file(f"ORDER PLACED (GTC): {direction} | ID: {order_id}")
                def cancel_remain():
                    try:
                        time.sleep(2)
                        cancel_resp = client.cancel(order_id)
                        if cancel_resp:
                            log_to_file(f"Cancelled remaining order {order_id[:12]}...")
                    except Exception as ce:
                        log_to_file(f"Cancel failed for {order_id[:12]}: {ce}")
                threading.Thread(target=cancel_remain, daemon=True).start()
            else:
                status = "failed"
                log_to_file(f"Order failed: {resp}")
        except Exception as e:
            status = "error"
            log_to_file(f"Execution Error: {e}")

    trade = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "window_ts": window_ts,
        "market_slug": slug,
        "direction": direction,
        "token_id": token_id,
        "token_price": token_price,
        "btc_price": btc_price,
        "price_to_beat": price_to_beat,
        "confidence": confidence,
        "order_id": order_id,
        "signals": signals,
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
            f"token=${token_price:.3f} | bet=${final_bet:.2f}"
        )
    if status in ("placed", "simulated"):
        with trades_lock:
            trades = safe_read_json(TRADES_PATH) or []
            trades.append(trade)
            safe_write_json(TRADES_PATH, trades)
        if risk_manager:
            risk_manager.record_trade(direction, final_bet, token_price)
    return {"blocked": False, "status": status, "order_id": order_id}

# ── Bot Main Loop v5.1 ───────────────────────────────────────────────────
def bot_loop():
    global bot_running, current_strategy_info, risk_manager
    load_dotenv(ENV_PATH)

    # Initialize risk manager from config
    cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True}
    risk_manager = RiskManager(cfg, BOT_DIR)
    if risk_manager.state.get("circuit_breaker_tripped"):
        log_to_file("WARNING: Circuit breaker is tripped. Reset it via dashboard to trade.")

    log_to_file("PolyBot v5.1 ENGINE STARTING (Profit-Focused + Risk Managed)")

    market_baselines = {}
    last_market_fetch = 0
    cached_market = None
    last_signal_check = 0
    last_outcome_check = time.time()
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

            # Refresh risk manager config without destroying state
            if risk_manager:
                risk_manager.cfg = cfg

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
                            from py_clob_client.client import ClobClient
                            from py_clob_client.clob_types import ApiCreds
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
                                    "https://clob.polymarket.com", key=pk, chain_id=CHAIN_ID
                                )
                                creds = temp.create_or_derive_api_creds()
                            cached_client = ClobClient(
                                "https://clob.polymarket.com",
                                key=pk,
                                chain_id=CHAIN_ID,
                                creds=creds,
                                signature_type=1,
                                funder=addr
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

            if already_traded:
                with strategy_lock:
                    current_strategy_info.update({
                        "slug": slug, "price_to_beat": price_to_beat,
                        "current_diff": round(diff_pct, 3),
                        "time_remaining": time_remaining,
                        "up_price": up_price, "down_price": down_price,
                        "status": f"Traded ✓ | Waiting for result ({time_remaining}s left)",
                        "confidence": 0, "signals": {},
                        "risk_status": "OK", "risk_reason": ""
                    })
                time.sleep(2)
                continue

            rcfg = cfg.get("risk_management", {})
            min_time = int(rcfg.get("min_time_remaining_to_enter", 120))
            max_time = int(rcfg.get("max_time_remaining_to_enter", 250))
            in_entry_window = min_time <= time_remaining <= max_time

            if now - last_signal_check >= SIGNAL_CHECK_INTERVAL:
                last_signal_check = now
                direction, confidence, signals = analyze_signals(price_to_beat, up_price, down_price)
                risk_status = "OK"
                risk_reason = ""
                if risk_manager:
                    target_price = up_price if direction == "UP" else down_price if direction else 0.5
                    _allowed, risk_reason = risk_manager.can_trade(target_price)
                    if not _allowed:
                        risk_status = "BLOCKED"
                with strategy_lock:
                    current_strategy_info.update({
                        "slug": slug, "price_to_beat": price_to_beat,
                        "current_diff": round(diff_pct, 3),
                        "time_remaining": time_remaining,
                        "up_price": up_price, "down_price": down_price,
                        "edge": f"v5.1 | {direction or 'NONE'} {confidence:.1f}%",
                        "confidence": confidence,
                        "signals": signals,
                        "risk_status": risk_status,
                        "risk_reason": risk_reason
                    })
                if int(now) % 60 < SIGNAL_CHECK_INTERVAL:
                    log_to_file(
                        f"BTC: ${price_now:.2f} | Strike: ${price_to_beat:.2f} | "
                        f"Diff: {diff_pct:+.3f}% | Up: {up_price:.3f} Down: {down_price:.3f} | "
                        f"Time: {time_remaining}s | Dir: {direction or 'NONE'} Conf: {confidence:.1f}% | Risk: {risk_status}"
                    )
                min_conf = float(cfg.get("min_confidence", 65))
                if not in_entry_window:
                    if time_remaining > max_time:
                        status_msg = f"Waiting for entry window... ({time_remaining}s left, enter at {max_time}s)"
                    else:
                        status_msg = f"Too late to enter ({time_remaining}s left, token too expensive)"
                    with strategy_lock:
                        current_strategy_info["status"] = status_msg
                elif direction and confidence >= min_conf:
                    clob_ids = market.get("clobTokenIds", "[]")
                    if isinstance(clob_ids, str):
                        try: clob_ids = json.loads(clob_ids)
                        except: clob_ids = []
                    tokens = market.get("tokens", [])
                    up_token_id = (clob_ids[0] if len(clob_ids) > 0
                                   else (tokens[0].get("tokenId") if tokens else None))
                    down_token_id = (clob_ids[1] if len(clob_ids) > 1
                                     else (tokens[1].get("tokenId") if len(tokens) > 1 else None))
                    target_token_id = up_token_id if direction == "UP" else down_token_id
                    target_price = up_price if direction == "UP" else down_price
                    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                    recent_trades = [t for t in trades if t.get("timestamp", "") > one_hour_ago]
                    max_hour = cfg.get("max_trades_per_hour", 12)
                    if len(recent_trades) >= max_hour:
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
                                f"token=${target_price:.3f} | {time_remaining}s left"
                            )
                        log_to_file(
                            f"TRADE ENTRY: {direction} conf={confidence:.1f}% | "
                            f"BTC ${price_now:.2f} vs strike ${price_to_beat:.2f} ({diff_pct:+.3f}%) | "
                            f"token=${target_price:.3f} | {time_remaining}s remaining"
                        )
                        execute_trade(
                            direction, target_token_id, target_price, price_now,
                            slug, window_ts, confidence, signals, cfg, client, market,
                            price_to_beat
                        )
                else:
                    reasons = []
                    if not direction:
                        reasons.append("no edge detected")
                    elif confidence < min_conf:
                        reasons.append(f"conf {confidence:.1f}% < {min_conf}%")
                    with strategy_lock:
                        current_strategy_info["status"] = f"Analyzing... ({', '.join(reasons) or 'ok'})"

            if len(market_baselines) > 20:
                cutoff = window_ts - 3600
                market_baselines = {k: v for k, v in market_baselines.items() if k > cutoff}

            time.sleep(1)
        except Exception as e:
            log_to_file(f"Bot Loop Error: {e}")
            import traceback
            log_to_file(traceback.format_exc())
            time.sleep(3)

# ── API Routes ─────────────────────────────────────────────────────────────────
@app.route("/status")
def get_status():
    trades = safe_read_json(TRADES_PATH) or []
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = sum(1 for t in trades if t.get("outcome") == "loss")
    total_pnl = sum(float(t.get("pnl", 0) or 0) for t in trades if t.get("pnl") is not None)
    cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True}
    with price_lock:
        live_price = last_btc_price
    with strategy_lock:
        info_snapshot = dict(current_strategy_info)
    resp = {
        "running": bot_running,
        "dry_run": cfg.get("dry_run", True),
        "btc_price": live_price,
        "strategy": "PolyBot v5.1 (Odds-Edge + Risk Managed)",
        "info": info_snapshot,
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "total_pnl": round(total_pnl, 2),
        "success_rate": round((wins / (wins + losses) * 100), 1) if (wins + losses) > 0 else 0,
        "uptime": str(datetime.now(timezone.utc) - start_time).split(".")[0],
        "account": dict(account_stats),
        "bet_size": float(cfg.get("bet_size", 2.0))
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
    """Lightweight health check for uptime monitoring."""
    with price_lock:
        price_ok = last_btc_price > 0
    ws_ok = ws_client.thread is not None and ws_client.thread.is_alive()
    healthy = price_ok and ws_ok
    return jsonify({
        "status": "healthy" if healthy else "degraded",
        "btc_feed": "up" if price_ok else "down",
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
    """Export trade history as JSON or CSV."""
    fmt = request.args.get("format", "json").lower()
    trades = safe_read_json(TRADES_PATH) or []
    if fmt == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "timestamp", "window_ts", "market_slug", "direction", "token_price",
            "btc_price", "price_to_beat", "confidence", "bet_size", "dry_run",
            "status", "outcome", "pnl", "order_id"
        ])
        for t in trades:
            writer.writerow([
                t.get("timestamp", ""),
                t.get("window_ts", ""),
                t.get("market_slug", ""),
                t.get("direction", ""),
                t.get("token_price", ""),
                t.get("btc_price", ""),
                t.get("price_to_beat", ""),
                t.get("confidence", ""),
                t.get("bet_size", ""),
                t.get("dry_run", ""),
                t.get("status", ""),
                t.get("outcome", ""),
                t.get("pnl", ""),
                t.get("order_id", "")
            ])
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=polybot_trades.csv"}
        )
    return jsonify({"trades": trades, "count": len(trades)})

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
    """Manual reset of circuit breaker and daily counters (use with caution)."""
    if risk_manager:
        risk_manager.reset_circuit_breaker()
        log_to_file("Risk circuit breaker manually reset via API")
        return jsonify({"status": "reset"})
    return jsonify({"status": "no_risk_manager"}), 503

@app.route("/config", methods=["GET", "POST"])
@require_api_key
def handle_config():
    if request.method == "POST":
        data = request.get_json()
        # v5.1: Prevent accidentally disabling dry_run without explicit flag
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

if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    app.run(host=host, port=3000)
