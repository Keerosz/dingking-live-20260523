param(
    [int]$CheckIntervalSeconds = 20,
    [string]$ProjectRoot = "C:\Users\Spann\weather-warfare",
    [string]$PythonExe = "python",
    [bool]$UseNamedTunnel = $false
)

$ErrorActionPreference = "SilentlyContinue"

$cloudflaredConfig = Join-Path $ProjectRoot "cloudflared-config.yml"
$cloudflaredExe = "cloudflared"
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    $fallback = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
    if (Test-Path $fallback) {
        $cloudflaredExe = $fallback
    }
}

function Ensure-Backend {
    $backend = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "uvicorn app.main:app" }
    if (-not $backend) {
        Write-Host "[keeper] backend down -> starting uvicorn" -ForegroundColor Yellow
        Start-Process powershell -WindowStyle Minimized -ArgumentList '-NoProfile','-Command',"Set-Location $ProjectRoot; $PythonExe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
    }
}

function Ensure-Tunnel {
    $anyTunnelRunning = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "cloudflared" -and $_.CommandLine -match "tunnel" }
    if ($anyTunnelRunning) {
        return
    }

    if ($UseNamedTunnel -and (Test-Path $cloudflaredConfig) -and (Test-Path $cloudflaredExe)) {
        Write-Host "[keeper] named tunnel down -> starting cloudflared named tunnel" -ForegroundColor Yellow
        Start-Process powershell -WindowStyle Minimized -ArgumentList '-NoProfile','-Command',"& `"$cloudflaredExe`" tunnel --config `"$cloudflaredConfig`" run"
        return
    }

    if (Test-Path $cloudflaredExe) {
        Write-Host "[keeper] tunnel down -> starting quick tunnel" -ForegroundColor Yellow
        Start-Process powershell -WindowStyle Minimized -ArgumentList '-NoProfile','-Command',"& `"$cloudflaredExe`" tunnel --url http://localhost:8000"
    }
}

Write-Host "[keeper] Cloudflare keepalive started. Interval: $CheckIntervalSeconds sec" -ForegroundColor Cyan
if ($UseNamedTunnel) {
    Write-Host "[keeper] Named tunnel mode enabled: https://app.dingking.bet/dashboard" -ForegroundColor Green
} else {
    Write-Host "[keeper] Quick tunnel mode enabled. Keep the tunnel process alive to keep same URL." -ForegroundColor Green
}

while ($true) {
    Ensure-Backend
    Ensure-Tunnel
    Start-Sleep -Seconds ([Math]::Max(5, $CheckIntervalSeconds))
}
