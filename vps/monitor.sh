#!/bin/bash
# PolyBot 24/7 Auto-Monitor & Self-Improvement Agent
# Monitors health, restarts on crash, auto-optimizes config daily
# Run this in the background or via cron: nohup bash monitor.sh &

cd "$(dirname "$0")"

PIDFILE="polybot.pid"
LOG="monitor.log"
HEALTH_URL="http://localhost:3000/health"
STATUS_URL="http://localhost:3000/status"
CONFIG="config.json"
IMPROVEMENTS="improvements.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

get_status() {
    curl -s "$STATUS_URL" 2>/dev/null
}

get_health() {
    curl -s "$HEALTH_URL" 2>/dev/null
}

restart_bot() {
    log "🔧 RESTARTING BOT..."
    
    # Kill existing process if any
    if [ -f "$PIDFILE" ]; then
        kill $(cat "$PIDFILE") 2>/dev/null
        rm "$PIDFILE"
    fi
    
    # Also kill any port 3000 processes
    lsof -ti:3000 | xargs kill 2>/dev/null
    
    sleep 2
    
    # Install deps silently
    pip3 install flask flask-cors websocket-client python-dotenv 2>/dev/null
    
    # Launch bot in background
    cd /Users/mshahas/Downloads/POLYBOT-main/vps
    nohup python3 api.py >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    log "🚀 Bot launched with PID $(cat "$PIDFILE")"
    
    # Wait for server to be ready
    sleep 5
    for i in {1..15}; do
        health=$(get_health)
        if echo "$health" | grep -q "healthy"; then
            log "✅ Server healthy!"
            return 0
        fi
        log "⏳ Waiting for server... ($i/15)"
        sleep 2
    done
    log "❌ Server failed to start within 30s"
    return 1
}

start_trading_loop() {
    log "🔄 Starting trading loop via API..."
    curl -s -X POST http://localhost:3000/start 2>/dev/null
    sleep 2
    check=$(get_status | grep -o '"running":true')
    if [ "$check" = '"running":true' ]; then
        log "✅ Trading loop started!"
    else
        log "⚠️ Trading loop may not have started"
    fi
}

analyze_performance() {
    log "📊 ANALYZING PERFORMANCE..."
    status=$(get_status)
    
    # Extract key metrics
    total=$(echo "$status" | grep -o '"total_trades":[0-9]*' | grep -o '[0-9]*')
    wins=$(echo "$status" | grep -o '"wins":[0-9]*' | grep -o '[0-9]*')
    losses=$(echo "$status" | grep -o '"losses":[0-9]*' | grep -o '[0-9]*')
    pnl=$(echo "$status" | grep -o '"total_pnl":-?[0-9.]*' | grep -o '-?[0-9.]*')
    winrate=$(echo "$status" | grep -o '"success_rate":[0-9.]*' | grep -o '[0-9.]*')
    conf=$(echo "$status" | grep -o '"min_confidence":[0-9]*' | grep -o '[0-9]*')
    
    if [ -z "$total" ] || [ "$total" = "0" ]; then
        log "No trades yet - skipping analysis"
        return
    fi
    
    log "Stats: Trades=$total, Wins=$wins, Losses=$losses, P&L=$pnl, WinRate=${winrate}%, MinConf=$conf"
    
    # Auto-optimize based on performance
    if [ -n "$winrate" ] && [ -n "$total" ]; then
        # Compare winrate to threshold (need 55%+ to be profitable)
        python3 -c "
import json, sys

total = int('$total')
wins = int('$wins')
losses = int('$losses')
pnl = float('$pnl')
winrate = float('$winrate')

# Read current config
with open('$CONFIG', 'r') as f:
    config = json.load(f)

changes = []

# Rule 1: Too many trades with low win rate → raise confidence threshold
if total >= 5 and winrate < 45:
    old = config.get('min_confidence', 50)
    config['min_confidence'] = min(old + 10, 85)
    changes.append(f'min_confidence: {old} → {config[\"min_confidence\"]}')

# Rule 2: High win rate but losing money → reduce bet size
if wins > losses and pnl < -5:
    old = config.get('bet_size', 1.0)
    config['bet_size'] = max(old * 0.7, 0.5)
    changes.append(f'bet_size: {old} → {config[\"bet_size\"]} (P&L negative)')

# Rule 3: Very high win rate → can increase bet size
if total >= 10 and winrate > 60 and pnl > 0:
    old = config.get('bet_size', 1.0)
    config['bet_size'] = min(old * 1.2, 5.0)
    changes.append(f'bet_size: {old} → {config[\"bet_size\"]} (profitable streak)')

# Rule 4: Too many losses → tighten risk management
if total >= 8 and losses > wins * 2:
    rm = config.get('risk_management', {})
    old_cooldown = rm.get('loss_cooldown', 120)
    rm['loss_cooldown'] = min(old_cooldown + 60, 600)
    rm['max_consecutive_losses'] = max(rm.get('max_consecutive_losses', 5) - 1, 2)
    config['risk_management'] = rm
    changes.append(f'loss_cooldown: {old_cooldown} → {rm[\"loss_cooldown\"]}, max_consec: tighter')

# Rule 5: Consecutive losses streak → emergency slowdown
if losses >= 3:
    rm = config.get('risk_management', {})
    rm['loss_cooldown'] = max(rm.get('loss_cooldown', 120), 300)
    rm['max_consecutive_losses'] = min(rm.get('max_consecutive_losses', 5), 3)
    config['risk_management'] = rm
    changes.append('Emergency: loss_cooldown=300s, max_consec=3')

# Save optimized config
with open('$CONFIG', 'w') as f:
    json.dump(config, f, indent=2)

if changes:
    for c in changes:
        print(f'  OPTIMIZED: {c}')
    print(f'  Changes applied: {len(changes)}')
else:
    print('  No changes needed - performance within bounds')
" 2>&1 | while read line; do
        log "$line"
        echo "$line" >> "$IMPROVEMENTS"
    done
fi
    
    log "📊 Analysis complete"
}

