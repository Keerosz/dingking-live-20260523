# DINGKING Link Diagnostics (Safe)

This guide creates a safe diagnostics package you can paste into ChatGPT.

## What Is Safe

- Secrets are never printed.
- `THE_ODDS_API_KEY` is reported only as `PRESENT` or `MISSING`.
- No cookies, tokens, sportsbook logins, or private keys are logged.

## 1) Start Server (PowerShell)

```powershell
Set-Location C:\Users\Spann\weather-warfare
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 2) Run Debug Script

```powershell
Set-Location C:\Users\Spann\weather-warfare
python -m app.debug_links
```

Optional custom base URL:

```powershell
$env:DINGKING_BASE_URL = "http://127.0.0.1:8000"
python -m app.debug_links
```

## 3) Manual Endpoint Test (PowerShell)

```powershell
$body = @{
  book = "fanduel"
  slips = @(
    @{
      legs = @(
        @{ player_name = "Aaron Judge" },
        @{ player_name = "Juan Soto" }
      )
    }
  )
} | ConvertTo-Json -Depth 6

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/dashboard/sportsbook/resolve-links" `
  -ContentType "application/json" `
  -Body $body
```

DraftKings:

```powershell
$body = @{
  book = "draftkings"
  slips = @(
    @{
      legs = @(
        @{ player_name = "Aaron Judge" },
        @{ player_name = "Juan Soto" }
      )
    }
  )
} | ConvertTo-Json -Depth 6

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/dashboard/sportsbook/resolve-links" `
  -ContentType "application/json" `
  -Body $body
```

Action Network:

```powershell
$body = @{
  book = "actionnetwork"
  slips = @(
    @{
      legs = @(
        @{ player_name = "Aaron Judge" },
        @{ player_name = "Juan Soto" }
      )
    }
  )
} | ConvertTo-Json -Depth 6

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/dashboard/sportsbook/resolve-links" `
  -ContentType "application/json" `
  -Body $body
```

## 4) Files To Paste Into ChatGPT

After running diagnostics, paste these files:

- `data/debug_links_report.json`
- `data/debug_links_response_fanduel.json`
- `data/debug_links_response_draftkings.json`
- `data/debug_links_response_actionnetwork.json`

Also paste the terminal output from `python -m app.debug_links`.

## 5) What ChatGPT Can Infer

The report includes:

- server reachable
- endpoint status code
- response keys
- link count
- fallback count
- unresolved players
- cache status
- provider status
- timeout status
- exact failure stage
- fallback reason

Common fallback reasons:

- `missing_api_key`
- `provider_timeout`
- `provider_unavailable`
- `no_market_match`
- `player_match_failed`
