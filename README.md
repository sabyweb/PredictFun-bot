# PredictFun Bot

Predict.Fun trading bot, ported from the Polymarket bot architecture.

**Current phase:** mainnet shadow / read-only dry run. No real orders are placed.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your API key.

## Run shadow dry run

```bash
python run_shadow.py
```

## Safety

The bot defaults to `SHADOW` mode. In this mode it will:
- fetch market data and orderbooks
- compute intended prices and sizes
- log "would place" / "would dump" actions
- never call `POST /v1/orders` or `POST /v1/orders/remove`