check_risk() {
    log "🛡️ CHECKING RISK METRICS..."
    status=$(get_status)
    
    risk_blocked=$(echo "$status" | grep -c '"circuit_breaker_tripped":true')
    consec=$(echo "$status" | grep -o '"consecutive_losses":[0-9]*' | grep -o '[0-9]*')
    drawdown=$(echo "$status" | grep -o '"drawdown_pct":[0-9.]*' | grep -o '[0-9.]*')
    
    if [ "$risk_blocked" = "1" ]; then
        log "🚨 CIRCUIT BREAKER TRIPPED! Auto-resetting..."
        curl -s -X POST http://localhost:3000/reset-risk
        log "✅ Risk reset - checking performance before resuming..."
        sleep 5
        analyze_performance
    fi
    
    if [ -n "$drawdown" ]; then
        python3 -c "
d = float('$drawdown')
if d > 10:
    print(f'  ⚠️ Drawdown at {d}% - approaching max (15%)')
elif d > 5:
    print(f'  ⚠️ Drawdown at {d}% - monitoring closely')
else:
    print(f'  ✅ Drawdown: {d}% - healthy')
" 2>&1 | while read line; do
            log "$line"
        done
    fi
}

daily_report() {
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "📋 DAILY PERFORMANCE REPORT"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    status=$(get_status)
    echo "$status" | python3 -c "
import sys, json
data = sys.stdin.read()
try:
    d = json.loads(data)
    print('┌─────────────────────────────────────┐')
    print(f'│ Status: {\"🟢 LIVE\" if d.get(\"running\") else \"🔴 OFF\"} │')
    print(f'│ BTC: \${d.get(\"btc_price\", 0):.2f} │')
    print(f'│ Uptime: {d.get(\"uptime\", \"N/A\")} │')
    print('├─────────────────────────────────────┤')
    print(f'│ Trades: {d.get(\"total_trades\", 0)} │')
    print(f'│ Wins: {d.get(\"wins\", 0)} ✓ │')
    print(f'│ Losses: {d.get(\"losses\", 0)} ✗ │')
    print(f'│ Win Rate: {d.get(\"success_rate\", 0)}% │')
    print(f'│ Total P&L: \${d.get(\"total_pnl\", 0):+.2f} │')
    print('├─────────────────────────────────────┤')
    if 'risk' in d:
        r = d['risk']
        print(f'│ Bankroll: \${r.get(\"current_bankroll\", 0):.2f} │')
        print(f'│ Peak: \${r.get(\"peak_bankroll\", 0):.2f} │')
        print(f'│ Drawdown: {r.get(\"drawdown_pct\", 0):.1f}% │')
        print(f'│ Consec Loss: {r.get(\"consecutive_losses\", 0)} │')
    if d.get('running'):
        info = d.get('info', {})
        print('├─────────────────────────────────────┤')
        print(f'│ Market: {info.get(\"slug\", \"N/A\")} │')
        print(f'│ Strike: \${info.get(\"price_to_beat\", 0):.2f} │')
        print(f'│ Status: {info.get(\"status\", \"N/A\")} │')
    print('└─────────────────────────────────────┘')
except json.JSONDecodeError:
    print('Could not parse status JSON')
" 2>&1
    
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    # Run analysis and optimization after report
    analyze_performance
    check_risk
}

# ── MAIN LOOP ──
log "🤖 PolyBot Monitor Agent STARTING"

# Initial setup
health=$(get_health)
if ! echo "$health" | grep -q "healthy"; then
    log "❌ Bot not healthy - restarting..."
    restart_bot
    sleep 3
    start_trading_loop
else
    log "✅ Bot already healthy"
    
    # Check if trading loop is running
    status=$(get_status)
    running=$(echo "$status" | grep -c '"running":true')
    if [ "$running" != "1" ]; then
        log "⚠️ Trading loop not active - starting..."
        start_trading_loop
    fi
fi

# Main monitoring loop - runs every 2 minutes
log "🔄 Starting main loop (check every 120s, report every hour)"

while true; do
    health=$(get_health)
    
    # Check if server is down
    if ! echo "$health" | grep -q "healthy"; then
        log "🚨 BOT CRASHED! Auto-restarting..."
        restart_bot
        sleep 3
        start_trading_loop
        sleep 60
        continue
    fi
    
    # Check if trading loop died
    status=$(get_status)
    running=$(echo "$status" | grep -c '"running":true')
    if [ "$running" != "1" ]; then
        log "⚠️ Trading loop stopped - restarting..."
        curl -s -X POST http://localhost:3000/restart 2>/dev/null
        sleep 5
    fi
    
    # Check risk state every check
    check_risk
    
    # Hourly performance report + optimization
    hour=$(date +%H)
    minute=$(date +%M)
    if [ "$minute" = "00" ]; then
        daily_report
    fi
    
    log "✅ All systems OK - next check in 2min"
    sleep 120
done
