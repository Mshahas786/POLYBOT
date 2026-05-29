# PolyBot v6.0

**Multi-Strategy Automated Trading Bot for Polymarket BTC Up/Down 5-Minute Markets**

PolyBot is a full-featured trading bot for Polymarket's binary options markets. It evaluates 14 signal strategies, applies 9 safety guards, runs a Bayesian confidence adjuster, and serves a real-time dashboard — all in a single Python process.

## Features

### Signal Strategies
| Module | Description |
|---|---|
| **Arbitrage** | Exploit price discrepancies between related tokens/markets |
| **Latency Arb** | Trade based on Chainlink oracle lag creating price discrepancies |
| **Window Delta** | Trade relative price movements within a rolling time window |
| **Wall + Momentum** | Combine order book wall analysis with momentum signals |
| **Wall Bias** | Signal direction based on order book wall imbalance |
| **Oracle Lag** | Detect and trade oracle lag between price feeds |
| **Vol Edge** | Detect edges using market volatility patterns |
| **Cheap Side Reversal** | Bet on the cheaper outcome expecting reversal at resolution |
| **Momentum Only** | Pure momentum-based entry signals |
| **Mean Reversion** | Bet against extreme price moves |
| **Delta Override** | Override signal direction when price delta is extreme |
| **Resolution Hunt** | Seek favorable resolution prices as expiry approaches |
| **Hard Deadline** | Aggressively fill orders in the final seconds |
| **Arb Hedge** | Hedge arbitrage positions to reduce directional risk |

### Safety Guards
| Guard | Description |
|---|---|
| **Momentum Consistency** | Require sustained momentum before trade entry |
| **Volatility Guard** | Block trades during excessive volatility |
| **Volume Confirmation** | Require sufficient trading volume |
| **Trend Bias Filter** | Filter signals against the prevailing trend |
| **Consecutive Loss Guard** | Halt after consecutive losses in same direction |
| **Stale Feed Guard** | Block trades when price feed is stale |
| **Signal Cooldown** | Enforce minimum time between signals |
| **Fee-Aware Gate** | Account for trading fees in edge calculation |
| **Edge Block (Mid)** | Block trades when price is near the 0.50 midpoint |

### Risk Management
- Daily loss limit (USDC)
- Hourly loss percentage cap
- Max drawdown % from peak bankroll
- Max consecutive losses circuit breaker
- Cooldown timers after losses
- Position sizing via bankroll % or Kelly Criterion
- Auto-stop based on rolling win rate

### Dashboard
Web-based real-time dashboard served alongside the API:
- **Dashboard**: BTC price, Chainlink price, countdown clock, signal confidence bar, performance stats, risk panel
- **Trades**: Sortable table with filters, JSON/CSV export
- **Bayesian**: Signal bucket win rates with confidence adjustments
- **Logs**: Live-updating log viewer
- **Settings**: Full configuration editor with +/- steppers, toggles, and save

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   api.py (Flask)                         │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐   │
│  │ BinanceWS │  │Chainlink │  │    OrderBookWS        │   │
│  │ (WebSocket)│  │WS (WebSocket)│  │    (WebSocket)       │   │
│  └──────────┘  └──────────┘  └──────────────────────┘   │
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │           Strategy Engine (1s loop)               │    │
│  │  evaluate_signal_stack() → guards → execute_trade()│   │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
│  ┌────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ RiskManager │  │ Bayesian DB  │  │  Config Cache   │  │
│  └────────────┘  └──────────────┘  └────────────────┘  │
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │          Flask API Routes (port 3000)             │    │
│  │  /status /config /logs /trades /start /stop etc. │    │
│  └──────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
                            │
                            ▼
              ┌─────────────────────────┐
              │  Dashboard (Vite +      │
              │  Tailwind v4)           │
              │  Served via Flask at /  │
              └─────────────────────────┘
```

## Prerequisites

- **Python 3.10+** (tested on 3.14)
- **Node.js 18+** (for dashboard build)
- A Polymarket account with API credentials
- Polygon MATIC for gas fees

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/yourusername/polybot.git
cd polybot

# 2. Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install dashboard dependencies and build
cd dashboard
npm install
npm run build
cd ..

# 5. Configure your .env file
cp .env.example .env
# Edit .env with your Polymarket credentials (see Configuration section below)
```

## Configuration

### Environment Variables (`.env`)

