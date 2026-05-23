# Skills Research for PolyBot

## Bot Strategy Skills (from research)

### 1. Mean Reversion Strategy
**Source:** QuantPedia, PredictEngine, Polymarket Bot Arena
- BTC 5m prices oscillate in predictable ranges — buy when token price drops to oversold levels, sell at overbought
- **Z-score signal:** Buy when z-score < -2, sell when > +2 (price vs 20-period MA)
- **RSI signal:** Buy when RSI < 30, sell when RSI > 70 (Polyblock RSI strategy)
- **Research paper evidence:** Mean-reversion generates "substantial alpha" on Polymarket binary contracts under limit-order execution
- **Add as new signal type:** `MEAN_REVERSION` at priority ~5-6, trades against extreme momentum

### 2. Resolution Hunting
**Source:** PredictEngine
- Buy shares above $0.95 or below $0.05 when markets are near expiration and outcome is nearly certain
- Highest win rate (90%+) — small profit per trade, high volume
- **Add:** In last 30s of window, if token price > 0.92 or < 0.08, buy with very high confidence

### 3. Multi-Bot Ensemble
**Source:** Polymarket Bot Arena (github.com/ThinkEnigmatic/polymarket-bot-arena)
- Run 4 competing strategies (momentum, mean-reversion, sentiment, hybrid) and let them compete
- **Evolutionary learning:** Every 12h, bottom 2 bots are replaced with mutated winners
- Could run as separate threads with weighted voting

### 4. Whale Copy Trading
**Source:** Polycopy, BotEdge, PredictEngine
- Track top-performing Polymarket wallets and mirror their trades
- **BotEdge tracks 271 profitable wallets with 6 signal patterns:** flips, consensus, divergence, etc.
- **Add as new module:** Track known profitable wallets' positions and mirror with delay

### 5. Order Flow + Cumulative Delta
**Source:** EKX.ai, Amberdata
- Track cumulative delta (aggressive buys - aggressive sells) on Polymarket order book
- Divergence between delta and price = reversal signal
- **Add:** Track trade-by-trade data from CLOB WebSocket feed

### 6. Latency Optimization
**Source:** QuantVPS
- Co-locate server near Polymarket infra (lowest latency = 1-5ms vs 150ms+ home)
- Use async programming instead of sequential processing
- Cache frequently used market data
- HMAC-SHA256 signature pre-computation

### 7. Cross-Platform Arbitrage
**Source:** PredictEngine
- Compare Polymarket prices vs Kalshi, sportsbooks (DraftKings, Caesars)
- Buy on Polymarket when 3%+ cheaper than sportsbook implied probability
- Requires external data feeds (sportsbook odds APIs)

### 8. Bayesian Adaptive Learning (already partially implemented)
**Source:** polymarket-bot-arena, existing codebase
- Your bot already has Bayesian buckets — but the learning can extend further:
  - Track win rate by time-of-day (different strategies work at different hours)
  - Track win rate by BTC volatility regime (low/high vol)
  - Use Thompson sampling to select between strategies

## UI/UX Skills for Dashboard

### 1. Real-Time Price Chart (Sparkline)
- Add a mini BTC price sparkline chart using Canvas or SVG
- Show 5-minute window price action visually
- Displays momentum direction instantly without reading numbers

### 2. Visual Countdown Ring
- Circular progress ring around the countdown timer
- Color transitions: green → yellow → red as window closes
- Much more glanceable than a text timer

### 3. Signal Strength Gauge
- Confidence bar with color zones (red/yellow/green) and threshold markers
- Show the min_confidence line on the bar

### 4. Trade History Heatmap
- Color-coded grid showing wins/losses by hour of day × day of week
- Identifies your best and worst trading periods at a glance

### 5. P&L Equity Curve
- Simple SVG line chart showing bankroll over time
- Mark trades as green/red dots on the curve

### 6. Bayesian Win-Rate Matrix
- Heatmap grid: signal_type × phase, colored by win rate
- Shows exactly which signal/phase combos are profitable vs losing

### 7. Performance Summary Cards
- Big number cards at top: Win Rate, P&L Today, Best Strategy, Current Streak
- Mini sparklines beside each card showing trend

### 8. Mobile-Responsive Layout
- Collapsible card sections on mobile
- Touch-friendly button sizes
- Landscape mode optimization for quick glances

### 9. Dark/Light Mode Toggle
- CSS variables already support theming
- Add a simple toggle that swaps the :root colors

### 10. Alert Sound Notifications
- Play a short sound when a trade is placed or when risk blocks trigger
- Browser Notification API for background tab alerts

### 11. Trade Entry Animation
- When a new trade appears, briefly highlight the row (pulse animation)
- Makes new activity visible even when not actively watching

### 12. Config Export/Import
- Export all settings + Bayesian data as JSON
- Import on another instance — zero-config migration

### 13. Strategy A/B Test Panel
- Side-by-side comparison of two strategies running simultaneously
- Show P&L, win rate, Sharpe ratio for each

### 14. Dashboard Refresh Rate Control
- Slider to adjust polling interval (1s / 2s / 5s / 10s / 30s)
- Trade off: real-time accuracy vs. server load

### 15. Risk Block Timeline
- History of when each risk block triggered/cleared
- Shows the bot's risk state over time (circuit breaker, consecutive losses, etc.)

## Priority Recommendations

### High Impact / Low Effort
1. **Resolution hunting** — Add last-30s token price check for cheap shares (highest ROI, simplest code)
2. **Visual countdown ring** on dashboard — easy UI win
3. **Refresh rate slider** — let user control polling cadence
4. **Trade entry animation** — pulse on new rows

### High Impact / Medium Effort
5. **Mean reversion signal** — Add z-score + RSI as a new signal type at priority ~5
6. **P&L equity curve chart** — SVG line chart for bankroll trend
7. **Bayesian win-rate heatmap** — Grid showing signal_type × phase performance
8. **Performance summary cards** — Big number cards with sparklines

### High Impact / High Effort
9. **Multi-bot ensemble** — Run momentum + mean-reversion + hybrid bots in parallel
10. **Whale copy trading module** — Track and mirror profitable Polymarket wallets
11. **Cumulative delta / order flow** — Real-time trade-by-trade analysis
