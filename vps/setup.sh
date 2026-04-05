#!/bin/bash
echo "Stopping existing processes..."
pkill -f python3
pkill -f cloudflared

mkdir -p ~/polybot
cd ~/polybot

echo "Checking cloudflared..."
if ! command -v cloudflared &> /dev/null; then
    echo "Downloading cloudflared..."
    wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
    chmod +x cloudflared-linux-amd64
    sudo mv cloudflared-linux-amd64 /usr/local/bin/cloudflared
fi

echo "Starting API and BOT..."
nohup python3 api.py > api_out.log 2>&1 &
nohup python3 bot.py > bot_out.log 2>&1 &

echo "Starting Cloudflare Quick Tunnel..."
rm -f cloudflared.log
nohup cloudflared tunnel --url http://127.0.0.1:3000 > cloudflared.log 2>&1 &

# Wait for tunnel to generate the URL (takes a few seconds)
sleep 8

echo "---TUNNEL_URL_START---"
grep -Eo 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' cloudflared.log | head -1
echo "---TUNNEL_URL_END---"
