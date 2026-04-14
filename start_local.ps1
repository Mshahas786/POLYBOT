# ============================================================
# PolyBot Local Development Launcher
# ============================================================
# Copies production files to ~/polybot/ and starts the bot
# with Cloudflare tunnel.
#
# Usage: .\start_local.ps1
# ============================================================

$ErrorActionPreference = "Stop"

# -- Configuration ------------------------------------------
$BotDir = "$env:USERPROFILE\polybot"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

# -- Setup Directory ----------------------------------------
if (-Not (Test-Path $BotDir)) {
    New-Item -ItemType Directory -Path $BotDir | Out-Null
}

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "       PolyBot Local Launcher v3.1          " -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# -- Copy Production Files ----------------------------------
Write-Host "[1/5] Copying production files..." -ForegroundColor Yellow
Copy-Item "$ProjectRoot\vps\api.py" "$BotDir\api.py" -Force
Copy-Item "$ProjectRoot\index.html" "$BotDir\index.html" -Force

if (Test-Path "$ProjectRoot\.env") {
    Copy-Item "$ProjectRoot\.env" "$BotDir\.env" -Force
    Write-Host "  [OK] Environment file copied" -ForegroundColor Green
}
else {
    Write-Host "  [ERROR] No .env file found!" -ForegroundColor Red
    Write-Host "  Run: copy .env.example .env" -ForegroundColor Gray
    exit 1
}

if (Test-Path "$ProjectRoot\config\config.json") {
    Copy-Item "$ProjectRoot\config\config.json" "$BotDir\config.json" -Force
}

# -- Check Dependencies -------------------------------------
Write-Host "[2/5] Checking Python dependencies..." -ForegroundColor Yellow

try {
    python -m pip install -r "$ProjectRoot\requirements.txt" --quiet --disable-pip-version-check
    Write-Host "  [OK] Dependencies satisfied" -ForegroundColor Green
}
catch {
    Write-Host "  [WARN] Some packages may have failed to install" -ForegroundColor Yellow
}

# -- Download Cloudflared -----------------------------------
Write-Host "[3/5] Checking Cloudflare tunnel..." -ForegroundColor Yellow
$CloudflaredExe = "$BotDir\cloudflared.exe"

if (-Not (Test-Path $CloudflaredExe)) {
    Write-Host "  Downloading cloudflared..." -ForegroundColor Gray
    Invoke-WebRequest `
        -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
        -OutFile $CloudflaredExe `
        -UseBasicParsing
    Write-Host "  [OK] Cloudflared downloaded" -ForegroundColor Green
}
else {
    Write-Host "  [OK] Cloudflared already installed" -ForegroundColor Green
}

# -- Stop Previous Instances --------------------------------
Write-Host "[4/5] Stopping previous instances..." -ForegroundColor Yellow

Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match "api.py" -or $_.CommandLine -match "cloudflared"
} | ForEach-Object {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
}

# Kill any process on port 3000
$port3000 = Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue
if ($port3000) {
    Stop-Process -Id $port3000.OwningProcess -Force -ErrorAction SilentlyContinue
}

Write-Host "  [OK] Previous instances stopped" -ForegroundColor Green

# -- Start Bot & Tunnel -------------------------------------
Write-Host "[5/5] Starting PolyBot..." -ForegroundColor Yellow

$env:PYTHONUTF8 = 1

# Start bot in new visible window
$BotCmd = "cd $BotDir; `$env:PYTHONUTF8=1; python api.py"
Start-Process -FilePath "powershell" `
    -ArgumentList "-NoExit", "-Command", $BotCmd `
    -WindowStyle Normal

# Start Cloudflare tunnel
if (Test-Path "$BotDir\cloudflared.log") { Remove-Item "$BotDir\cloudflared.log" -Force }

Start-Process -FilePath $CloudflaredExe `
    -ArgumentList "tunnel --url http://127.0.0.1:3000" `
    -WindowStyle Hidden `
    -RedirectStandardError "$BotDir\cloudflared.log" `
    -RedirectStandardOutput "$BotDir\cloudflared.out"

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "       PolyBot Started Successfully         " -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  - Bot window: Check the new PowerShell window for logs" -ForegroundColor White
Write-Host "  - Dashboard:  Waiting for tunnel URL..." -ForegroundColor White
Write-Host ""

Write-Host "Waiting 12 seconds for Cloudflare tunnel..." -ForegroundColor Gray
Start-Sleep -Seconds 12

# Extract tunnel URL
if (Test-Path "$BotDir\cloudflared.log") {
    $TunnelLine = Select-String `
        -Path "$BotDir\cloudflared.log" `
        -Pattern 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' `
    | Select-Object -Last 1

    if ($TunnelLine) {
        $Url = $TunnelLine.Matches.Value
        Write-Host "============================================" -ForegroundColor Cyan
        Write-Host "        Your Dashboard URL                  " -ForegroundColor Cyan
        Write-Host "============================================" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "  $Url" -ForegroundColor Green
        Write-Host ""
        Write-Host "  -> Copy this URL and open in your browser" -ForegroundColor Gray
        Write-Host ""
    }
    else {
        Write-Host "  [WARN] Tunnel URL not ready yet" -ForegroundColor Yellow
        Write-Host "  Check: Get-Content $BotDir\cloudflared.log -Tail 20" -ForegroundColor Gray
    }
}
else {
    Write-Host "  [WARN] cloudflared.log not found" -ForegroundColor Yellow
}

Write-Host "Useful commands:" -ForegroundColor Cyan
Write-Host "  Stop bot:    Get-Process | Where Name -eq 'python' | Stop-Process -Force" -ForegroundColor Gray
Write-Host "  View logs:   Get-Content $BotDir\bot.log -Tail 50" -ForegroundColor Gray
Write-Host ""
