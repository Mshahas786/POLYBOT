$ErrorActionPreference = "Stop"

$BotDir = "$env:USERPROFILE\polybot"
if (-Not (Test-Path $BotDir)) {
    New-Item -ItemType Directory -Path $BotDir | Out-Null
}

Write-Host "Copying python files locally..."
Copy-Item "vps\api.py" "$BotDir\api.py" -Force
Copy-Item "vps\bot.py" "$BotDir\bot.py" -Force

# Initialize default config if missing
if (-Not (Test-Path "$BotDir\config.json")) {
    '{"dry_run": true, "bet_size": 2.0}' | Out-File -FilePath "$BotDir\config.json" -Encoding utf8
}

Write-Host "Installing Python Dependencies..."
python -m pip install flask flask-cors requests

# Download cloudflared for windows
$CloudflaredExe = "$BotDir\cloudflared.exe"
if (-Not (Test-Path $CloudflaredExe)) {
    Write-Host "Downloading Cloudflared..."
    Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $CloudflaredExe
}

Set-Location $BotDir

Write-Host "Stopping any previous local instances..."
if (Test-Path "bot.pid") {
    $pidToKill = Get-Content "bot.pid"
    Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
}

# Kill anything on port 3000 (the API)
$port3000 = Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue
if ($port3000) {
    Stop-Process -Id $port3000.OwningProcess -Force -ErrorAction SilentlyContinue
}

# Kill old local cloudflared tunnels
Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'cloudflared' -and $_.ExecutablePath -match 'polybot' } | Invoke-CimMethod -MethodName Terminate | Out-Null

Write-Host "Starting API & BOT locally in background..."
Start-Process -FilePath "python" -ArgumentList "api.py" -WindowStyle Hidden -RedirectStandardOutput "api.log" -RedirectStandardError "api.err"
Start-Process -FilePath "python" -ArgumentList "bot.py" -WindowStyle Hidden -RedirectStandardOutput "bot.log" -RedirectStandardError "bot.err"

Write-Host "Starting Cloudflare Quick Tunnel locally..."
if (Test-Path "cloudflared.log") { Remove-Item "cloudflared.log" -Force }
Start-Process -FilePath $CloudflaredExe -ArgumentList "tunnel --url http://127.0.0.1:3000" -WindowStyle Hidden -RedirectStandardError "cloudflared.log" -RedirectStandardOutput "cloudflared.out"

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
