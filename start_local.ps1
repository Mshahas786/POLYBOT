$ErrorActionPreference = "Stop"

$BotDir = "$env:USERPROFILE\polybot"
if (-Not (Test-Path $BotDir)) {
    New-Item -ItemType Directory -Path $BotDir | Out-Null
}

Write-Host "Copying configuration files locally..."
Copy-Item "vps\api.py" "$BotDir\api.py" -Force
Copy-Item "vps\bot.py" "$BotDir\bot.py" -Force
if (Test-Path ".env") {
    Copy-Item ".env" "$BotDir\.env" -Force
}
if (Test-Path "config.json") {
    Copy-Item "config.json" "$BotDir\config.json" -Force
}

# Initialize default config and env if missing in workspace
if (-Not (Test-Path "config.json")) {
    '{"dry_run": true, "bet_size": 2.0}' | Out-File -FilePath "config.json" -Encoding utf8
}
if (-Not (Test-Path ".env")) {
    $envContent = "# POLYMARKET WALLET`n" +
                  "POLY_PRIVATE_KEY=your_private_key_here`n" +
                  "POLY_WALLET_ADDRESS=your_wallet_address_here`n" +
                  "`n# POLYMARKET API CREDENTIALS`n" +
                  "POLY_API_KEY=your_api_key_here`n" +
                  "POLY_API_SECRET=your_api_secret_here`n" +
                  "POLY_API_PASSPHRASE=your_api_passphrase_here"
    $envContent | Out-File -FilePath ".env" -Encoding utf8
}

Write-Host "Installing Python Dependencies..."
python -m pip install flask flask-cors requests websocket-client

# Download cloudflared for windows
$CloudflaredExe = "$BotDir\cloudflared.exe"
if (-Not (Test-Path $CloudflaredExe)) {
    Write-Host "Downloading Cloudflared..."
    Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $CloudflaredExe
}

Set-Location $BotDir

Write-Host "Stopping any previous local instances..."
# Kill any python processes running bot.py or api.py in the polybot folder
Get-WmiObject Win32_Process | Where-Object { 
    $_.CommandLine -match "api.py" -or $_.CommandLine -match "bot.py" -or $_.CommandLine -match "cloudflared"
} | ForEach-Object { 
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} 
}

# Final check for port 3000
$port3000 = Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue
if ($port3000) {
    Stop-Process -Id $port3000.OwningProcess -Force -ErrorAction SilentlyContinue
}

$env:PYTHONUTF8 = 1

Write-Host "Starting PolyBot Unified Backend (VISIBLE WINDOW)..."
# Start the Unified API + Bot in a new visible window
Start-Process -FilePath "powershell" -ArgumentList "-NoExit", "-Command", "cd $BotDir; `$env:PYTHONUTF8=1; python api.py" -WindowStyle Normal

Write-Host "Starting Cloudflare Quick Tunnel..."
if (Test-Path "cloudflared.log") { Remove-Item "cloudflared.log" -Force }
Start-Process -FilePath $CloudflaredExe -ArgumentList "tunnel --url http://127.0.0.1:3000" -WindowStyle Hidden -RedirectStandardError "cloudflared.log" -RedirectStandardOutput "cloudflared.out"

Write-Host "----------------------------------------------------"
Write-Host "SUCCESS: PolyBot is now running locally."
Write-Host "1. Wait 10 seconds for the tunnel to go live."
Write-Host "2. Copy the green URL from 'cloudflared.log' and paste it into the Dashboard."
Write-Host "3. Keep the black 'PolyBot Backend' window open!"
Write-Host "----------------------------------------------------"

Write-Host "Waiting 12 seconds for tunnel to connect..."
Start-Sleep -Seconds 12

$TunnelLines = Select-String -Path "cloudflared.log" -Pattern 'https://[a-zA-Z0-9-]+\.trycloudflare\.com'
if ($TunnelLines) {
    # Extract just the url
    $Url = $TunnelLines[-1].Matches.Value
    Write-Host "`n---TUNNEL_URL_START---"
    Write-Host $Url -ForegroundColor Green
    Write-Host "---TUNNEL_URL_END---`n"
    Write-Host "SUCCESS! Copy the green URL above and paste it into the Github Pages Dashboard Settings (VPS URL field)!" -ForegroundColor Cyan
} else {
    Write-Host "`nFailed to extract Tunnel URL. Checking local logs:" -ForegroundColor Red
    Get-Content "cloudflared.log" -Tail 15
}
