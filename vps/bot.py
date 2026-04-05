#!/usr/bin/env python3
"""
PolyBot - Bitcoin 5-Min Market Trader for Polymarket
Momentum + RSI strategy with dry-run mode
"""

import time
import json
import logging
import os
import sys
import signal
import requests
from datetime import datetime, timezone
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
    "scan_interval_seconds": 60,
}

# ── Globals ────────────────────────────────────────────────
running = True
price_history = deque(maxlen=60)  # ~30 min of data at 30s intervals
trades_this_hour = 0
hour_start = None
consecutive_losses = 0
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
        logging.FileHandler(str(LOG_PATH)),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("polybot")


# ── Helpers ────────────────────────────────────────────────
def load_env():
    env = {}
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


def load_config():
    global config
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                saved = json.load(f)
            config = {**DEFAULT_CONFIG, **saved}
        except Exception:
            config = dict(DEFAULT_CONFIG)
    else:
        config = dict(DEFAULT_CONFIG)
        save_config()
    return config


def save_config():
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def load_trades():
    if TRADES_PATH.exists():
        try:
            with open(TRADES_PATH) as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_trade(trade):
    trades = load_trades()
    trades.append(trade)
    # Keep last 500 trades
    if len(trades) > 500:
        trades = trades[-500:]
    with open(TRADES_PATH, "w") as f:
        json.dump(trades, f, indent=2)


def write_pid():
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))


def remove_pid():
    if PID_PATH.exists():
        PID_PATH.unlink()


# ── Price Feed ─────────────────────────────────────────────
def get_btc_price():
    """Fetch BTC/USDT price from Binance (no API key needed)."""
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as e:
        log.warning(f"⚠️  Binance price fetch failed: {e}")
        return None


# ── Indicators ─────────────────────────────────────────────
def calculate_momentum(prices, lookback=4):
    """Calculate price momentum over lookback periods.
    lookback=4 at 30s intervals = ~2 minutes."""
    if len(prices) < lookback + 1:
        return 0.0
    current = prices[-1]
    past = prices[-(lookback + 1)]
    return (current - past) / past


def calculate_rsi(prices, period=10):
    """Calculate RSI-like oscillator."""
    if len(prices) < period + 1:
        return 50.0  # neutral

    changes = []
    for i in range(-period, 0):
        changes.append(prices[i] - prices[i - 1])

    gains = [c for c in changes if c > 0]
    losses = [-c for c in changes if c < 0]

    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_volatility(prices, period=10):
    """Simple volatility measure (std dev of returns)."""
    if len(prices) < period + 1:
        return 0.0
    returns = []
    for i in range(-period, 0):
        r = (prices[i] - prices[i - 1]) / prices[i - 1]
        returns.append(r)
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return variance ** 0.5


def generate_signal(prices):
    """Generate trading signal based on momentum + RSI.
    Returns: ('UP', confidence), ('DOWN', confidence), or ('SKIP', 0)
    """
    if len(prices) < config["rsi_period"] + 2:
        return "SKIP", 0.0

    mom = calculate_momentum(prices, lookback=4)
    rsi = calculate_rsi(prices, period=config["rsi_period"])
    vol = calculate_volatility(prices, period=config["rsi_period"])
    threshold = config["momentum_threshold"]

    log.info(
        f"📊 Momentum: {mom:+.6f} | RSI: {rsi:.1f} | "
        f"Volatility: {vol:.6f} | Threshold: {threshold:.6f}"
    )

    # Calculate confidence (0-100)
    mom_strength = abs(mom) / threshold if threshold > 0 else 0
    confidence = min(mom_strength * 50, 80)

    # RSI confirmation boost
    if mom > threshold and rsi < config["rsi_overbought"]:
        confidence += 10
    elif mom < -threshold and rsi > config["rsi_oversold"]:
        confidence += 10

    # Skip if volatility too low (choppy/flat market)
    if vol < threshold * 0.3:
        log.info("⏸️  Low volatility - skipping")
        return "SKIP", 0.0

    # Bullish signal
    if mom > threshold and rsi < config["rsi_overbought"]:
        return "UP", min(confidence, 90)

    # Bearish signal
    if mom < -threshold and rsi > config["rsi_oversold"]:
        return "DOWN", min(confidence, 90)

    return "SKIP", 0.0


