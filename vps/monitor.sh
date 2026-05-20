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
    
    # Activate virtual environment if exists
    if [ -d "../venv" ]; then
        log "⚡ Activating virtual environment..."
        source ../venv/bin/activate
        PYTHON_BIN="python"
        PIP_BIN="pip"
    else
        PYTHON_BIN="python3"
        PIP_BIN="pip3"
    fi
    
    # Install deps silently
    $PIP_BIN install -q flask flask-cors websocket-client python-dotenv requests 2>/dev/null
    
    # Launch bot in background
    nohup $PYTHON_BIN api.py >> "$LOG" 2>&1 &
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
    
    if [ -z "$status" ]; then
        log "⚠️ Could not retrieve status from API"
        return
    fi
    
    # Activate virtual environment if exists to run python
    if [ -d "../venv" ]; then
        source ../venv/bin/activate
        PYTHON_BIN="python"
    else
        PYTHON_BIN="python3"
    fi
    
    # We pass the status JSON via stdin to python and do all extraction and optimization in python directly!
    echo "$status" | $PYTHON_BIN -c "
import json, sys

try:
    status = json.load(sys.stdin)
except Exception as e:
    print(f'❌ Failed to parse status JSON: {e}')
    sys.exit(1)

total = status.get('total_trades', 0)
wins = status.get('wins', 0)
losses = status.get('losses', 0)
pnl = status.get('total_pnl', 0.0)
winrate = status.get('success_rate', 0.0)
conf = status.get('info', {}).get('confidence', 0.0)

# Check if there are trades
if total == 0:
    print('No trades yet - skipping analysis')
    sys.exit(0)

print(f'Stats: Trades={total}, Wins={wins}, Losses={losses}, P&L={pnl}, WinRate={winrate}%, MinConf={conf}')

# Read current config
try:
    with open('$CONFIG', 'r') as f:
        config = json.load(f)
except Exception as e:
    print(f'❌ Failed to read config: {e}')
    sys.exit(1)

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
try:
    with open('$CONFIG', 'w') as f:
        json.dump(config, f, indent=2)
except Exception as e:
    print(f'❌ Failed to write config: {e}')
    sys.exit(1)

if changes:
    for c in changes:
        print(f'  OPTIMIZED: {c}')
    print(f'  Changes applied: {len(changes)}')
else:
    print('  No changes needed - performance within bounds')
" 2>&1 | while read line; do
        log "$line"
        echo "$line" >> "$IMPROVEMEcheck_risk() {
    log "🛡️ CHECKING RISK METRICS..."
    status=$(get_status)
    
    if [ -z "$status" ]; then
        log "⚠️ Could not retrieve status from API"
        return
    fi
    
    if [ -d "../venv" ]; then
        source ../venv/bin/activate
        PYTHON_BIN="python"
    else
        PYTHON_BIN="python3"
    fi
    
    echo "$status" | $PYTHON_BIN -c "
import json, sys

try:
    status = json.load(sys.stdin)
except Exception as e:
    print(f'❌ Failed to parse status JSON: {e}')
    sys.exit(1)

risk = status.get('risk', {})
risk_blocked = risk.get('circuit_breaker_tripped', False)
drawdown = risk.get('drawdown_pct', 0.0)

if risk_blocked:
    print('TRIPPED')

print(f'DRAWDOWN:{drawdown}')
" 2>&1 | while read line; do
        if [ "$line" = "TRIPPED" ]; then
            log "🚨 CIRCUIT BREAKER TRIPPED! Auto-resetting..."
            curl -s -X POST http://localhost:3000/reset-risk
            log "✅ Risk reset - checking performance before resuming..."
            sleep 5
            analyze_performance
        elif [[ "$line" == DRAWDOWN:* ]]; then
            drawdown_val=${line#DRAWDOWN:}
            $PYTHON_BIN -c "
d = float('$drawdown_val')
if d > 10:
    print(f'  ⚠️ Drawdown at {d}% - approaching max (15%)')
elif d > 5:
    print(f'  ⚠️ Drawdown at {d}% - monitoring closely')
else:
    print(f'  ✅ Drawdown: {d}% - healthy')
" | while read outline; do log "$outline"; done
        fi
    done
}

daily_report() {
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "📋 DAILY PERFORMANCE REPORT"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    status=$(get_status)
    
    if [ -d "../venv" ]; then
        source ../venv/bin/activate
        PYTHON_BIN="python"
    else
        PYTHON_BIN="python3"
    fi
    
    echo "$status" | $PYTHON_BIN -c "
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
}�────────────────────────────────────┤')
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
