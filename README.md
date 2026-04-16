# wani_stockbot

FastAPI-based stock screening service for daily post-market foreign/institutional flow analysis.

## Run

```bash
uvicorn main:app --reload
```

KRX live collection requires credentials:

```bash
$env:KRX_ID=""
$env:KRX_PW=""
```

## Endpoints

- `GET /health`
- `POST /screen`
- `POST /screen/live`

## Input

Use [data/input/daily_snapshot.example.json](/C:/Users/jogic/Git/wani_stockbot/data/input/daily_snapshot.example.json) as the request template.

`/screen` accepts manual JSON snapshots. `/screen/live` fetches live KRX data through the configured provider.

## Current scope

- FastAPI app bootstrap
- Daily 16:10 KST scheduler scaffold
- File-based result/history/audit persistence
- Rule-based filtering and scoring engine
- KRX-backed live provider with local JSON fallback

## Next implementation targets

- FSS/Naver secondary collector adapters
- Telegram notifier
- Backtest pipeline
- Database persistence