# ── Market Discovery ──────────────────────────────────────
def find_btc_5min_market(clob_client):
    """Find the active Bitcoin Up or Down 5-minute market."""
    try:
        # Search via Gamma API (public, no auth needed)
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "limit": 100, "order": "endDate", "ascending": "false"},
            timeout=15,
        )
        resp.raise_for_status()
        markets = resp.json()

        for market in markets:
            q = market.get("question", "").lower()
            if "bitcoin" in q and ("5 min" in q or "5-min" in q) and ("up or down" in q or "up/down" in q):
                tokens = market.get("tokens", [])
                if tokens:
                    log.info(f"🎯 Found market: {market.get('question', '')[:80]}")
                    return {
                        "question": market.get("question", ""),
                        "condition_id": market.get("conditionId", ""),
                        "slug": market.get("slug", ""),
                        "tokens": tokens,
                        "end_date": market.get("endDate", ""),
                        "minimum_tick_size": market.get("minimumTickSize", "0.01"),
                    }

        # Fallback: try CLOB search
        try:
            clob_markets = clob_client.get_markets()
            if clob_markets and "data" in clob_markets:
                for m in clob_markets["data"]:
                    q = m.get("question", "").lower()
                    if "bitcoin" in q and "5 min" in q:
                        tokens = m.get("tokens", [])
                        if tokens:
                            log.info(f"🎯 Found market (CLOB): {m.get('question', '')[:80]}")
                            return {
                                "question": m.get("question", ""),
                                "condition_id": m.get("condition_id", ""),
                                "tokens": tokens,
                                "minimum_tick_size": m.get("minimum_tick_size", "0.01"),
                            }
        except Exception as e:
            log.warning(f"CLOB market search failed: {e}")

        log.warning("⚠️  No active BTC 5-min market found")
        return None

    except Exception as e:
        log.error(f"Market discovery error: {e}")
        return None


def get_token_id_for_direction(market, direction):
    """Get the token_id for UP (Yes) or DOWN (No) from market data."""
    tokens = market.get("tokens", [])
    for token in tokens:
        outcome = token.get("outcome", "").lower()
        if direction == "UP" and outcome in ("yes", "up"):
            return token.get("token_id", "")
        if direction == "DOWN" and outcome in ("no", "down"):
            return token.get("token_id", "")
    # Fallback: first token = Yes/Up, second = No/Down
    if len(tokens) >= 2:
        return tokens[0].get("token_id", "") if direction == "UP" else tokens[1].get("token_id", "")
    return None


# ── Trade Execution ───────────────────────────────────────
def execute_trade(clob_client, market, direction, confidence, btc_price):
    """Execute a trade or simulate in dry run mode."""
    global trades_this_hour, consecutive_losses

    token_id = get_token_id_for_direction(market, direction)
    if not token_id:
        log.error("❌ Could not find token_id for direction: " + direction)
        return

    bet_size = config["bet_size"]
    # Price: buy at slight premium to improve fill rate
    order_price = 0.52 if direction == "UP" else 0.52
    tick_size = market.get("minimum_tick_size", "0.01")

    trade_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "direction": direction,
        "confidence": round(confidence, 1),
        "btc_price": btc_price,
        "bet_size": bet_size,
        "order_price": order_price,
        "token_id": token_id[:16] + "...",
        "market": market.get("question", "")[:60],
        "dry_run": config["dry_run"],
        "status": "pending",
        "outcome": None,
    }

    if config["dry_run"]:
        log.info(
            f"🧪 DRY RUN | {direction} @ ${btc_price:,.2f} | "
            f"Confidence: {confidence:.0f}% | Size: ${bet_size}"
        )
        trade_record["status"] = "simulated"
        save_trade(trade_record)
        trades_this_hour += 1
        return

    # ── Live trading ──
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        log.info(
            f"💰 LIVE TRADE | {direction} @ ${btc_price:,.2f} | "
            f"Confidence: {confidence:.0f}% | Size: ${bet_size}"
        )

        order_args = OrderArgs(
            token_id=token_id,
            price=order_price,
            size=bet_size,
            side=BUY,
        )

        response = clob_client.create_and_post_order(
            order_args,
            {"tick_size": tick_size, "neg_risk": False},
            OrderType.GTC,
        )

        order_id = response.get("orderID", "unknown")
        status = response.get("status", "unknown")
        log.info(f"✅ Order placed: {order_id} | Status: {status}")

        trade_record["status"] = status
        trade_record["order_id"] = order_id
        save_trade(trade_record)
        trades_this_hour += 1

    except Exception as e:
        log.error(f"❌ Trade execution failed: {e}")
        trade_record["status"] = "error"
        trade_record["error"] = str(e)
        save_trade(trade_record)


# ── Rate Limiter ──────────────────────────────────────────
def check_rate_limit():
    global trades_this_hour, hour_start

    now = datetime.now(timezone.utc)
    if hour_start is None or (now - hour_start).total_seconds() > 3600:
        hour_start = now
        trades_this_hour = 0

    if trades_this_hour >= config["max_trades_per_hour"]:
        log.info(f"⏳ Rate limit reached ({trades_this_hour}/{config['max_trades_per_hour']} trades/hr)")
        return False

    if consecutive_losses >= config["max_consecutive_losses"]:
        log.warning(f"🚨 {consecutive_losses} consecutive losses - pausing trades")
        return False

    return True


