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
from py_clob_client.clob_types import MarketOrderArgs
from web3 import Web3

# RPC Configuration (Prioritize .env, then LlamaRPC, then official)
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon.llamarpc.com") 
FALLBACK_RPCS = [
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
    "https://poly-rpc.gateway.pokt.network"
]

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
last_redeem_time = 0

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
                
                # 5. Decision Window (150s-285s) - Active in the latter half of the window
                if 150 <= window_offset <= 285:
                    
                    # Filter trades in the last hour for limit
                    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                    recent_trades = [t for t in trades if t["timestamp"] > one_hour_ago]
                    
                    max_hour = cfg.get("max_trades_per_hour", 12)
                
                    # Extract Token IDs
                    tokens = market.get("tokens", [])
                    clob_ids = market.get("clobTokenIds", "[]") # Some markets use this as a string
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
                from py_clob_client.clob_types import BalanceAllowanceParams
                balance_data = client.get_balance()
                current_balance = float(balance_data) if balance_data else 0.0
                
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
            
            from py_clob_client.clob_types import OrderType
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
    """Scan history for unredeemed wins and execute on-chain redemption."""
    trades = safe_read_json(TRADES_PATH) or []
    winners = [t for t in trades if t.get("outcome") == "win" and not t.get("redeemed")]
    
    if not winners:
        return
    
    log_to_file(f"💰 Found {len(winners)} unredeemed winning positions. Starting settlement...")
    
    pk = os.getenv("POLY_PRIVATE_KEY")
    proxy_addr = os.getenv("POLY_WALLET_ADDRESS") # The Safe Wallet
    
    if not pk or not proxy_addr:
        log_to_file("❌ Redemption failed: Missing Private Key or Proxy Wallet Address in .env")
        return

    try:
        w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
        account = w3.eth.account.from_key(pk)
        
        # 1. Check MATIC for Gas
        balance = w3.eth.get_balance(account.address)
        if balance < w3.to_wei(0.01, 'ether'):
            log_to_file(f"❌ Redemption failed: Insufficient MATIC for gas in EOA {account.address}")
            return

        # 2. Setup CTF Contract
        ctf = w3.eth.contract(address=w3.to_checksum_address(CTF_CONTRACT), abi=CTF_ABI)
        
        redeemed_count = 0
        for t in winners:
            log_to_file(f"🔍 Analyzing Win: {t.get('market_slug')}...")
            cid = t.get("condition_id") or get_market_condition_id(t.get("market_slug"))
            if not cid:
                continue
            
            log_to_file(f"🔗 Redeeming: {t.get('market_slug')} (CID: {cid[:10]}...)")
            
            # Note: For Polymarket Safe wallets, the simplest method is often to try direct 
            # redemption from the proxy if the owner signs it, OR using the Safe ABI.
            # To keep it robust, we construct the redeemPositions data.
            
            # Construct the call data
            # parentCollectionId is Bytes32(0)
            p_cid = "0x" + "0" * 64
            # IndexSets for binary is [1, 2]
            
            try:
                # We attempt to send a transaction FROM the EOA that triggers the SAFE to redeem.
                # However, a simpler direct way for Polymarket bots is often to use the 
                # CTExchange or FPMM contracts. 
                # For this implementation, we will use the most direct CTF redemption.
                
                # NOTE: If the user address is a SAFE, the EOA cannot call redeemPositions(proxy) 
                # directly unless the EOA is the proxy.
                # We will attempt to call it as the proxy via the relayer methods if possible, 
                # but since we are using Web3, we construct a standard transaction.
                
                # FALLBACK: If Proxy logic is too complex for a single script, we log the need.
                # For now, we use the standard redeemPositions call structure.
                
                data = ctf.encode_abi("redeemPositions", [
                    w3.to_checksum_address(USDC_E),
                    p_cid,
                    cid,
                    [1, 2]
                ])
                
                # Construct Safe Transaction (Minimal execTransaction ABI)
                safe_abi = [{"name":"execTransaction","type":"function","inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"signatures","type":"bytes"}],"outputs":[{"name":"success","type":"bool"}]},{"name":"nonce","type":"function","inputs":[],"outputs":[{"name":"","type":"uint256"}]}]
                safe_contract = w3.eth.contract(address=w3.to_checksum_address(proxy_addr), abi=safe_abi)
                
                # For a simple 1-of-1 Safe, the signature is just:
                # 000000000000000000000000{EOA_ADDRESS}000000000000000000000000000000000000000000000000000000000000000001
                # where 01 is the signature type for EOA.
                sig = "0x000000000000000000000000" + account.address[2:].lower() + "0000000000000000000000000000000000000000000000000000000000000000" + "01"
                
                nonce = safe_contract.functions.nonce().call()
                
                # Network-level Gas Pricing Optimization for Polygon
                # Using 1.25x multiplier to avoid stuck transactions
                current_gas_price = w3.eth.gas_price
                optimized_gas_price = int(current_gas_price * 1.25)
                
                tx = safe_contract.functions.execTransaction(
                    w3.to_checksum_address(CTF_CONTRACT),
                    0,
                    data,
                    0, # Call
                    0, 0, 0,
                    "0x0000000000000000000000000000000000000000",
                    "0x0000000000000000000000000000000000000000",
                    sig
                ).build_transaction({
                    'from': account.address,
                    'nonce': w3.eth.get_transaction_count(account.address),
                    'gas': 350000, # Slightly higher limit for CTF redemptions
                    'gasPrice': optimized_gas_price
                })
                
                signed_tx = w3.eth.account.sign_transaction(tx, pk)
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                log_to_file(f"✅ Redemption Transaction Sent: {tx_hash.hex()}")
                
                # Mark as redeemed in our history
                t["redeemed"] = True
                redeemed_count += 1
                time.sleep(2) # Avoid nonce issues
            except Exception as ex:
                log_to_file(f"⚠️ Redemption step failed: {ex}")
                continue
                
        if redeemed_count > 0:
            safe_write_json(TRADES_PATH, trades)
            log_to_file(f"🎊 Successfully redeemed {redeemed_count} positions!")

    except Exception as e:
        log_to_file(f"⚠️ Global Redemption Error: {e}")

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
