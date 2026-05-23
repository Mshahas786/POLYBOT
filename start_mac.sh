#!/bin/bash
# POLYBOT Mac Startup Script

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"

mkdir -p "$LOG_DIR"

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo ".env file not found. Please create it with your API credentials."
    exit 1
fi

if [ -d "$SCRIPT_DIR/venv" ]; then
    echo "Activating virtual environment..."
    source "$SCRIPT_DIR/venv/bin/activate"
else
    echo "No virtual environment found. Creating one..."
    python3 -m venv "$SCRIPT_DIR/venv"
    source "$SCRIPT_DIR/venv/bin/activate"
fi

echo "Installing dependencies..."
pip install -q -r "$SCRIPT_DIR/requirements.txt"

echo "Building dashboard (Vite + Tailwind)..."
cd "$SCRIPT_DIR/dashboard"
npm install --silent 2>/dev/null
npm run build --silent 2>/dev/null
cd "$SCRIPT_DIR"

ulimit -n 10240 2>/dev/null || true

set -a
source "$SCRIPT_DIR/.env"
set +a

echo ""
echo "=========================================="
echo "  POLYBOT - BTC Trading Bot"
echo "  Multi-Strategy Engine v6.0"
echo "=========================================="
echo ""
echo "Dashboard: http://localhost:3000"
TRADING_MODE=$(python3 -c "import json; cfg=json.load(open('$SCRIPT_DIR/config.json')); print('DRY RUN (Simulation)' if cfg.get('dry_run') else 'LIVE TRADING')" 2>/dev/null || echo "DRY RUN")
echo "Mode: $TRADING_MODE"
echo ""
echo "Starting bot... (Ctrl+C to stop)"
echo ""

cd "$SCRIPT_DIR"
exec python api.py 2>&1 | tee "$LOG_DIR/polybot.log"
