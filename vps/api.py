#!/usr/bin/env python3
"""
PolyBot v5.0 - Profit-Focused Trading Engine
Fixed: redemption, outcome tracking, signal engine, entry timing, edge detection
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
from dotenv import load_dotenv

CHAIN_ID = 137
PRICE_BUFFER_SIZE = 300   # 10 min of 2s ticks
SIGNAL_CHECK_INTERVAL = 10

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

# ── Paths ──────────────────────────────────────────────────
BOT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = BOT_DIR / "config.json"
TRADES_PATH = BOT_DIR / "trades.json"
ENV_PATH = BOT_DIR / ".env"
LOG_PATH = BOT_DIR / "bot.log"

# ── Global State ───────────────────────────────────────────
start_time = datetime.now(timezone.utc)
bot_running = False
bot_thread = None
last_btc_price = 0
price_lock = threading.Lock()
strategy_lock = threading.Lock()  # H3 FIX: Protect current_strategy_info from race conditions
trades_lock = threading.Lock()  # M1 FIX: Protect trades.json read-modify-write
price_buffer = []   # list of (timestamp, price)
account_stats = {"balance": 0.0, "pnl": 0.0, "last_updated": 0}
current_strategy_info = {
    "slug": "N/A", "price_to_beat": 0, "current_diff": 0,
    "time_remaining": 0, "up_price": 0, "down_price": 0,
    "edge": "None", "status": "Inactive", "confidence": 0, "signals": {}
}

# ── Binance WebSocket ──────────────────────────────────────
class BinanceWS:
    def __init__(self):
        self.url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        self.ws = None
        self.thread = None
        self.last_buf = 0

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

    def on_error(self, ws, error): pass

    def on_close(self, ws, code, msg):
        time.sleep(5)
        self.run()

    def run(self):
        self.ws = websocket.WebSocketApp(
            self.url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever(ping_interval=30, ping_timeout=10)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

ws_client = BinanceWS()
ws_client.start()

# ── File Helpers ───────────────────────────────────────────
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
def log_to_file(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"{ts} [INFO] {msg}"
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

# ── Signal Engine v5.0 ─────────────────────────────────────
# Core insight: on a 5-min binary market the ONLY real edges are:
#   1. BTC price is already above/below the strike (locked-in position)
#   2. Strong short-term momentum pointing away from the strike
#   3. Polymarket odds are MISPRICED vs actual BTC momentum
# Lagging indicators (RSI/MACD on 5-min) add noise, not signal.

def calc_momentum_score(prices, seconds=60):
    """
    Compute normalized momentum score [-100, +100] over `seconds` window.
    Uses velocity (rate of change) + acceleration (trend of trend).
    """
    if len(prices) < 10:
        return 0
    # last N ticks (2s each, so 60s = 30 ticks)
    n = min(seconds // 2, len(prices))
    recent = prices[-n:]
    if len(recent) < 4:
        return 0
    
    half = len(recent) // 2
    first_half_avg = sum(recent[:half]) / half
    second_half_avg = sum(recent[half:]) / (len(recent) - half)
    
    velocity = (second_half_avg - first_half_avg) / first_half_avg * 100  # % change
    
    # Acceleration: compare last quarter vs second quarter
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
    
    # Weighted: velocity matters more than acceleration
    score = (velocity * 3 + accel) / 4
    # Normalize to [-100, 100]
    return max(-100, min(100, score * 500))  # 0.2% move = score ~100

def calc_odds_edge(current_btc, price_to_beat, up_price, down_price):
    """
    THE PRIMARY EDGE: Are Polymarket odds mispriced vs actual BTC position?
    
    If BTC is currently 0.3% ABOVE the strike, the "Up" token should be
    worth ~65-70 cents, not 50. If it's priced at 50, BUY UP immediately.
    
    Returns: (direction, edge_strength 0-100, reason)
    """
    if not price_to_beat or not current_btc:
        return None, 0, "no_data"
    
    diff_pct = (current_btc - price_to_beat) / price_to_beat * 100
    
    # What the "true" probability should be based on BTC position
    # Using a logistic curve: 0.5% move => ~70% probability
    # 0.1% move => ~55%, 0.2% => ~60%, 0.5% => ~70%, 1%+ => ~80%
    import math
    true_prob_up = 1 / (1 + math.exp(-diff_pct * 8))  # Sigmoid, steepness=8
    true_prob_down = 1 - true_prob_up
    
    # Compare to market pricing
    market_prob_up = up_price    # e.g. 0.52
    market_prob_down = down_price  # e.g. 0.48
    
    edge_up = true_prob_up - market_prob_up      # Positive = UP is underpriced
    edge_down = true_prob_down - market_prob_down  # Positive = DOWN is underpriced
    
    if edge_up > 0.04 and edge_up > edge_down:  # Minimum 4 cent edge
        return "UP", round(edge_up * 100, 1), f"BTC {diff_pct:+.3f}% vs strike, UP mispriced by {edge_up:.3f}"
    elif edge_down > 0.04:
        return "DOWN", round(edge_down * 100, 1), f"BTC {diff_pct:+.3f}% vs strike, DOWN mispriced by {edge_down:.3f}"
    
    return None, 0, f"no_edge (diff={diff_pct:+.3f}%, edge_up={edge_up:.3f}, edge_down={edge_down:.3f})"

def analyze_signals(price_to_beat, up_price, down_price):
    """
    v5.0 Signal Engine: Focuses on ACTUAL edge (odds mispricing + momentum).
    
    Returns: (direction, confidence 0-100, signals_dict)
    """
    with price_lock:
        buf = list(price_buffer)
        current = last_btc_price

    if not current or not price_to_beat or len(buf) < 20:
        return None, 0, {}

    prices = [p for _, p in buf]
    now = time.time()
    window_offset = int(now % 300)
    time_remaining = 300 - window_offset

    # ── Signal 1: Odds Mispricing (MOST IMPORTANT) ──────────
    odds_dir, odds_edge, odds_reason = calc_odds_edge(current, price_to_beat, up_price, down_price)
    
    # ── Signal 2: Short-term momentum (60s window) ──────────
    mom_60 = calc_momentum_score(prices, 60)
    mom_dir_60 = "UP" if mom_60 > 15 else ("DOWN" if mom_60 < -15 else "NEUTRAL")
    
    # ── Signal 3: Ultra-short momentum (20s window) ─────────
    mom_20 = calc_momentum_score(prices, 20)
    mom_dir_20 = "UP" if mom_20 > 20 else ("DOWN" if mom_20 < -20 else "NEUTRAL")
    
    # ── Signal 4: Price locked above/below strike ────────────
    diff_pct = (current - price_to_beat) / price_to_beat * 100
    if diff_pct > 0.15:
        locked_dir = "UP"
        locked_strength = min(diff_pct / 0.5 * 100, 100)  # 0.5% = max strength
    elif diff_pct < -0.15:
        locked_dir = "DOWN"
        locked_strength = min(abs(diff_pct) / 0.5 * 100, 100)
    else:
        locked_dir = "NEUTRAL"
        locked_strength = 0

    # ── Signal 5: Token price ROI check ─────────────────────
    # Only trade when risk/reward makes sense
    # If entering UP at time_remaining=120s, token might be $0.65
    # Win = +$0.35, Loss = -$0.65 => need 65% win rate to break even
    # Entry is only worthwhile when we have real edge
    target_price = up_price if (odds_dir == "UP") else down_price if odds_dir else 0.5
    expected_payout = 1.0 - target_price  # Profit per $1 staked
    expected_loss = target_price           # Loss per $1 staked
    
    # Min win rate needed to break even at this price
    breakeven_winrate = expected_loss / (expected_loss + expected_payout)
    
    # ── Combine Signals ──────────────────────────────────────
    # The key: signals must AGREE on direction + odds must be mispriced
    
    signals_agree = 0
    total_signals = 0
    direction_votes = {"UP": 0, "DOWN": 0}
    
    if odds_dir:
        direction_votes[odds_dir] += 3  # Odds mispricing = 3x weight
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
    agreement_score = direction_votes[final_dir] / total_signals  # 0.0 to 1.0
    
    # Confidence = agreement × odds_edge × ROI quality
    # Only fire when odds are mispriced AND signals agree AND ROI makes sense
    if not odds_dir or odds_dir != final_dir:
        # No odds mispricing in our direction = skip
        confidence = 0
        final_dir = None
    else:
        # Base confidence from agreement
        base_conf = agreement_score * 80  # Max 80 from agreement
        
        # Boost from odds edge size (each cent of edge = +2 confidence)
        edge_boost = min(odds_edge * 2, 20)
        
        # Penalty if ROI is poor (token price > 0.70 means we need 70%+ win rate)
        roi_penalty = 0
        if breakeven_winrate > 0.65:
            roi_penalty = (breakeven_winrate - 0.65) * 200  # -10 per 5% above threshold
        
        confidence = max(0, base_conf + edge_boost - roi_penalty)
    
    signals = {
        "odds_dir": odds_dir,
        "odds_edge": odds_edge,
        "odds_reason": odds_reason,
        "diff_pct": round(diff_pct, 4),
        "locked_dir": locked_dir,
        "locked_strength": round(locked_strength, 1),
        "mom_60s": round(mom_60, 1),
        "mom_60_dir": mom_dir_60,
        "mom_20s": round(mom_20, 1),
        "mom_20_dir": mom_dir_20,
        "token_price": round(target_price, 3),
        "breakeven_wr": round(breakeven_winrate * 100, 1),
        "agreement": round(agreement_score * 100, 1),
        "confidence": round(confidence, 1)
    }
    
    if confidence > 30:
        log_to_file(
            f"📊 SIGNAL v5: {final_dir} conf={confidence:.1f}% | "
            f"edge={odds_edge:.1f}¢ | diff={diff_pct:+.3f}% | "
            f"mom60={mom_60:.1f} | token=${target_price:.3f} | "
            f"breakeven={breakeven_winrate*100:.1f}% | {odds_reason}"
        )
    
    return (final_dir, round(confidence, 1), signals) if final_dir else (None, 0, signals)

# ── Market Data ────────────────────────────────────────────
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
        log_to_file(f"⚠️ Gamma API Error: {e}")
        return None

def get_price_to_beat(window_ts, condition_id=None):
    """
    Get the official strike price for this 5-min window.
    Priority: CLOB API > Binance 1m open > CryptoCompare
    """
    # 1. CLOB API (most accurate)
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

    # 2. Binance 1m open (matches oracle very closely)
    try:
        resp = requests.get(
            f"https://api.binance.com/api/v3/klines"
            f"?symbol=BTCUSDT&interval=1m&startTime={window_ts * 1000}&limit=1",
            timeout=5
        )
        data = resp.json()
        if data and len(data) > 0:
            return float(data[0][1])  # Open price
    except:
        pass

    # 3. CryptoCompare fallback
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

# ── Outcome Tracking (FIXED) ───────────────────────────────
def check_outcomes(baselines):
    """
    FIX: Use the CORRECT resolution price.
    Polymarket BTC 5-min markets resolve using the Chainlink price at the
    START of the next candle (i.e., exactly at window_ts + 300).
    We fetch the OPEN of the Binance 1m candle at that timestamp.
    """
    global bot_running
    # M1 FIX: Lock trades.json read-modify-write
    with trades_lock:
        trades = safe_read_json(TRADES_PATH) or []
        updated = False
        now = time.time()
        wins = 0
        losses = 0

        for t in trades:
            if t.get("outcome") == "win":
                wins += 1
                continue
            elif t.get("outcome") == "loss":
                losses += 1
                continue

            wts = t.get("window_ts", 0)
            resolve_ts = wts + 300  # Resolution timestamp

            # Need at least 60s buffer after resolution
            if now < resolve_ts + 60:
                continue

            base = baselines.get(wts) or t.get("price_to_beat") or get_price_to_beat(wts)
            if not base:
                log_to_file(f"⚠️ No baseline for window {wts}, skipping outcome check")
                continue

            # Fetch the OPEN of the candle AT resolution time (most accurate)
            resolve_open_price = None
            try:
                resp = requests.get(
                    f"https://api.binance.com/api/v3/klines"
                    f"?symbol=BTCUSDT&interval=1m&startTime={resolve_ts * 1000}&limit=1",
                    timeout=5
                )
                data = resp.json()
                if data and len(data) > 0:
                    resolve_open_price = float(data[0][1])  # Open of resolution candle
            except:
                pass

            # L2+M11 FIX: CryptoCompare — use startTime equiv (toTs = resolve_ts + 60 for same candle)
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
            updated = True

            log_to_file(
                f"{'✅ WIN' if win else '❌ LOSS'}: {direction} | "
                f"Strike: ${base:.2f} → Resolve: ${resolve_open_price:.2f} | "
                f"Diff: {(resolve_open_price-base)/base*100:+.3f}%"
            )

            if win:
                wins += 1
            else:
                losses += 1

        if updated:
            safe_write_json(TRADES_PATH, trades)

    # Auto-stop if win rate drops below 45% with enough data
    total = wins + losses
    if total >= 10 and bot_running:
        wr = wins / total * 100
        if wr < 45.0:
            bot_running = False
            log_to_file(f"🛑 AUTO-STOP: Win rate {wr:.1f}% < 45% after {total} trades. Review config.")

# ── Redemption (FIXED - actually works now) ───────────────
def redeem_all_winners():
    """
    Claim all winning positions via the Polymarket relayer.
    Uses the py_clob_client's redeem functionality.
    """
    load_dotenv(ENV_PATH)
    pk = os.getenv("POLY_PRIVATE_KEY")
    addr = os.getenv("POLY_WALLET_ADDRESS")
    
    if not pk or not addr:
        log_to_file("⚠️ Redemption: Missing POLY_PRIVATE_KEY or POLY_WALLET_ADDRESS")
        return
    
    trades = safe_read_json(TRADES_PATH) or []
    unredeemed_wins = [
        t for t in trades
        if t.get("outcome") == "win"
        and not t.get("redeemed")
        and t.get("condition_id")
    ]
    
    if not unredeemed_wins:
        log_to_file("🔄 Redemption: No unredeemed wins found")
        return
    
    log_to_file(f"💰 Redemption: Found {len(unredeemed_wins)} unredeemed wins - processing...")
    
    try:
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
            temp = ClobClient("https://clob.polymarket.com", key=pk, chain_id=CHAIN_ID)
            creds = temp.create_or_derive_api_creds()
        
        client = ClobClient(
            "https://clob.polymarket.com",
            key=pk,
            chain_id=CHAIN_ID,
            creds=creds,
            signature_type=1,
            funder=addr
        )

        redeemed_conditions = set()
        for trade in unredeemed_wins:
            cid = trade.get("condition_id")
            if cid in redeemed_conditions:
                continue
            try:
                # Use ClobClient's proper redeem method (handles auth/signing internally)
                resp = client.redeem_winnings(condition_id=cid)
                if resp and (hasattr(resp, "transactionHash") or
                             (isinstance(resp, dict) and "transactionHash" in resp) or
                             isinstance(resp, str) and resp.startswith("0x")):
                    log_to_file(f"✅ Redeemed condition {cid[:12]}...")
                    redeemed_conditions.add(cid)
                    # Mark all matching trades as redeemed
                    for t2 in trades:
                        if t2.get("condition_id") == cid:
                            t2["redeemed"] = True
                            t2["redeemed_at"] = datetime.now(timezone.utc).isoformat()
                else:
                    log_to_file(f"⚠️ Redeem failed {cid[:12]}: response={resp}")
            except Exception as e:
                err_msg = str(e)
                if "already redeemed" in err_msg.lower() or "no position" in err_msg.lower():
                    log_to_file(f"ℹ️ Redeem skipped {cid[:12]}: {err_msg[:80]}")
                    redeemed_conditions.add(cid)
                    for t2 in trades:
                        if t2.get("condition_id") == cid:
                            t2["redeemed"] = True
                            t2["redeemed_at"] = datetime.now(timezone.utc).isoformat()
                else:
                    log_to_file(f"⚠️ Redeem error {cid[:12]}: {err_msg[:120]}")
        
        safe_write_json(TRADES_PATH, trades)
        log_to_file(f"💰 Redemption complete: {len(redeemed_conditions)} conditions processed")
    
    except ImportError:
        log_to_file("⚠️ py_clob_client not available for redemption")
    except Exception as e:
        log_to_file(f"⚠️ Redemption error: {e}")

# ── Trade Execution ────────────────────────────────────────
def execute_trade(direction, token_id, token_price, btc_price, slug,
                  window_ts, confidence, signals, cfg, client=None, market=None, price_to_beat=0):
    is_dry = cfg.get("dry_run", True)
    status = "simulated"
    order_id = "N/A"
    condition_id = market.get("conditionId") if market else None

    if not is_dry and client:
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType
            bet_size = float(cfg.get("bet_size", 2.0))

            # Balance check
            try:
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                balance_data = client.get_balance_allowance(params)
                if isinstance(balance_data, dict):
                    current_balance = float(balance_data.get("balance", 0)) / 1e6
                else:
                    current_balance = float(balance_data) / 1e6 if balance_data else 0.0

                if current_balance < bet_size:
                    log_to_file(
                        f"⚠️ BALANCE: Need ${bet_size:.2f}, have ${current_balance:.2f}. "
                        f"Triggering redemption..."
                    )
                    threading.Thread(target=redeem_all_winners, daemon=True).start()
                    return
            except Exception as be:
                log_to_file(f"⚠️ Balance check failed: {be}")

            log_to_file(f"🎯 LIVE ORDER: {direction} ${bet_size} @ ${token_price:.3f}")

            # Widen cap to $0.85 to ensure FOK fills on thin 5-min order books
            capped_price = min(token_price + 0.10, 0.85)

            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=bet_size,
                side="BUY",
                price=capped_price
            )
            signed_order = client.create_market_order(order_args)
            
            # Try FOK first (all-or-nothing)
            try:
                resp = client.post_order(signed_order, OrderType.FOK)
                order_type_used = "FOK"
            except Exception as fok_err:
                # FOK failed — fall back to GTC (fills whatever liquidity exists)
                log_to_file(f"⚠️ FOK failed, retrying GTC: {fok_err}")
                resp = client.post_order(signed_order, OrderType.GTC)
                order_type_used = "GTC"

            if resp and (hasattr(resp, "orderID") or
                         (isinstance(resp, dict) and "orderID" in resp)):
                order_id = getattr(resp, "orderID", resp.get("orderID") if isinstance(resp, dict) else "N/A")
                status = "placed"
                log_to_file(f"✅ ORDER PLACED ({order_type_used}): {direction} | ID: {order_id}")
            else:
                status = "failed"
                log_to_file(f"⚠️ Order failed: {resp}")

        except Exception as e:
            status = "error"
            log_to_file(f"⚠️ Execution Error: {e}")

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
        "bet_size": cfg.get("bet_size", 2.0),
        "dry_run": is_dry,
        "status": status,
        "outcome": None,
        "condition_id": condition_id,
        "redeemed": False
    }

    if is_dry:
        log_to_file(
            f"🧪 DRY TRADE: {direction} conf={confidence:.1f}% | "
            f"BTC ${btc_price:.2f} vs strike ${price_to_beat:.2f} | "
            f"token=${token_price:.3f}"
        )

    # M3 FIX: Only write placed trades (not failed/error phantom trades)
    if status == "placed":
        with trades_lock:
            trades = safe_read_json(TRADES_PATH) or []
            trades.append(trade)
            safe_write_json(TRADES_PATH, trades)
    elif status == "simulated":
        # Dry run — always write for tracking
        with trades_lock:
            trades = safe_read_json(TRADES_PATH) or []
            trades.append(trade)
            safe_write_json(TRADES_PATH, trades)

# ── Bot Main Loop ──────────────────────────────────────────
def bot_loop():
    global bot_running, current_strategy_info

    # M10 FIX: Load .env once at startup, not in hot loop
    load_dotenv(ENV_PATH)

    log_to_file("🚀 PolyBot v5.0 ENGINE STARTING (Profit-Focused)")

    market_baselines = {}
    last_market_fetch = 0
    cached_market = None
    last_signal_check = 0
    last_redeem_check = time.time()
    last_outcome_check = time.time()

    # H1 FIX: Cache the ClobClient so it's only created once per session
    cached_client = None
    cached_client_cfg = None  # Track config hash to recreate if dry_run toggles

    while bot_running:
        try:
            cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True}
            now = time.time()
            window_ts = get_current_5min_ts()
            window_offset = int(now % 300)
            time_remaining = 300 - window_offset
            slug = f"btc-updown-5m-{window_ts}"

            # Periodic tasks
            if now - last_outcome_check > 120:
                threading.Thread(
                    target=check_outcomes, args=(dict(market_baselines),), daemon=True
                ).start()
                last_outcome_check = now

            if now - last_redeem_check > 600:  # Every 10 minutes
                threading.Thread(target=redeem_all_winners, daemon=True).start()
                last_redeem_check = now

            # Fetch market data every 30s
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

            # Parse market prices
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

            # Get baseline (strike price) for this window
            if window_ts not in market_baselines:
                line = get_price_to_beat(window_ts, market.get("conditionId"))
                market_baselines[window_ts] = line
                if line:
                    log_to_file(f"🎯 Strike Price Synced: ${line:.2f} | Up: {up_price:.3f} Down: {down_price:.3f}")

            price_to_beat = market_baselines.get(window_ts, 0)

            # Update BTC price display
            with price_lock:
                price_now = last_btc_price

            # H1 FIX: Init live client once and cache it (not every iteration)
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
                            log_to_file("🔗 ClobClient initialized")
                    except Exception as e:
                        log_to_file(f"⚠️ Client init error: {e}")
                        cached_client = None
                client = cached_client
            else:
                # dry_run mode — no client needed
                cached_client = None
                cached_client_cfg = None

            # Check if we already traded this window
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
                        "confidence": 0, "signals": {}
                    })
                time.sleep(2)
                continue

            # ── ENTRY WINDOW LOGIC ───────────────────────────────
            #
            # CORRECT Risk/Reward Math:
            #   Token at $0.50 → Win +$0.50, Lose -$0.50 (1:1, breakeven at 50%)
            #   Token at $0.60 → Win +$0.40, Lose -$0.60 (need 60% win rate)
            #   Token at $0.70 → Win +$0.30, Lose -$0.70 (need 70% win rate)
            #   Token at $0.80 → Win +$0.20, Lose -$0.80 (need 80% win rate!!!)
            #
            # Best entries: 240s-180s remaining (tokens ~$0.45-$0.60, good ROI)
            # Acceptable:   180s-120s remaining (tokens ~$0.55-$0.70)
            # Avoid:        <120s remaining (tokens too expensive)
            # Also avoid:   >270s remaining (not enough momentum data)
            #
            # We run signal checks on interval, but only execute in window.

            if now - last_signal_check >= SIGNAL_CHECK_INTERVAL:
                last_signal_check = now
                direction, confidence, signals = analyze_signals(
                    price_to_beat, up_price, down_price
                )
                with strategy_lock:
                    current_strategy_info.update({
                        "slug": slug, "price_to_beat": price_to_beat,
                        "current_diff": round(diff_pct, 3),
                        "time_remaining": time_remaining,
                        "up_price": up_price, "down_price": down_price,
                        "edge": f"v5 | {direction or 'NONE'} {confidence:.1f}%",
                        "confidence": confidence,
                        "signals": signals
                    })

                # Heartbeat log every 60s
                if int(now) % 60 < SIGNAL_CHECK_INTERVAL:
                    log_to_file(
                        f"🤖 BTC: ${price_now:.2f} | Strike: ${price_to_beat:.2f} | "
                        f"Diff: {diff_pct:+.3f}% | Up: {up_price:.3f} Down: {down_price:.3f} | "
                        f"Time: {time_remaining}s | Dir: {direction or 'NONE'} Conf: {confidence:.1f}%"
                    )

                # Entry conditions
                min_conf = float(cfg.get("min_confidence", 55))
                in_entry_window = 120 <= time_remaining <= 250
                
                if not in_entry_window:
                    if time_remaining > 250:
                        status_msg = f"🕐 Waiting for entry window... ({time_remaining}s left, enter at 250s)"
                    else:
                        status_msg = f"⛔ Too late to enter ({time_remaining}s left, token too expensive)"
                    with strategy_lock:
                        current_strategy_info["status"] = status_msg
                elif direction and confidence >= min_conf:
                    # Validate token list
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

                    # Hourly trade limit
                    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                    recent_trades = [t for t in trades if t.get("timestamp", "") > one_hour_ago]
                    max_hour = cfg.get("max_trades_per_hour", 12)

                    if len(recent_trades) >= max_hour:
                        with strategy_lock:
                            current_strategy_info["status"] = f"⏱ Hourly limit ({len(recent_trades)}/{max_hour})"
                    elif not target_token_id:
                        with strategy_lock:
                            current_strategy_info["status"] = "⚠️ No token ID found"
                    elif not client and not cfg.get("dry_run", True):
                        with strategy_lock:
                            current_strategy_info["status"] = "⚠️ Waiting for live client..."
                    else:
                        with strategy_lock:
                            current_strategy_info["status"] = (
                                f"🚀 ENTERING: {direction} conf={confidence:.1f}% | "
                                f"token=${target_price:.3f} | {time_remaining}s left"
                            )
                        log_to_file(
                            f"🚀 TRADE ENTRY: {direction} conf={confidence:.1f}% | "
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
                        current_strategy_info["status"] = f"🔍 Analyzing... ({', '.join(reasons) or 'ok'})"

            # Prune old baselines
            if len(market_baselines) > 20:
                cutoff = window_ts - 3600
                market_baselines = {k: v for k, v in market_baselines.items() if k > cutoff}

            time.sleep(1)

        except Exception as e:
            log_to_file(f"⚠️ Bot Loop Error: {e}")
            import traceback
            log_to_file(traceback.format_exc())
            time.sleep(3)

# ── API Routes ─────────────────────────────────────────────
@app.route("/status")
def get_status():
    trades = safe_read_json(TRADES_PATH) or []
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = sum(1 for t in trades if t.get("outcome") == "loss")
    cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True}
    with price_lock:
        live_price = last_btc_price
    with strategy_lock:
        info_snapshot = dict(current_strategy_info)
    return jsonify({
        "running": bot_running,
        "dry_run": cfg.get("dry_run", True),
        "btc_price": live_price,
        "strategy": "PolyBot v5.0 (Odds-Edge)",
        "info": info_snapshot,
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "success_rate": round((wins / (wins + losses) * 100), 1) if (wins + losses) > 0 else 0,
        "uptime": str(datetime.now(timezone.utc) - start_time).split(".")[0],
        "account": dict(account_stats),
        "bet_size": float(cfg.get("bet_size", 2.0))
    })

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

    return jsonify({
        "total_trades": len(filtered),
        "wins": wins,
        "losses": losses,
        "success_rate": round((wins / (wins + losses) * 100), 1) if (wins + losses) > 0 else 0,
        "history": filtered[-100:]
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

@app.route("/restart", methods=["POST"])
def restart_bot():
    global bot_running, bot_thread
    if not bot_running:
        return jsonify({"status": "not_running"})
    bot_running = False
    # H2 FIX: Wait for old thread to actually stop before starting new one
    old_thread = bot_thread
    timeout = time.time() + 10  # Max 10s wait
    while old_thread and old_thread.is_alive() and time.time() < timeout:
        time.sleep(0.2)
    bot_running = True
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    return jsonify({"status": "restarted"})

@app.route("/config", methods=["GET", "POST"])
def handle_config():
    if request.method == "POST":
        data = request.get_json()
        safe_write_json(CONFIG_PATH, data)
        return jsonify({"status": "saved"})
    return jsonify(safe_read_json(CONFIG_PATH) or {})

@app.route("/redeem", methods=["POST"])
def trigger_redeem():
    threading.Thread(target=redeem_all_winners, daemon=True).start()
    return jsonify({"status": "redemption_triggered"})

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

@app.route("/clear-trades", methods=["POST"])
def clear_trades():
    try:
        if TRADES_PATH.exists():
            TRADES_PATH.unlink()
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route("/clear-logs", methods=["POST"])
def clear_logs():
    try:
        if LOG_PATH.exists():
            LOG_PATH.unlink()
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

if __name__ == "__main__":
    # H4 FIX: Bind to 0.0.0.0 on VPS (configurable via env var)
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    app.run(host=host, port=3000)