# ── Main Loop ─────────────────────────────────────────────
def main():
    global running

    log.info("=" * 50)
    log.info("🤖 POLYBOT v2.0 STARTING")
    log.info("=" * 50)

    # Load config
    load_config()
    log.info(f"📋 Mode: {'🧪 DRY RUN' if config['dry_run'] else '💰 LIVE TRADING'}")
    log.info(f"📋 Bet size: ${config['bet_size']}")
    log.info(f"📋 Max trades/hr: {config['max_trades_per_hour']}")
    log.info(f"📋 Momentum threshold: {config['momentum_threshold']}")

    # Write PID file
    write_pid()
    log.info(f"📋 PID: {os.getpid()}")

    # Load env
    env = load_env()
    PRIVATE_KEY = env.get("PRIVATE_KEY", "")
    API_KEY = env.get("API_KEY", "")
    API_SECRET = env.get("API_SECRET", "")
    API_PASSPHRASE = env.get("API_PASSPHRASE", "")
    WALLET = env.get("WALLET", "")

    if WALLET:
        log.info(f"💳 Wallet: {WALLET[:10]}...{WALLET[-4:]}")

    # Connect to Polymarket
    clob_client = None
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON

        creds = None
        if API_SECRET and API_PASSPHRASE:
            creds = {
                "api_key": API_KEY,
                "api_secret": API_SECRET,
                "api_passphrase": API_PASSPHRASE,
            }

        clob_client = ClobClient(
            "https://clob.polymarket.com",
            key=PRIVATE_KEY,
            chain_id=POLYGON,
            signature_type=0,
            funder=WALLET,
        )

        if creds:
            clob_client.set_api_creds(creds)
            log.info("✅ Connected with full API credentials")
        else:
            # Try to derive creds
            try:
                derived = clob_client.create_or_derive_api_creds()
                clob_client.set_api_creds(derived)
                log.info("✅ Derived API credentials automatically")
                # Save them for future use
                if hasattr(derived, "api_key"):
                    with open(ENV_PATH, "a") as f:
                        f.write(f"\nAPI_SECRET={derived.api_secret}\n")
                        f.write(f"API_PASSPHRASE={derived.api_passphrase}\n")
                    log.info("💾 Saved derived credentials to .env")
            except Exception as e:
                log.warning(f"⚠️  Could not derive API creds: {e}")
                log.warning("⚠️  Bot will run in SCAN-ONLY mode (no trading)")

        log.info("✅ Connected to Polymarket CLOB")

    except Exception as e:
        log.error(f"❌ Polymarket connection error: {e}")
        log.warning("⚠️  Running in price-monitoring mode only")

    # ── Main trading loop ──
    last_trade_time = 0
    last_market_scan = 0
    active_market = None

    log.info("🚀 Entering main trading loop")

    while running:
        try:
            # Reload config each iteration (allows dashboard changes)
            load_config()

            # Fetch BTC price
            price = get_btc_price()
            if price is None:
                time.sleep(10)
                continue

            price_history.append(price)
            log.info(f"💲 BTC: ${price:,.2f} | History: {len(price_history)} samples")

            # Scan for market periodically (every 5 minutes)
            now = time.time()
            if clob_client and (now - last_market_scan > 300 or active_market is None):
                active_market = find_btc_5min_market(clob_client)
                last_market_scan = now

            # Generate signal
            prices_list = list(price_history)
            direction, confidence = generate_signal(prices_list)

            if direction != "SKIP":
                log.info(f"📡 Signal: {direction} | Confidence: {confidence:.0f}%")

                # Check rate limits and cooldown
                can_trade = check_rate_limit()
                cooldown_ok = (now - last_trade_time) >= config["cooldown_seconds"]

                if can_trade and cooldown_ok:
                    if active_market and clob_client:
                        execute_trade(clob_client, active_market, direction, confidence, price)
                        last_trade_time = now
                    elif config["dry_run"]:
                        # Even without market, log the simulated trade
                        trade_record = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "direction": direction,
                            "confidence": round(confidence, 1),
                            "btc_price": price,
                            "bet_size": config["bet_size"],
                            "dry_run": True,
                            "status": "simulated",
                            "market": "BTC Up/Down 5-Min (no active market)",
                            "outcome": None,
                        }
                        save_trade(trade_record)
                        log.info(
                            f"🧪 DRY RUN | {direction} @ ${price:,.2f} | "
                            f"Confidence: {confidence:.0f}%"
                        )
                        last_trade_time = now
                    else:
                        log.warning("⚠️  Signal generated but no active market found")
                elif not cooldown_ok:
                    remaining = config["cooldown_seconds"] - (now - last_trade_time)
                    log.info(f"⏳ Cooldown: {remaining:.0f}s remaining")

            # Sleep before next price check
            time.sleep(config["price_poll_seconds"])

        except KeyboardInterrupt:
            running = False
        except Exception as e:
            log.error(f"❌ Loop error: {e}")
            time.sleep(30)

    log.info("🛑 PolyBot stopped gracefully")
    remove_pid()


if __name__ == "__main__":
    main()
