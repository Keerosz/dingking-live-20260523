Set-Location C:\Users\Spann\weather-warfare

Write-Host 'Starting DINGKING keepalive agent for anywhere-phone access...' -ForegroundColor Cyan

$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($conn) {
  $procIds = $conn | Select-Object -ExpandProperty OwningProcess -Unique
  foreach ($procId in $procIds) {
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
  }
}

Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match 'cloudflared' -or $_.CommandLine -match 'cloudflared' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Process powershell -ArgumentList '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', 'C:\Users\Spann\weather-warfare\keep_cloudflare_active.ps1' | Out-Null

Write-Host 'Keepalive agent started in a new window.' -ForegroundColor Green
Write-Host 'Watch tunnel output for the current https://...trycloudflare.com URL.' -ForegroundColor Yellow
Write-Host 'Fallback local URL (same Wi-Fi): http://192.168.1.75:8000/dashboard' -ForegroundColor Yellow

exit