| Variable | Required | Description |
|---|---|---|---|
| `POLY_PRIVATE_KEY` | Yes | Your Polymarket wallet private key (with 0x prefix) |
| `POLY_WALLET_ADDRESS` | Yes | Your Polymarket wallet address |
| `RELAYER_API_KEY_ADDRESS` | Yes | API key address from Polymarket |
| `POLY_API_KEY` | Yes | API key from Polymarket |
| `POLY_API_SECRET` | No | API secret (auto-generated from private key if blank) |
| `POLY_API_PASSPHRASE` | No | API passphrase (auto-generated from private key if blank) |
| `POLYGON_RPC` | Yes | Polygon RPC endpoint |
| `POLY_SIGNATURE_TYPE` | Yes | Signature type: `1` = EOA (legacy wallets), `3` = POLY_1271 (smart wallets) |
| `POLY_DEPOSIT_WALLET_ADDRESS` | No | Deposit wallet address (only for POLY_1271; defaults to POLY_WALLET_ADDRESS) |
| `POLY_RTDS_WS_URL` | No | RTDS WebSocket URL (defaults to Polymarket's official endpoint) |
| `POLYBOT_API_KEY` | No | API key for dashboard authentication (leave blank for no auth) |
| `POLYBOT_LOG_LEVEL` | No | Logging level: `DEBUG`, `INFO`, `WARN`, `ERROR` (default: `INFO`) |
| `FLASK_HOST` | No | API bind address (default: `0.0.0.0`) |
| `FLASK_PORT` | No | API port (default: `3000`) |

### Bot Settings (`config.json`)

All settings are editable through the dashboard Settings tab or directly in `config.json`:

#### Trading Config
| Setting | Default | Description |
|---|---|---|
| `dry_run` | `false` | Enable simulation mode — no real funds used |
| `bet_size` | `1` | Fixed USDC amount per trade |
| `max_bet` | `3` | Maximum USDC allowed per single trade |
| `max_trades_per_hour` | `12` | Trade frequency cap |
| `min_confidence` | `60` | Minimum signal confidence % to execute |

#### Risk Limits
| Setting | Default | Description |
|---|---|---|
| `daily_loss_limit_usdc` | `6` | Max cumulative daily loss |
| `hourly_loss_pct` | `5` | Max loss % per hour |
| `max_consecutive_losses` | `4` | Halt after this many losses |
| `max_drawdown_pct` | `10` | Max drawdown from peak |
| `cooldown_after_loss_seconds` | `300` | Pause after a loss (seconds) |

#### Bet Sizing
| Setting | Default | Description |
|---|---|---|
| `base_bet_pct` | `1` | % of bankroll per standard trade |
| `high_conf_bet_pct` | `2` | % for high-confidence trades |
| `arb_bet_pct` | `5` | % for arbitrage trades |
| `use_kelly_sizing` | `true` | Enable Kelly Criterion sizing |
| `kelly_fraction` | `0.3` | Fractional Kelly multiplier |

#### Signals
Each of the 14 signal modules can be toggled on/off under `modules` in `config.json`.

#### Guards
Each of the 9 safety guards can be toggled on/off under `guards` in `config.json`.

### Strategy Tuning
Fine-grained thresholds for each strategy module are available under `strategy_tuning` in `config.json`, covering wall detection, momentum, latency arb, window delta, arbitrage, Bayesian confidence, resolution hunt, phases, and miscellaneous parameters.

## Running the Bot

### Quick Start (macOS / Linux)

```bash
./start_mac.sh
```

This will:
1. Activate the virtual environment
2. Install/verify Python dependencies
3. Build the Vite dashboard
4. Source `.env` credentials
5. Start the bot on port 3000

### Manual Start

```bash
source venv/bin/activate
source .env
python api.py
```

### Dashboard

Open **http://localhost:3000** in your browser.

The dashboard auto-refreshes every 2 seconds and provides:
- Real-time BTC price and market data
- Countdown clock with phase indicator
- Signal confidence bar with strategy metadata
- Performance stats (win rate, P&L, bankroll)
- Risk management status
- Trade history with filters and export
- Bayesian signal analysis
- Live log viewer
- Full configuration editor with toggles and +/- steppers

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Dashboard (Vite-built HTML) |
| `/status` | GET | Current engine status, prices, signals |
| `/stats` | GET | Performance statistics |
| `/health` | GET | Engine health check |
| `/risk` | GET | Risk manager state |
| `/config` | GET/POST | Read/update configuration |
| `/logs` | GET | Recent log lines |
| `/trades` | GET | Trade history |
| `/db-trades` | GET | Trades from SQLite DB |
| `/bayesian` | GET | Bayesian signal bucket data |
| `/export-trades` | GET | Export trades (JSON/CSV) |
| `/start` | POST | Start the engine |
| `/stop` | POST | Stop the engine |
| `/restart` | POST | Restart the engine |
| `/reset-risk` | POST | Reset risk blocks |
| `/hard-reset` | POST | Factory reset (clears trades, risk state, DB) |
| `/clear-trades` | POST | Clear trade history |
| `/clear-logs` | POST | Clear log file |
| `/assets/*` | GET | Static dashboard assets |

## How the Strategy Engine Works

1. **Data Collection** — WebSocket feeds stream Binance BTC price, Chainlink oracle price, and Polymarket order book data in real-time.

2. **Market Window** — Each 5-minute Polymarket window is evaluated independently. The bot tracks which windows have been traded to avoid double entries.

3. **Signal Evaluation** — Every second, `evaluate_signal_stack()` runs all enabled signal modules, producing a prioritized list of trade opportunities. Signals are ordered by priority (arb > delta override > vol edge > wall bias > etc.).

4. **Guard Chain** — Each candidate signal passes through the enabled guard chain. Any guard can block the trade (momentum consistency, volatility, volume, trend bias, consecutive loss, stale feed, signal cooldown).

5. **Risk Check** — The RiskManager validates the trade against daily loss limits, hourly loss caps, drawdown, and other circuit breakers.

6. **Bayesian Adjustment** — If enough historical data exists for the signal bucket, confidence is boosted or penalized based on past win rates.

7. **Execution** — If all checks pass, the trade is placed via the Polymarket CLOB API. Trade outcomes are tracked by polling the oracle at market resolution.

## Signal Priority Stack

When multiple signals fire simultaneously, the engine uses this priority order:

1. **ARB** (highest) — Pure arbitrage opportunities
2. **DELTA_OVERRIDE** — Extreme price delta override
3. **RESOLUTION_HUNT** — Last-second resolution plays
4. **VOL_EDGE** — Volatility-based edges
5. **WALL_BIAS** — Order book wall signals
6. **WALL_MOMENTUM** — Wall + momentum combo
7. **ORACLE_LAG** — Oracle lag discrepancies
8. **MOMENTUM_ONLY** — Pure momentum
9. **MEAN_REVERSION** — Mean reversion bets
10. **LATENCY_ARB** — Latency-based arb
11. **WINDOW_DELTA** — Window delta signals
12. **CHEAP_SIDE** — Cheap side reversal
13. **HARD_DEADLINE** — Last-resort deadline fill
14. **SIGNAL** (lowest, contextual) — Generic signal

## Phases

The 5-minute market window is divided into 4 phases, each with different trading behavior:

| Phase | Time Remaining | Characteristics |
|---|---|---|
| 1 | 250–300s | Early entry, wider confidence bands |
| 2 | 100–249s | Mid-range, standard signal evaluation |
| 3 | 30–99s | Late entry, more aggressive |
| 4 | 5–29s | Final seconds, urgency |
| Deadline | 0–5s | Hard deadline — forced entry of best signal |

## Project Structure

```
polybot/
├── api.py               # Main engine + Flask API
├── risk_manager.py      # Risk management module
├── config.json          # Bot configuration
├── requirements.txt     # Python dependencies
├── start_mac.sh         # macOS/Linux startup script
├── .env                 # API credentials (not committed)
├── .gitignore
├── README.md
├── CHANGELOG.md
├── dashboard/
│   ├── index.html       # Dashboard HTML (Vite entry)
│   ├── vite.config.js   # Vite + Tailwind config
│   ├── package.json     # Node.js dependencies
│   └── src/
│       ├── main.js      # Tab switching, theme toggle, steppers
│       ├── dashboard.js # Render, data fetching, controls
│       ├── api.js       # API client layer
│       └── style.css    # Tailwind v4 + custom styles
└── logs/                # Runtime logs (auto-created)
```

## Troubleshooting

**"ModuleNotFoundError: No module named 'flask'"**
```bash
source venv/bin/activate
pip install -r requirements.txt
```

**Dashboard shows "Offline"**
- Ensure the bot is running (`python api.py`)
- Check port 3000 is not blocked
- Verify `.env` has valid credentials

**"Web3 v7 compatibility error"**
`py-clob-client-v2` requires `web3<7.0.0`. Pin the version in `requirements.txt`.

**Bot won't trade**
- Check dashboard for blocking reasons (risk limits, stale feed, etc.)
- Verify `dry_run: false` in config if you want live trading
- Ensure wallet has sufficient MATIC for gas

## License

MIT

## Disclaimer

This bot is provided for educational purposes. Cryptocurrency trading carries significant financial risk. Test thoroughly in dry-run mode before using with real funds. The authors are not responsible for any financial losses.
