# PolyBot v3.1 — Automated Polymarket Trading Bot

> Multi-signal trading engine for Polymarket BTC 5-minute markets with automatic position redemption.

## 📋 Features

- **Multi-Signal Strategy** — RSI, EMA, VWAP, and momentum voting system
- **Early Entry Optimization** — Trades in first 90s of each 5-min window for fair pricing
- **Auto-Redeem** — Automatically claims winnings every 10 minutes via Polymarket relayer
- **Live & Dry Run Modes** — Test strategies risk-free before going live
- **Real-Time Dashboard** — Web UI with trade history, P&L tracking, and analytics
- **Cloudflare Tunnel** — Secure remote access without port forwarding

## 📁 Project Structure

```
POLYBOT/
├── vps/                    # Production server files
│   ├── api.py             # Main bot engine + Flask API (deploy this)
│   ├── bot.py             # Legacy bot (not used, kept for reference)
│   └── setup.sh           # VPS deployment script
├── config/                 # Runtime configuration (created on first run)
│   ├── config.json        # Bot strategy settings
│   ├── trades.json        # Trade history
│   └── bot.log            # Bot activity logs
├── docs/                   # Documentation
│   └── DEPLOYMENT.md      # VPS deployment guide
├── scripts/                # Utility scripts
│   └── activate_bot.py    # API credential generator
├── index.html             # Web dashboard UI
├── start_local.ps1        # Local development launcher
├── requirements.txt       # Python dependencies
├── .env.example           # Environment template
└── .gitignore             # Git ignore rules
```

## 🚀 Quick Start (Local Testing)

### Prerequisites
- Python 3.10+ installed
- Polymarket account with wallet access
- At least 10 USDC in your Polymarket wallet (for live trading)

### 1. Install Dependencies

```powershell
pip install -r requirements.txt
```

### 2. Configure Environment

```powershell
# Copy the template
copy .env.example .env

# Edit .env with your credentials
notepad .env
```

Required fields in `.env`:
```env
POLY_PRIVATE_KEY=your_private_key_here
POLY_WALLET_ADDRESS=your_wallet_address_here
```

### 3. Generate API Credentials (One-Time Setup)

```powershell
python scripts/activate_bot.py
```

This will output `POLY_API_KEY`, `POLY_API_SECRET`, and `POLY_API_PASSPHRASE`. Add them to your `.env` file.

### 4. Start the Bot

```powershell
.\start_local.ps1
```

This will:
1. Copy files to `~/polybot/`
2. Install dependencies
3. Start the bot with Cloudflare tunnel
4. Display your dashboard URL

### 5. Access Dashboard

Open the Cloudflare tunnel URL shown in the terminal (e.g., `https://xxxx.trycloudflare.com`)

## ⚙️ Configuration

### Strategy Settings (`config.json`)

```json
{
  "dry_run": true,              // true = simulation, false = real trades
  "bet_size": 1.0,              // USDC per trade
  "max_trades_per_hour": 4,     // Maximum trades per hour
  "cooldown_seconds": 180,      // Minimum seconds between trades
  "min_confidence": 80,         // Minimum signal confidence (%)
  "max_consecutive_losses": 5   // Auto-stop after N losses
}
```

### Recommended Settings for Testing

| Setting | Value | Reason |
|---------|-------|--------|
| `dry_run` | `true` | Test without real money |
| `bet_size` | `1.0` | Lower risk during testing |
| `min_confidence` | `80` | Only high-conviction trades |
| `max_trades_per_hour` | `4` | Prevent overtrading |
| `cooldown_seconds` | `180` | Wait 3 min between trades |

## 📊 Dashboard Features

- **Real-Time Stats** — Balance, P&L, Win Rate, Total Trades
- **Trade Analysis** — Best/Worst trade, Avg Win/Loss, Win/Loss streaks
- **Performance Chart** — Visual win/loss distribution
- **Trade History** — Click any trade to expand full details
- **Settings Panel** — Adjust strategy, trigger manual redemption

## 🔄 Auto-Redeem

The bot automatically claims winning positions every 10 minutes via the Polymarket relayer (gas fees covered by relayer).

**Manual trigger:** Settings → "CLAIM ALL WINNINGS" button

**Requirements for auto-redeem:**
- `POLY_BUILDER_API_KEY`, `POLY_BUILDER_SECRET`, `POLY_BUILDER_PASSPHRASE` in `.env`
- Obtain these from Polymarket → Settings → API Keys

## 📈 Strategy Explained

The bot uses a 4-signal voting system:

1. **Trend** — Price vs 50-period SMA
2. **RSI Momentum** — Overbought/oversold detection
3. **EMA Crossover** — Fast vs slow exponential moving average
4. **VWAP** — Volume-weighted average price comparison

**Entry Window:** First 90 seconds of each 5-minute market cycle (ensures fair ~0.50 pricing)

**Price Filter:** Only trades when both UP/DOWN prices are between 0.35-0.65 (skips skewed markets)

## 🖥️ VPS Deployment

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for complete Oracle Cloud VPS setup guide.

Quick deploy:
```bash
# Upload to VPS
scp vps/api.py vps/setup.sh user@your-vps:~/polybot/

# Run setup
ssh user@your-vps "cd ~/polybot && bash setup.sh"
```

## 📝 Logs & Monitoring

- **Bot logs:** `~/polybot/bot.log`
- **API status:** `http://127.0.0.1:3000/status`
- **Manual redeem:** `POST http://127.0.0.1:3000/redeem`

## ⚠️ Risk Disclaimer

- Polymarket trading involves real financial risk
- Past performance does not guarantee future results
- Never trade more than you can afford to lose
- Always test in `dry_run: true` mode first
- This bot is provided AS-IS without warranties

## 🛠️ Troubleshooting

| Issue | Solution |
|-------|----------|
| `not enough balance` | Add USDC to wallet or reduce `bet_size` |
| `Relayer client not available` | Add Builder API credentials to `.env` |
| Bot won't start | Check `.env` has valid `POLY_PRIVATE_KEY` |
| Dashboard not loading | Verify Cloudflare tunnel is running |
| Trades failing | Ensure `min_confidence` is not too low (try 80+) |

## 📄 License

Private project — All rights reserved.
