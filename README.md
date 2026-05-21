# Trading SAR Bot

Telegram-controlled trading bot for BingX futures strategies.

The bot combines two strategy modules in one Telegram interface:

- **SAR** — DOGE-USDT futures using Parabolic SAR + SMA confirmation.
- **EMA** — SOL-USDT futures using EMA pullback logic.

The bot supports paper mode and live execution configuration through environment variables.

## Features

- Telegram control interface with inline buttons.
- BingX API integration.
- SAR and EMA strategy modules.
- Persistent local state files.
- Trade log CSV.
- Railway deployment files.

## Environment

Copy the template and fill real values locally or in Railway:

```bash
cp .env.example .env
```

Required variables:

- `BINGX_API_KEY` — BingX API key.
- `BINGX_SECRET_KEY` — BingX API secret.
- `TELEGRAM_TOKEN` — Telegram bot token.
- `TELEGRAM_CHAT_ID` — target Telegram chat ID.

Common optional variables:

- `PAPER_MODE` — `true` to log signals without live execution.
- `SAR_PAPER_MODE` — per-strategy SAR live/paper override.
- `SAR_SYMBOL` — default `DOGE-USDT`.
- `MARGIN` — base margin per trade.
- `LEVERAGE` — leverage multiplier.
- `DATA_DIR` — directory for state files and logs.

## Local Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python bot.py
```

## Deployment

The repository includes:

- `Procfile`
- `railway.toml`
- `start.sh`

Set secrets in Railway variables. Do not commit `.env`.

## Risk Notes

- Live futures trading can lose money quickly.
- Keep paper mode enabled until the strategy is validated.
- Use small size and strict API permissions.
- Avoid storing exchange keys outside environment variables.
