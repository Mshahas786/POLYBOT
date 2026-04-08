# VPS Deployment Guide — Oracle Cloud Free Tier

## 📋 Prerequisites

- Oracle Cloud Always-Free VPS (Ubuntu 22.04 ARM64)
- SSH access to your VPS
- Domain name (optional, for HTTPS)

## 🚀 Step-by-Step Deployment

### 1. Connect to VPS

```bash
ssh -i ~/.ssh/your_key ubuntu@YOUR_VPS_IP
```

### 2. Install System Dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv wget curl
```

### 3. Create Bot Directory

```bash
mkdir -p ~/polybot
cd ~/polybot
```

### 4. Upload Files

From your local machine:

```powershell
# Upload main files
scp vps/api.py vps/setup.sh ubuntu@YOUR_VPS_IP:~/polybot/

# Upload dashboard
scp index.html ubuntu@YOUR_VPS_IP:~/polybot/
```

### 5. Configure Environment

On the VPS:

```bash
cd ~/polybot
nano .env
```

Add your credentials:

```env
POLY_PRIVATE_KEY=your_private_key_here
POLY_WALLET_ADDRESS=your_wallet_address_here
POLY_SIGNATURE_TYPE=1
```

### 6. Run Setup Script

```bash
chmod +x setup.sh
./setup.sh
```

This will:
- Install Python dependencies
- Start the bot as a background process
- Launch Cloudflare tunnel
- Display your dashboard URL

### 7. Verify It's Running

```bash
# Check process
ps aux | grep api.py

# Check logs
tail -f ~/polybot/bot.log

# Check API
curl http://127.0.0.1:3000/status
```

### 8. Access Dashboard

The setup script outputs a Cloudflare URL like:
```
https://xxxx-xxxx-xxxx.trycloudflare.com
```

Open this in your browser.

## 🔧 Management Commands

### View Logs

```bash
# Live bot logs
tail -f ~/polybot/bot.log

# API output logs
tail -f ~/polybot/api_out.log
```

### Stop the Bot

```bash
pkill -f "python3 api.py"
```

### Restart the Bot

```bash
pkill -f "python3 api.py"
sleep 2
cd ~/polybot
nohup python3 api.py > api_out.log 2>&1 &
```

### Update the Bot

```bash
# Upload new api.py
exit  # back to local machine
scp vps/api.py ubuntu@YOUR_VPS_IP:~/polybot/api.py

# Restart on VPS
ssh ubuntu@YOUR_VPS_IP "pkill -f 'python3 api.py' && sleep 2 && cd ~/polybot && nohup python3 api.py > api_out.log 2>&1 &"
```

## 🔒 Security Best Practices

1. **Firewall Rules:** Only allow SSH (port 22) in Oracle Cloud security list
2. **SSH Keys:** Use key-based auth, disable password login
3. **Keep Updated:** Run `sudo apt update` monthly
4. **Monitor:** Check `htop` for resource usage

## 💡 Troubleshooting

### Bot Crashed / Not Running

```bash
# Check logs
cat ~/polybot/bot.log | tail -50

# Check Python errors
cat ~/polybot/api_out.log | tail -50
```

### Cloudflare Tunnel Down

```bash
# Restart tunnel
pkill -f cloudflared
cd ~/polybot
nohup cloudflared tunnel --url http://127.0.0.1:3000 > cloudflared.log 2>&1 &
sleep 8
grep -o 'https://.*\.trycloudflare\.com' cloudflared.log | head -1
```

### Out of Memory (ARM64 1GB)

```bash
# Check memory
free -h

# If needed, add swap
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

## 📊 Monitoring Setup (Optional)

### Systemd Service (Auto-Restart)

Create `/etc/systemd/system/polybot.service`:

```ini
[Unit]
Description=PolyBot Trading Engine
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/polybot
ExecStart=/usr/bin/python3 api.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable polybot
sudo systemctl start polybot
sudo systemctl status polybot
```

View logs:

```bash
journalctl -u polybot -f
```
