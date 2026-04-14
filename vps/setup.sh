#!/bin/bash
set -e  # Exit on any error

echo "========================================="
echo "  PolyBot VPS Setup Script"
echo "========================================="

echo "[1/6] Stopping existing processes..."
pkill -f python3 || true
pkill -f cloudflared || true

echo "[2/6] Creating bot directory and copying config..."
mkdir -p ~/polybot
cd ~/polybot

# Copy config.json if it doesn't exist yet
if [ ! -f config.json ]; then
    echo "  Creating default config.json..."
    cat > config.json << 'CONFIGEOF'
{
  "dry_run": true,
  "strategy": "directional",
  "bet_size": 2,
  "spread_bps": 50,
  "max_trades_per_hour": 8,
  "min_confidence": 55,
  "strategy_version": "5.0"
}
CONFIGEOF
    echo "  ✓ config.json created"
else
    echo "  ✓ config.json already exists"
fi

echo "[3/6] Checking cloudflared..."
if ! command -v cloudflared &> /dev/null; then
    echo "  Downloading cloudflared..."
    wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O cloudflared
    chmod +x cloudflared
    sudo mv cloudflared /usr/local/bin/cloudflared
    echo "  ✓ cloudflared installed"
else
    echo "  ✓ cloudflared already installed"
fi

echo "[4/6] Checking Python dependencies..."
# Use requirements.txt if available, otherwise install core packages
if [ -f requirements.txt ]; then
    echo "  Installing from requirements.txt..."
    pip install --quiet --upgrade -r requirements.txt 2>/dev/null || {
        echo "  ⚠️ Some packages failed to install, continuing..."
    }
else
    echo "  Installing core packages..."
    pip install --quiet --upgrade flask flask-cors requests websocket-client python-dotenv py-clob-client eth-abi web3 2>/dev/null || {
        echo "  ⚠️ pip install had warnings, continuing..."
    }
fi

echo "[5/6] Starting services..."
cd ~/polybot

# Start ONLY api.py (it includes the bot loop internally)
# bot.py is a legacy file and should NOT be started separately
echo "  Starting PolyBot API (includes bot loop)..."
nohup python3 api.py > api_out.log 2>&1 &
API_PID=$!
echo "  ✓ API started (PID: $API_PID)"

sleep 2

# Verify API is running
if kill -0 $API_PID 2>/dev/null; then
    echo "  ✓ API is running successfully"
else
    echo "  ❌ API failed to start! Check api_out.log for errors:"
    tail -20 api_out.log
    exit 1
fi

echo ""
echo "Starting Cloudflare Tunnel..."
rm -f cloudflared.log
nohup cloudflared tunnel --url http://127.0.0.1:3000 > cloudflared.log 2>&1 &

echo "Waiting for tunnel URL (8 seconds)..."
sleep 8

echo ""
echo "========================================="
echo "[6/6] Setup Complete!"
echo "========================================="

if [ -f cloudflared.log ]; then
    TUNNEL_URL=$(grep -Eo 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' cloudflared.log | head -1)
    if [ -n "$TUNNEL_URL" ]; then
        echo "✓ Tunnel URL: $TUNNEL_URL"
    else
        echo "⚠️ Tunnel URL not yet available, check cloudflared.log"
    fi
else
    echo "⚠️ cloudflared.log not found"
fi

echo ""
echo "Useful commands:"
echo "  View API logs: tail -f ~/polybot/api_out.log"
echo "  View bot logs: tail -f ~/polybot/bot.log"
echo "  View tunnel:   tail -f ~/polybot/cloudflared.log"
echo "  Stop bot:      pkill -f 'python3 api.py'"
echo "========================================="
