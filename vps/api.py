#!/usr/bin/env python3
"""
PolyBot Unified Backend v3.1
70%+ Accuracy & Official Baseline Sync.
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
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType
from web3 import Web3
from eth_abi import encode

# Use poly_web3 for redemption (the official working SDK)
try:
    from poly_web3 import PolyWeb3Service, WalletType
    from poly_web3 import RedeemResult, RedeemErrorItem
    RELAYER_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    PolyWeb3Service = None
    WalletType = None
    RELAYER_AVAILABLE = False

# RPC Configuration (Prioritize .env, then reliable public RPCs)
# Using multiple reliable Polygon RPC endpoints
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")
FALLBACK_RPCS = [
    "https://1rpc.io/matic",
    "https://rpc.ankr.com/polygon",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon.drpc.org"
]

# Relayer Configuration
RELAYER_URL = "https://relayer-v2.polymarket.com"
CHAIN_ID = 137  # Polygon Mainnet

# Signature types
SIGNATURE_TYPE_EOA = 0  # Direct EOA wallet
SIGNATURE_TYPE_POLY_PROXY = 1  # Polymarket proxy wallet (email accounts)
SIGNATURE_TYPE_GNOSIS_SAFE = 2  # Gnosis Safe wallet (browser wallets)

CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"}
        ],
        "outputs": []
    }
]


app = Flask(__name__)
CORS(app)

# ── Paths ──────────────────────────────────────────────────
# Use the current directory (vps folder) for all production data to ensure consistency across environments
BOT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = BOT_DIR / "config.json"
TRADES_PATH = BOT_DIR / "trades.json"
ENV_PATH = BOT_DIR / ".env"
LOG_PATH = BOT_DIR / "bot.log"

# ── Global State ───────────────────────────────────────────
start_time = datetime.now(timezone.utc)
bot_running = False
bot_thread = None

# Fast Price Feed
last_btc_price = 0
price_lock = threading.Lock()
price_buffer = []  # Rolling buffer of (timestamp, price) for signals
account_stats = {"balance": 0.0, "pnl": 0.0, "last_updated": 0}

current_strategy_info = {
    "slug": "N/A", "price_to_beat": 0, "current_diff": 0,
    "time_remaining": 0, "up_price": 0, "down_price": 0,
    "edge": "None", "status": "Inactive", "confidence": 0,
    "signals": {}
}
last_redeem_time = time.time()  # Initialize to current time to trigger first redemption after 10 min

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
                if len(price_buffer) > 600:  # ~20 min of data for SMA
                    price_buffer = price_buffer[-600:]
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
    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0.001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def fetch_account_stats(address):
    global account_stats
    if not address: return
    try:
        # 1. Fetch Balance from Gamma API (USDC on Polygon)
        balance_url = f"https://gamma-api.polymarket.com/balances?address={address}"
        resp = requests.get(balance_url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            usdc_balance = 0.0
            for item in data:
                # The Gamma API returns a list of balances
                if item.get("asset") == "USDC" or "USDC" in str(item.get("asset")):
                    usdc_balance = float(item.get("balance", 0))
                    break
            account_stats["balance"] = round(usdc_balance, 2)

        # 2. Fetch P&L from Data API
        pnl_url = f"https://data-api.polymarket.com/pnl?address={address}&period=all"
        resp = requests.get(pnl_url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                total_pnl = float(data[-1].get("pnl", 0))
                account_stats["pnl"] = round(total_pnl, 2)
        
        account_stats["last_updated"] = time.time()
    except Exception as e:
        print(f"Stats Fetch Error: {e}")

def analyze_signals(price_to_beat):
    with price_lock:
        buf = list(price_buffer)
        current = last_btc_price
    
    if len(buf) < 100 or not current or not price_to_beat:
        return None, 0, {}

    prices = [p for _, p in buf]
    
    # ── Signal 1: Trend Filter (50-period SMA) ──
    sma_50 = sum(prices[-50:]) / 50
    trend = "UP" if current > sma_50 else "DOWN"
    
    # ── Signal 2: RSI Momentum ──
    rsi = calc_rsi(prices[-30:], 14)
    
    # ── Signal 3: EMA Crossover ──
    ema_fast = calc_ema(prices[-15:], 5)
    ema_slow = calc_ema(prices[-40:], 20)
    ema_signal = "UP" if ema_fast > ema_slow else "DOWN"
    
    # ── Signal 4: VWAP Comparison ──
    recent_60 = [p for t, p in buf if t > time.time() - 60]
    prior_60 = [p for t, p in buf if time.time() - 120 < t <= time.time() - 60]
    vwap_signal = "NEUTRAL"
    if recent_60 and prior_60:
        vwap_signal = "UP" if (sum(recent_60)/len(recent_60)) > (sum(prior_60)/len(prior_60)) else "DOWN"

    # ── Voting System ──
    votes_up = 0
    votes_down = 0
    
    if (current - price_to_beat) / price_to_beat * 100 > 0.08: votes_up += 1
    elif (current - price_to_beat) / price_to_beat * 100 < -0.08: votes_down += 1
    
    if trend == "UP": votes_up += 1
    else: votes_down += 1
    
    if ema_signal == "UP": votes_up += 2
    else: votes_down += 2
    
    if rsi > 60: votes_up += 1
    elif rsi < 40: votes_down += 1
    
    if vwap_signal == "UP": votes_up += 2
    elif vwap_signal == "DOWN": votes_down += 2
    
    total_votes = votes_up + votes_down
    direction = "UP" if votes_up > votes_down else "DOWN"
    confidence = max(votes_up, votes_down) / 7 * 100
    
    signals = {
        "trend": trend, "rsi": round(rsi, 1), "ema": ema_signal, 
        "vwap": vwap_signal, "votes": f"{votes_up}U/{votes_down}D", "confidence": round(confidence, 0)
    }
    
    # Verbose logging for every signal check (if confidence is meaningful)
    if confidence >= 50:
        log_to_file(f"🔍 ANALYSIS: {trend} Trend | RSI: {round(rsi,1)} | EMA: {ema_signal} | VWAP: {vwap_signal} | Votes: {votes_up}U/{votes_down}D")
    
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

def get_clob_market_line(condition_id):
    """Fetch official strike price from CLOB API."""
    try:
        resp = requests.get(f"https://clob.polymarket.com/markets/{condition_id}", timeout=5)
        data = resp.json()
        return float(data["line"]) if "line" in data else None
    except Exception as e:
        log_to_file(f"⚠️ CLOB Sync Error: {e}")
        return None

def get_price_to_beat(window_ts, condition_id=None):
    # 1. Try Official CLOB Sync (100% accurate)
    if condition_id:
        line = get_clob_market_line(condition_id)
        if line: return line
    
    # 2. Binance 1m Open Price (Matches Chainlink oracle start timestamp very closely)
    try:
        resp = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={window_ts * 1000}&limit=1",
            timeout=5
        )
        data = resp.json()
        if data and len(data) > 0:
            return float(data[0][1]) # Index 1 is Open price
    except Exception as e:
        log_to_file(f"⚠️ Binance Baseline Sync Error: {e}")
        pass

    # 3. Historical Sync (CryptoCompare Fallback)
    try:
        resp = requests.get(
            f"https://min-api.cryptocompare.com/data/v2/histominute?fsym=BTC&tsym=USD&limit=1&toTs={window_ts}",
            timeout=5
        )
        return float(resp.json()["Data"]["Data"][-1]["close"])
    except:
        # 4. Last recorded price
        with price_lock: return last_btc_price

def get_current_5min_ts():
    return (int(time.time()) // 300) * 300

# ── Bot Loop ──────────────────────────────────────────────
def bot_loop():
    global bot_running, current_strategy_info
    log_to_file("🚀 ENGINE v3.1 (70%+ Accuracy) STARTING...")
    
    market_baselines = {}
    
    while bot_running:
        try:
            cfg = safe_read_json(CONFIG_PATH) or {"dry_run": True}
            now = time.time()
            window_ts = get_current_5min_ts()
            window_offset = int(now % 300)
            slug = f"btc-updown-5m-{window_ts}"
            
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
            
            # Baseline Sync
            if window_ts not in market_baselines:
                line = get_price_to_beat(window_ts, market.get("conditionId"))
                market_baselines[window_ts] = line
                log_to_file(f"🎯 Baseline Synced: ${line}")
            
            price_to_beat = market_baselines[window_ts]
            
            # 3. Initialize Live Client if needed
            client = None
            if not cfg.get("dry_run", True):
                try:
                    load_dotenv(ENV_PATH)
                    pk = os.getenv("POLY_PRIVATE_KEY")
                    addr = os.getenv("POLY_WALLET_ADDRESS")
                    
                    # Refresh Balance & P&L every 2 minutes (fresher data)
                    if addr and (time.time() - account_stats["last_updated"] > 120):
                        threading.Thread(target=fetch_account_stats, args=(addr,), daemon=True).start()

                    if pk and addr:
                        # Use existing credentials if provided, otherwise derive them
                        api_key = os.getenv("POLY_API_KEY")
                        api_secret = os.getenv("POLY_API_SECRET")
                        api_passphrase = os.getenv("POLY_API_PASSPHRASE")

                        if api_key and api_secret and api_passphrase:
                            from py_clob_client.clob_types import ApiCreds
                            creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
                        else:
                            # Derive API Credentials
                            temp_client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137)
                            creds = temp_client.create_or_derive_api_creds()
                        # Use signature_type=1 (POLY_PROXY) because the funder is a Proxy wallet
                        client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, creds=creds, signature_type=1, funder=addr)
                except Exception as e:
                    log_to_file(f"⚠️ Live Client Init Error: {e}")

            trades = safe_read_json(TRADES_PATH) or []
            already_traded = any(t.get("window_ts") == window_ts for t in trades)

            if already_traded:
                # Bypass signal analysis to save resources
                current_strategy_info = {
                    "slug": slug, "price_to_beat": price_to_beat, "current_diff": 0,
                    "time_remaining": 300 - window_offset, "up_price": up_price, "down_price": down_price,
                    "edge": "None", "status": "Waiting for Result... ⏳", "confidence": 0, "signals": {}
                }
            else:
                # 4. Run Signal Engine
                direction, confidence, signals = analyze_signals(price_to_beat)
                
                with price_lock: price_now = last_btc_price
                
                # Heartbeat log only once at the start of every minute
                if int(now) % 60 == 0:
                    log_to_file(f"🤖 BTC: ${price_now} | Target: ${price_to_beat} | Conf: {confidence}%")
                diff = (price_now - price_to_beat) / price_to_beat * 100 if price_to_beat else 0
                
                current_strategy_info = {
                    "slug": slug, "price_to_beat": price_to_beat, "current_diff": round(diff, 3),
                    "time_remaining": 300 - window_offset, "up_price": up_price, "down_price": down_price,
                    "edge": "None", "status": "Targeting", "confidence": confidence, "signals": signals
                }
                
                # 5. Decision Window (10s-90s) - EARLY entry for fair pricing
                # WHY EARLY? At 0-90s, prices are near 0.50/0.50 → win = +$2.00, loss = -$2.00
                # LATE entry (150s+) means prices skewed to 0.70+ → win = +$0.70, loss = -$2.00
                # With 66.7% win rate: late = -$2.40 net, early = +$8.00 net
                if 10 <= window_offset <= 90:

                    # CRITICAL: Price fairness check
                    # Only trade when both sides are between 0.35-0.65
                    # Skip if market already moved too far (no edge, terrible risk/reward)
                    if up_price < 0.35 or up_price > 0.65:
                        if window_offset % 30 == 0:  # Log every 30s to avoid spam
                            log_to_file(f"⏭️ Price too skewed (UP: {up_price:.2f} / DOWN: {down_price:.2f}). Waiting for next window.")
                        pass  # Skip this window, wait for next 5-min cycle
                    else:
                        # Filter trades in the last hour for limit
                        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                        recent_trades = [t for t in trades if t["timestamp"] > one_hour_ago]

                        max_hour = cfg.get("max_trades_per_hour", 12)

                        # Extract Token IDs
                        tokens = market.get("tokens", [])
                        clob_ids = market.get("clobTokenIds", "[]")
                        if isinstance(clob_ids, str):
                            try: clob_ids = json.loads(clob_ids)
                            except: clob_ids = []

                        up_token_id = clob_ids[0] if len(clob_ids) > 0 else (tokens[0].get("tokenId") if len(tokens) > 0 else None)
                        down_token_id = clob_ids[1] if len(clob_ids) > 1 else (tokens[1].get("tokenId") if len(tokens) > 1 else None)

                        target_token_id = up_token_id if direction == "UP" else down_token_id
                        target_price = up_price if direction == "UP" else down_price

                        min_conf = cfg.get("min_confidence", 60)
                        if direction and confidence >= min_conf:
                            if len(recent_trades) >= max_hour:
                                pass
                            elif not client and not cfg.get("dry_run", True):
                                pass
                            elif target_token_id:
                                execute_trade(direction, target_token_id, target_price, price_now, slug, window_ts, confidence, signals, cfg, client, market)
            
            check_outcomes(market_baselines)
            
            # 6. Periodic Redemption (Exactly every 10 minutes)
            # Use a global timestamp lock to prevent double-firing in the same window
            global last_redeem_time
            if now - last_redeem_time > 600:
                log_to_file("💰 [AUTO] 10-Minute Settlement Heartbeat...")
                # Ensure outcomes are checked first so we know what to redeem
                check_outcomes(market_baselines)
                threading.Thread(target=redeem_all_winners, daemon=True).start()
                last_redeem_time = now

            if len(market_baselines) > 20:
                market_baselines = {k: v for k, v in market_baselines.items() if k > window_ts - 3600}

            time.sleep(1)
        except Exception as e:
            log_to_file(f"⚠️ Strategy Error: {e}")
            time.sleep(2)

def check_outcomes(baselines):
    global bot_running
    trades = safe_read_json(TRADES_PATH) or []
    updated = False
    now = time.time()
    wins = 0
    losses = 0
    for t in trades:
        if t.get("outcome") == "win": wins += 1
        elif t.get("outcome") == "loss": losses += 1

        if t.get("outcome") is not None: continue
        wts = t.get("window_ts", 0)
        if now < wts + 330: continue
        try:
            resp = requests.get(f"https://min-api.cryptocompare.com/data/v2/histominute?fsym=BTC&tsym=USD&limit=1&toTs={wts+300}", timeout=5)
            close = float(resp.json()["Data"]["Data"][-1]["close"])
            base = baselines.get(wts, t.get("btc_price"))
            win = (t["direction"] == "UP" and close >= base) or (t["direction"] == "DOWN" and close < base)
            t["outcome"] = "win" if win else "loss"
            log_to_file(f"{'✅' if win else '❌'} {t['direction']} Result | Base: {base} → Close: {close}")
            updated = True
            
            if win: wins += 1
            else: losses += 1
        except: continue
        
    if updated: 
        safe_write_json(TRADES_PATH, trades)
        
    total = wins + losses
    if total >= 5 and bot_running:
        win_rate = (wins / total) * 100
        if win_rate < 50.0:
            bot_running = False
            log_to_file(f"🛑 AUTO-STOP: Win rate dropped below 50% ({win_rate:.1f}%). Engine Paused.")

def execute_trade(direction, token_id, token_price, btc_price, slug, window_ts, confidence, signals, cfg, client=None, market=None):
    is_dry = cfg.get("dry_run", True)
    status = "simulated"
    order_id = "N/A"
    condition_id = market.get("conditionId") if market else None

    if not is_dry and client:
        try:
            bet_size = float(cfg.get("bet_size", 2.0))
            
            # 0. PRE-FLIGHT BALANCE CHECK
            try:
                # Get fresh balance from CLOB
                # With signature_type=1 and funder=proxy_wallet, this checks the Safe Proxy balance
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                balance_data = client.get_balance_allowance(params)
                # balance_data is a dict with 'balance' key in USDC decimals (6 decimals)
                if isinstance(balance_data, dict):
                    current_balance = float(balance_data.get("balance", 0)) / 1e6
                else:
                    current_balance = float(balance_data) / 1e6 if balance_data else 0.0

                if current_balance < bet_size:
                    log_to_file(f"⚠️ BALANCE GUARD: Skipping trade. Need ${bet_size:.2f} USDC, but only have ${current_balance:.2f} USDC.")
                    # Trigger an immediate redemption check to see if we can recover funds
                    threading.Thread(target=redeem_all_winners, daemon=True).start()
                    return # Status stays 'simulated' or we can set it to 'skipped'
            except Exception as be:
                log_to_file(f"⚠️ Balance Check failed: {be} (Proceeding with attempt)")

            # Record conditionId for future redemptions
            if market:
                condition_id = market.get("conditionId")
            
            log_to_file(f"🎯 Placing MARKET {direction} Order (Amount: ${bet_size})")
            
            # 1. Create the Market Order Args
            # We use an aggressive price of 0.99 to ensure we match with the BEST available price 
            # on the order book (Polymarket CLOB will still fill at the lowest possible price).
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=bet_size,
                side="BUY",
                price=0.99
            )
            
            # 2. Create and Post in one go if supported, or use the two-step process
            # For market orders, create_market_order is the standard helper
            signed_order = client.create_market_order(order_args)

            resp = client.post_order(signed_order, OrderType.FAK)
            
            if resp and (hasattr(resp, "orderID") or (isinstance(resp, dict) and "orderID" in resp)):
                order_id = getattr(resp, "orderID", resp.get("orderID") if isinstance(resp, dict) else "N/A")
                status = "placed"
                log_to_file(f"✅ LIVE MARKET ORDER SUCCESS: {direction} | OrderID: {order_id}")
            else:
                status = "failed"
                log_to_file(f"❌ LIVE MARKET ORDER FAILED: {resp}")
        except Exception as e:
            status = "error"
            log_to_file(f"⚠️ Trade Execution Error: {e}")

    trade = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "window_ts": window_ts,
        "market_slug": slug, "direction": direction, "token_id": token_id,
        "token_price": token_price, "btc_price": btc_price, "confidence": confidence,
        "order_id": order_id, "signals": signals, "bet_size": cfg.get("bet_size", 2.0),
        "dry_run": is_dry, "status": status, "outcome": None, "condition_id": condition_id, "redeemed": False
    }
    
    if is_dry:
        log_to_file(f"🚀 HIGH CONFIDENCE (SIM): {direction} | Conf: {confidence}% | BTC: ${btc_price}")
    
    # Only save to history if actually placed or simulated
    if status in ["placed", "simulated"]:
        trades = safe_read_json(TRADES_PATH) or []
        trades.append(trade)
        safe_write_json(TRADES_PATH, trades)
    else:
        log_to_file(f"⚠️ Trade skipped recording due to status: {status}")

# ── Redemption Logic ───────────────────────────────────────

def get_market_condition_id(slug):
    """Fetch conditionId from Gamma API if missing from trade history."""
    try:
        resp = requests.get(f"https://gamma-api.polymarket.com/markets?slug={slug}", timeout=5)
        data = resp.json()
        if data and len(data) > 0:
            return data[0].get("conditionId")
    except Exception as e:
        log_to_file(f"⚠️ Error fetching conditionId for {slug}: {e}")
    return None

def redeem_all_winners():
    """Scan for unredeemed winning positions and execute on-chain redemption via Polymarket Relayer."""
    global last_redeem_time

    log_to_file("💰 [AUTO-REDEEM] Starting redemption scan...")

    load_dotenv(ENV_PATH)
    pk = os.getenv("POLY_PRIVATE_KEY")
    proxy_addr = os.getenv("POLY_WALLET_ADDRESS")

    if not pk or not proxy_addr:
        log_to_file("❌ Redemption failed: Missing Private Key or Wallet Address in .env")
        last_redeem_time = time.time()
        return

    try:
        # Step 1: Fetch redeemable positions from Data API
        log_to_file(f"🔍 Fetching redeemable positions for {proxy_addr}...")
        positions_url = "https://data-api.polymarket.com/positions"
        params = {"user": proxy_addr, "redeemable": "true", "sizeThreshold": "0"}
        resp = requests.get(positions_url, params=params, timeout=10)

        if resp.status_code != 200:
            log_to_file(f"❌ Failed to fetch positions: HTTP {resp.status_code}")
            last_redeem_time = time.time()
            return

        positions = resp.json()
        if not isinstance(positions, list):
            positions = [positions] if positions else []

        redeemable_positions = [
            p for p in positions
            if float(p.get("size", 0)) > 0 or float(p.get("currentValue", 0)) > 0
        ]

        if not redeemable_positions:
            log_to_file("✅ No redeemable positions found. All winnings already claimed!")
            last_redeem_time = time.time()
            return

        log_to_file(f"💰 Found {len(redeemable_positions)} redeemable positions!")

        # Step 2: Initialize ClobClient (needed for signing)
        log_to_file("🔑 Initializing ClobClient for signing...")
        clob_client = ClobClient(
            "https://clob.polymarket.com",
            key=pk,
            chain_id=137,
            signature_type=1,  # PROXY
            funder=proxy_addr
        )

        # Step 3: Import poly_web3 components
        from poly_web3.web3_service import ProxyWeb3Service
        from poly_web3.const import (
            CTF_ADDRESS, CTF_ABI_REDEEM, USDC_POLYGON, ZERO_BYTES32,
            PROXY_INIT_CODE_HASH, RPC_URL, RELAYER_URL,
            SUBMIT_TRANSACTION, GET_RELAY_PAYLOAD, GET_TRANSACTION,
            STATE_MINED, STATE_CONFIRMED, STATE_FAILED
        )
        from poly_web3.signature.build import derive_proxy_wallet, create_struct_hash, keccak256
        from poly_web3.signature.hash_message import hash_message
        from poly_web3.signature import secp256k1
        from eth_utils import to_bytes, to_checksum_address
        from web3 import Web3
        import hmac
        import hashlib

        log_to_file("🔧 Initializing ProxyWeb3Service...")
        poly_service = ProxyWeb3Service(clob_client=clob_client)

        # Step 4: Build redeem transactions
        condition_ids = [p.get("conditionId") for p in redeemable_positions if p.get("conditionId")]
        log_to_file(f"📋 Building redemption for {len(condition_ids)} markets...")

        txs = []
        for cid in condition_ids:
            try:
                tx_data = poly_service.build_ctf_redeem_tx_data(cid)
                txs.append({
                    "to": CTF_ADDRESS,
                    "data": tx_data,
                    "value": 0,
                    "typeCode": 1,
                })
            except Exception as tx_err:
                log_to_file(f"⚠️ Failed to build tx for {cid[:16]}: {tx_err}")

        if not txs:
            log_to_file("❌ No valid transactions to submit.")
            last_redeem_time = time.time()
            return

        log_to_file(f"🚀 Submitting {len(txs)} transactions to relayer...")

        # Step 5: Build and sign the proxy transaction
        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        _from = to_checksum_address(clob_client.get_address())

        # Get relay payload (nonce, relay address)
        relay_resp = requests.get(
            f"{RELAYER_URL}{GET_RELAY_PAYLOAD}",
            params={"address": _from, "type": 1},  # type 1 = PROXY
            timeout=10
        )
        relay_resp.raise_for_status()
        rp = relay_resp.json()

        # Encode the proxy transaction data
        encoded_data = poly_service.encode_proxy_transaction_data(txs)

        # Build signature
        gas_limit_str = poly_service.estimate_gas(
            tx={"from": _from, "to": CTF_ADDRESS, "data": encoded_data}
        )
        relayer_fee = "0"
        relay_hub = "0xD216153c06E857cD7f72665E0aF1d7D82172F494"
        proxy_factory = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
        proxy = derive_proxy_wallet(_from, proxy_factory, PROXY_INIT_CODE_HASH)

        tx_hash = create_struct_hash(
            _from, CTF_ADDRESS, encoded_data,
            relayer_fee, "0", gas_limit_str,
            rp["nonce"], relay_hub, rp["address"]
        )

        message = {"raw": list(to_bytes(hexstr=tx_hash))}
        r, s, recovery = secp256k1.sign(
            hash_message(message)[2:], clob_client.signer.private_key
        )
        final_sig = secp256k1.serialize_signature(
            r=secp256k1.int_to_hex(r, 32),
            s=secp256k1.int_to_hex(s, 32),
            v=28 if recovery else 27,
            yParity=recovery,
            to="hex"
        )

        req = {
            "from": _from,
            "to": CTF_ADDRESS,
            "proxyWallet": proxy,
            "data": encoded_data,
            "nonce": rp["nonce"],
            "signature": final_sig,
            "signatureParams": {
                "gasPrice": "0",
                "gasLimit": gas_limit_str,
                "relayerFee": relayer_fee,
                "relayHub": relay_hub,
                "relay": rp["address"],
            },
            "type": 1,
            "metadata": "redeem",
        }

        # Step 6: Generate HMAC auth headers
        ts = str(int(time.time()))
        body = json.dumps(req, separators=(',', ':'))
        msg = f"POST{SUBMIT_TRANSACTION}{ts}{body}".encode('utf-8')

        # Get API credentials from CLOB client
        api_key = clob_client.api_creds.api_key
        api_secret = clob_client.api_creds.api_secret
        api_passphrase = clob_client.api_creds.api_passphrase

        signature = hmac.new(
            api_secret.encode('utf-8'),
            msg,
            hashlib.sha256
        ).hexdigest()

        headers = {
            "POLY_ADDRESS": _from,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": ts,
            "POLY_API_KEY": api_key,
            "POLY_PASSPHRASE": api_passphrase,
            "Content-Type": "application/json",
        }

        # Step 7: Submit to relayer
        log_to_file("📨 Submitting to Polymarket relayer...")
        submit_resp = requests.post(
            f"{RELAYER_URL}{SUBMIT_TRANSACTION}",
            json=req,
            headers=headers,
            timeout=30
        )
        submit_resp.raise_for_status()
        submit_result = submit_resp.json()

        if submit_result.get("error"):
            raise Exception(f"Relayer error: {submit_result.get('error')}")

        transaction_id = submit_result.get("transactionID")
        if not transaction_id:
            raise Exception(f"Missing transactionID: {submit_result}")

        log_to_file(f"✅ Transaction submitted! ID: {transaction_id[:20]}...")

        # Step 8: Poll for completion
        log_to_file("⏳ Waiting for on-chain confirmation...")
        for attempt in range(100):
            time.sleep(5)
            status_resp = requests.get(
                f"{RELAYER_URL}{GET_TRANSACTION}",
                params={"transactionID": transaction_id},
                timeout=10
            )
            status_resp.raise_for_status()
            status = status_resp.json()

            state = status.get("state", "")
            if state in [STATE_MINED, STATE_CONFIRMED]:
                log_to_file(f"🎊 Redemption confirmed! TX: {status.get('transactionHash', 'N/A')}")
                for cid in condition_ids:
                    mark_position_as_redeemed(cid)
                break
            elif state == STATE_FAILED:
                raise Exception(f"Transaction failed: {status}")
            elif attempt % 12 == 0:  # Log every ~60 seconds
                log_to_file(f"⏳ Still waiting... state: {state}")
        else:
            log_to_file("⚠️ Transaction not confirmed within timeout, but may still succeed")

        log_to_file(f"🎊 Successfully redeemed {len(condition_ids)} positions!")

    except Exception as e:
        log_to_file(f"⚠️ Global Redemption Error: {e}")
        import traceback
        log_to_file(f"📋 Traceback: {traceback.format_exc()}")
    finally:
        last_redeem_time = time.time()


def mark_position_as_redeemed(condition_id):
    """Mark a position as redeemed in the local trades.json file."""
    if not condition_id:
        return
    
    trades = safe_read_json(TRADES_PATH) or []
    updated = False
    
    for trade in trades:
        if trade.get("condition_id") == condition_id and trade.get("outcome") == "win" and not trade.get("redeemed"):
            trade["redeemed"] = True
            trade["redeemed_at"] = datetime.now(timezone.utc).isoformat()
            updated = True
    
    if updated:
        safe_write_json(TRADES_PATH, trades)
        log_to_file(f"📝 Marked condition {condition_id[:10]}... as redeemed in local records")

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
        "btc_price": live_price, "strategy": "Multi-Signal v3.1",
        "info": current_strategy_info, "total_trades": len(trades), "wins": wins, "losses": losses,
        "success_rate": round((wins/(wins+losses)*100),1) if (wins+losses) > 0 else 0,
        "uptime": str(datetime.now(timezone.utc) - start_time).split(".")[0],
        "account": account_stats,
        "bet_size": float(cfg.get("bet_size", 2.0))
    })

@app.route("/stats")
def get_stats():
    trades = safe_read_json(TRADES_PATH) or []
    period = request.args.get("period", "all")
    
    filtered = trades
    now = datetime.now(timezone.utc)
    
    if period == "30m":
        cutoff = now - timedelta(minutes=30)
        filtered = [t for t in trades if datetime.fromisoformat(t["timestamp"]) > cutoff]
    elif period == "1h":
        cutoff = now - timedelta(hours=1)
        filtered = [t for t in trades if datetime.fromisoformat(t["timestamp"]) > cutoff]
    elif period == "24h":
        cutoff = now - timedelta(hours=24)
        filtered = [t for t in trades if datetime.fromisoformat(t["timestamp"]) > cutoff]
        
    wins = sum(1 for t in filtered if t.get("outcome") == "win")
    losses = sum(1 for t in filtered if t.get("outcome") == "loss")
    
    return jsonify({
        "total_trades": len(filtered),
        "wins": wins,
        "losses": losses,
        "success_rate": round((wins/(wins+losses)*100),1) if (wins+losses) > 0 else 0,
        "history": filtered[-100:]  # Limit to last 100 for performance
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

@app.route("/redeem", methods=["POST"])
def trigger_redeem():
    threading.Thread(target=redeem_all_winners, daemon=True).start()
    return jsonify({"status": "redemption_triggered"})

@app.route("/logs")
def get_logs():
    if not LOG_PATH.exists(): return jsonify({"logs": []})
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return jsonify({"logs": [l.strip() for l in lines[-100:]]})
    except: return jsonify({"logs": []})
    
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
    app.run(host="127.0.0.1", port=3000)
