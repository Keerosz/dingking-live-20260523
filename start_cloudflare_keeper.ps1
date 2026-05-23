Set-Location C:\Users\Spann\weather-warfare

Write-Host 'Launching DINGKING Cloudflare keepalive agent...' -ForegroundColor Cyan

$runningKeeper = Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'keep_cloudflare_active.ps1' }

if ($runningKeeper) {
  Write-Host 'Keepalive agent is already running.' -ForegroundColor Yellow
  $runningKeeper | Select-Object ProcessId, CommandLine | Format-List
  exit
}

Start-Process powershell -ArgumentList '-NoExit','-ExecutionPolicy','Bypass','-File','C:\Users\Spann\weather-warfare\keep_cloudflare_active.ps1'

Write-Host 'Keepalive agent started.' -ForegroundColor Green
Write-Host 'Quick tunnel mode is default. Use the current https://...trycloudflare.com URL while keeper is running.' -ForegroundColor Yellow
