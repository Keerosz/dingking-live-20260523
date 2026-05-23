# WEATHER WARFARE

WEATHER WARFARE is an MLB Home Run portfolio-generation system for diversified parlay construction.

It builds many distinct slate stories instead of concentrating on the same chalk outcomes.

## Core Principles

- Hard 2x max exposure per player across the whole portfolio (default, mandatory).
- Portfolio-first construction over projection-first rankings.
- Explicit archetype diversity across slips.
- Exposure validation and repeated-pairing controls.

## Tech Stack

- Python
- pandas
- numpy
- sqlite
- FastAPI

## Quick Start

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run API:

```bash
uvicorn app.main:app --reload
```

4. Generate a sample portfolio:

```bash
python -m app.cli
```

## API

- `POST /portfolio/generate`: Generate a portfolio board from input records.
- `GET /portfolio/{run_id}`: Retrieve a previously stored portfolio run.
- `GET /health`: Service health check.
- `GET /learning/archetypes`: Archetype performance snapshot.
- `GET /learning/archetype-weights`: Adaptive archetype allocation weights.
- `POST /learning/decision-outcomes`: Record settled outcomes for playbook/category learning.
- `GET /learning/categories`: Category and subcategory performance from settled outcomes.
- `GET /learning/playbooks`: Playbook performance from settled outcomes.
- `GET /learning/playbook/recommend`: Recommend playbook + deep-link order for current context.
- `POST /learning/slips/could-have-hit`: Estimate how many slips would have hit given a list of winning players.
- `POST /signals/twitter`: Store Twitter/X signals for context features.
- `GET /signals/twitter`: Retrieve recent Twitter/X signals + aggregated feature snapshot.

Learning model notes:

- Core learning should use settled outcome data from `POST /learning/decision-outcomes`.
- Twitter/X signals should be context-only and confirmation features, not direct labels.
- Playbook recommendation is separate from model training and includes deep-link routing order.

## Real-Time Research Agents

Weather Warfare now supports autonomous research-sync agents.

One-time setup:

1. Log into PropFinder in your browser.
2. Preferred: bootstrap Playwright authenticated state (stays logged in longer than cookie-only mode).
3. Set environment variables before starting API:

```powershell
$env:PROPFINDER_AUTH_MODE='playwright'
$env:PROPFINDER_EMAIL='your_email'
$env:PROPFINDER_PASSWORD='your_password'
$env:WW_AGENT_AUTO_START='1'
$env:WW_SYNC_INTERVAL_SECONDS='900'
$env:GAMBLY_ENABLED='true'
```

Install Playwright browser binary once:

```bash
playwright install chromium
```

Optional one-time interactive bootstrap instead of env creds:

```bash
python -m app.login_bootstrap
```

Cookie fallback mode is still supported:

```powershell
$env:PROPFINDER_AUTH_MODE='cookie'
$env:PROPFINDER_SESSION_COOKIE='paste_cookie_here'
```

Then run:

```bash
uvicorn app.main:app --reload
```

Agent endpoints:

- `GET /agents/status`
- `POST /agents/run-once`
- `POST /agents/start?every_seconds=900`
- `POST /agents/stop`
- `POST /agents/bootstrap-login`

Generated files:

- `data/propfinder_raw_latest.csv`
- `data/propfinder_normalized_latest.csv`

Each successful sync auto-generates a new portfolio run and stores it in sqlite.

## Notes

The system defaults to 4-leg HR parlays and supports configurable leg counts.

## Phone Without Wi-Fi

If your phone is on cellular (not same Wi-Fi), run:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_phone_anywhere.ps1
```

This now starts a keepalive agent that auto-recovers both backend + Cloudflare tunnel.

Manual keepalive launcher:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_cloudflare_keeper.ps1
```

Preferred stable URL:

```text
https://app.dingking.bet/dashboard
```

If neither tool is installed, install one:

```powershell
winget install Cloudflare.cloudflared
```

or

```powershell
winget install Ngrok.Ngrok
```

Dashboard offline behavior on phone:

- If the app was loaded before losing connection, it can show your last saved board.
- Generating new boards still needs server connectivity.

## Gambly Login Keeper (24/7 Session)

Run the Gambly session keeper agent:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_gambly_keeper.ps1
```

What it does:

- Uses a persistent Chromium profile at `data/gambly_chromium_profile` to keep Gambly cookies/session.
- Re-checks session every few minutes and refreshes page activity.
- Writes status to `data/gambly_session_status.json`.

Optional env vars:

```powershell
$env:GAMBLY_BASE_URL='https://gambly.com/bet-builder?type=straight%7Cplayer_prop&partials=exclude&alts=exclude&minPrice=-200&maxPrice=200&limit=10&sort_by=popularity'
$env:GAMBLY_KEEPALIVE_SECONDS='300'
$env:GAMBLY_KEEPER_HEADED='1'
```

Optional auto-login (if you want fully unattended refresh):

```powershell
$env:GAMBLY_EMAIL='your_email'
$env:GAMBLY_PASSWORD='your_password'
```

Note:

- No Gambly API key is required for link generation.
- If Gambly enforces re-auth/2FA, fully unattended 24/7 login may still require periodic manual confirmation.
