Set-Location C:\Users\Spann\weather-warfare

$ips = Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } |
  Select-Object -ExpandProperty IPAddress -Unique

Write-Host 'Starting DINGKING for phone access on port 8000...' -ForegroundColor Cyan
Write-Host 'Open one of these URLs on your phone (same Wi-Fi):' -ForegroundColor Yellow
foreach ($ip in $ips) {
  Write-Host ("  http://{0}:8000/dashboard" -f $ip) -ForegroundColor Green
}

uvicorn app.main:app --host 0.0.0.0 --port 8000
