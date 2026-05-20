#!/bin/bash
# POLYBOT Mac Startup Script
# Auto-launches the bot with monitoring + self-improvement

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_DIR="$SCRIPT_DIR/vps"
LOG_DIR="$SCRIPT_DIR/logs"
DATA_DIR="$SCRIPT_DIR/data"

# Create directories
mkdir -p "$LOG_DIR"
mkdir -p "$DATA_DIR"

# Copy config and .env to vps folder if not already there
if [ ! -f "$BOT_DIR/config.json" ]; then
    cp "$SCRIPT_DIR/config.json" "$BOT_DIR/config.json"
    echo "✅ Copied config.json to vps/"
fi

if [ ! -f "$BOT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env" "$BOT_DIR/.env"
    echo "✅ Copied .env to vps/"
fi

# Check for .env
if [ ! -f "$BOT_DIR/.env" ]; then
    echo "❌ .env file not found. Please create it with your API credentials."
    exit 1
fi

# Activate virtual environment
if [ -d "$SCRIPT_DIR/venv" ]; then
    echo "⚡ Activating virtual environment..."
    source "$SCRIPT_DIR/venv/bin/activate"
else
    echo "⚠️  No virtual environment found at $SCRIPT_DIR/venv."
    echo "🔧 Creating a new one..."
    python3 -m venv "$SCRIPT_DIR/venv"
    source "$SCRIPT_DIR/venv/bin/activate"
fi

# Install dependencies if needed
echo "🔧 Checking and installing dependencies inside virtual environment..."
pip install -q -r "$SCRIPT_DIR/requirements.txt"

# Set max open files for macOS
ulimit -n 10240 2>/dev/null || true

# Export env vars
set -a
source "$BOT_DIR/.env"
set +a

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║          POLYBOT - BTC Trading Bot              ║"
echo "║          Multi-Strategy Engine v9               ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "📊 Market: BTC/5min"
echo "💰 Trade Size: $1.00"
echo "🎯 Strategy: 8-signal multi-strategy"
echo "📈 Dashboard: http://localhost:3000"
echo ""
echo "Starting bot..."
echo ""

# Run the API server
cd "$BOT_DIR"
exec python api.py 2>&1 | tee "$LOG_DIR/polybot.log"
