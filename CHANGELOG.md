# PolyBot Changelog

## Risk Config (`config.json`)

| Setting | Before | After | Why |
|---|---|---|---|
| `max_consecutive_losses` | 20 | 3 | Stop after 3 losses instead of 20 |
| `max_concurrent_positions` | 999 | removed | Unused — per-window guard already prevents double trades |
| `max_drawdown_pct` | 15% | 10% | Trip breaker sooner |
| `cooldown_after_loss_seconds` | 60 | 180 | Wait 3min after a loss |
| `toxic_lag_threshold` | 1.5% | 1.0% | Avoid more toxic flow |
| `min_confidence` | 70 | 75 | Higher conviction required |
| `max_trades_per_hour` | 10 | 6 | Fewer, higher-quality trades |
| `max_token_price` | 0.95 | 0.92 | Avoid overpriced tokens |
| `use_kelly_sizing` | false | true | Optimal position sizing |
| `kelly_fraction` | 0.25 | 0.3 | Slightly more aggressive Kelly |
| `base_bet_pct` | missing | 1.0% | % of bankroll per trade |
| `high_conf_bet_pct` | missing | 2.0% | Scale up on high confidence |

## Strategy (`api.py`)

### Bayesian Confidence Adjustment
- Queries historical win rate per `{phase}_{direction}_{signal_type}` bucket
- Boosts confidence +3 or +5 for proven signals (win rate > 55%)
- Cuts confidence -5 or -10 for losing signals (< 45%)
- Only activates after 3+ samples per bucket

### Volatility Guard
- `calc_volatility()` — measures BTC spread over last 60s
- Skips trades when 60s spread exceeds 0.5%
- Avoids unpredictable / choppy windows

### Trend Bias Filter
- `calc_trend_bias()` — compares current price vs 120s moving average
- Blocks UP trades in bearish trend (< -0.15%)
- Blocks DOWN trades in bullish trend (> +0.15%)

### Edge Block Tightened
- Changed from `0.47 < price < 0.53` → `0.48 < price < 0.52`
- Requires minimum 2% edge (was 3%)

## Risk Manager (`risk_manager.py`)

### New: `reset_all_blocks()`
Resets everything at once:
- circuit breaker
- consecutive losses counter
- win_rate_reduced flag
- position_count
- hourly_pnl
- last_loss_time / block_reason

### Removed
- `max_concurrent_positions` check in `can_trade()` — was dead code since `release_position()` was never called

## API (`api.py`)

### New Endpoints
| Endpoint | Method | Description |
|---|---|---|
| `/reset-risk` | POST | Now calls `reset_all_blocks()` (was only resetting circuit breaker) |
| `/hard-reset` | POST | Wipes risk state, trades.json, and Bayesian DB |

## Dashboard (`index.html`)

Complete professional overhaul with 5 tabs:

| Tab | Content |
|---|---|
| **Dashboard** | Price grid (BTC/Chainlink/Market), alert banners, market window with countdown/phase, signal card with confidence bar + full metadata, performance stats, indicators panel, risk management panel, module status |
| **Trades** | Sortable table (Time/Dir/Size/Token/Type/Conf/Outcome/P&L), 1H/24H/ALL filters, JSON/CSV export, summary stats bar |
| **Bayesian** | Signal bucket win rates with visual bars, sorted by sample count |
| **Logs** | Live-updating log viewer, color-coded levels, clear button |
| **Settings** | All config editable: dry run toggle, bet size, max trades, min confidence, loss limits, drawdown, cooldown, Kelly sizing + save |

### Key UI Features
- 2-second auto-refresh (was 10s)
- Alert banners for all blocking conditions (circuit breaker, consecutive losses, stale feed)
- Color-coded metrics throughout (green=good, red=bad)
- Bitcoin price delta %
- Countdown clock with phase indicator
- Reset Risk Blocks and Factory Reset buttons
