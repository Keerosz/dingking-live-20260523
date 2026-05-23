Set-Location C:\Users\Spann\weather-warfare

Write-Host 'Launching Gambly session keeper...' -ForegroundColor Cyan

$existing = Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'app.gambly_session_keeper' }

if ($existing) {
  Write-Host 'Gambly keeper already running.' -ForegroundColor Yellow
  $existing | Select-Object ProcessId, CommandLine | Format-List
  exit
}

Start-Process powershell -ArgumentList '-NoExit','-ExecutionPolicy','Bypass','-Command','Set-Location C:\Users\Spann\weather-warfare; $env:GAMBLY_KEEPER_HEADED="1"; python -m app.gambly_session_keeper'

Write-Host 'Gambly keeper started (headed mode for first-login support).' -ForegroundColor Green
Write-Host 'Status file: C:\Users\Spann\weather-warfare\data\gambly_session_status.json' -ForegroundColor Yellow
Write-Host 'After first successful login, you can run headless by setting GAMBLY_KEEPER_HEADED=0.' -ForegroundColor Yellow
