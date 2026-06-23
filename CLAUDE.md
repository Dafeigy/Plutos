# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Plutos is a FastAPI service wrapping [OpenCTP](https://github.com/openctp/openctp) to expose futures trading via REST. The core challenge: CTP is callback-driven (results arrive in CTP's own threads) while FastAPI is async request-response. The bridge uses `concurrent.futures.Future` + `asyncio.wrap_future()`.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Configure (edit with real credentials)
cp .env.example .env          # default environment
cp .env.example .env.prod     # production overrides
cp .env.example .env.test     # test overrides

# Run dev server (pick environment via APP_ENV)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload           # uses .env
APP_ENV=prod uvicorn app.main:app --host 0.0.0.0 --port 8000       # uses .env.prod
APP_ENV=test uvicorn app.main:app --host 0.0.0.0 --port 8000       # uses .env.test

# Health check
curl http://localhost:8000/health
```

## Architecture

```
Endpoint → create Future → call CTP API → await Future
                                        ↑
                      CTP callback → resolve Future
```

| Module | Responsibility |
|--------|---------------|
| `app/ctp/bridge.py` | `FutureStore` — thread-safe Future registry with accumulator pattern for multi-record CTP queries; background daemon thread for timeout cleanup |
| `app/ctp/md_client.py` | `MdClient(CThostFtdcMdSpi)` — connection, login (no credentials), instrument→price cache with `threading.Lock`, dynamic subscription via `run_in_executor` |
| `app/ctp/trader_client.py` | `TraderClient(CThostFtdcTraderSpi)` — auth→login→settlement flow, query methods returning Futures, `insert_order()` with atomic OrderRef |
| `app/api/account.py` | `GET /account/balance` — calls `trader.query_account()`, maps `CThostFtdcTradingAccountField` → `BalanceResponse` |
| `app/api/market.py` | `GET /market/{id}/price` — auto-subscribes on first request, reads from MdClient cache |
| `app/api/order.py` | `POST /order` — infers exchange from instrument prefix, calls `trader.insert_order()` |
| `app/main.py` | FastAPI lifespan: creates `FutureStore` instances (query 10s / order 15s), connects both CTP clients, pre-subscribes instruments, stores on `app.state` |
| `app/config.py` | `pydantic-settings` reading `.env`; `subscribe_list` property parses comma-separated instruments |
| `app/models.py` | Pydantic models: `PriceResponse`, `BalanceResponse`, `OrderRequest` (validated), `OrderResponse` |

## Reference Demos

The two demo scripts in the repo root show how the underlying CTP API works:

| File | What it demonstrates |
|------|---------------------|
| [td_demo.py](td_demo.py) | Full trader API lifecycle — auth, login, settlement, all query types, order insert/cancel. Reference for callback patterns. |
| [md_demo.py](md_demo.py) | Market data subscription — connect, login (no credentials), subscribe, receive `OnRtnDepthMarketData` push callbacks. |

## Key Design Notes

- **FutureStore accumulator pattern**: CTP query callbacks fire once per record + final `bIsLast=True`. `FutureStore.accumulate()` collects records; `resolve_with_accumulator()` resolves with the full list on the final callback. Single-result operations use `resolve_direct()`.
- **Two FutureStore instances**: queries use configurable timeout (default 10s), orders use 15s timeout. Separate stores mean order timeouts don't affect query timeouts.
- **Order error mapping**: `OnErrRtnOrderInsert` has no `nRequestID` — `FutureStore` maintains a secondary `OrderRef → request_id` index so the error callback can reject the correct Future.
- **Thread safety**: `FutureStore._store`/`_accumulators`/`_timestamps` protected by `threading.Lock`. `MdClient._cache` protected by `threading.Lock`. CTP callbacks run in CTP's internal threads — they must never touch asyncio objects directly.
- **Dynamic subscription**: `MdClient.ensure_subscribed()` deduplicates concurrent requests via `_pending_subs` dict; subscription runs in `run_in_executor` to avoid blocking the event loop.
- **Error handling**: CTP errors (`pRspInfo.ErrorID != 0`) map to `CTPError` exception → `HTTPException(500)`. Timeouts → `HTTPException(408)`. No data → `HTTPException(404)`.
- **Exchange inference**: `POST /order` requires `ExchangeID` which isn't in the API — `_infer_exchange()` uses a static prefix→exchange lookup covering SHFE/DCE/CZCE/CFFEX/INE/GFEX.
- **Login as startup gate**: Both clients must complete their login sequence before the server accepts requests. Failure prevents startup.

## Environment & Constraints

- **Python ≥ 3.10**, **Linux x86_64** (openctp-ctp `.so` is Linux-only; works on Windows for dev with the `.pyd`)
- CTP requires separate **market data** and **trader** front addresses (SimNow defaults: `tcp://180.168.146.187:10131` and `:10130`, broker `9999`)
- CTP does NOT allow duplicate logins — only one instance per account at a time
- CTP has no built-in reconnect — a disconnect requires service restart
- `.env` configures: `MD_FRONT`, `TRADE_FRONT`, `BROKER_ID`, `USER_ID`, `PASSWORD`, `APP_ID`, `AUTH_CODE`, `SUBSCRIBE_INSTRUMENTS`, `DEFAULT_TIMEOUT`
