# Fix CTP struct memory-reuse corruption in query responses

**Date:** 2026-06-22  
**Severity:** High — all query endpoints return corrupted data most of the time.  
**Files changed:** `app/ctp/trader_client.py`, `app/ctp/md_client.py`

## Symptom

`GET /account/balance` returned very small (garbage) numbers ~80% of the time.
`GET /market/{id}/price` was also affected, as was `POST /order`. The data was
*occasionally* correct — roughly 1–2 times out of every 10 requests.

## Root Cause

CTP's swig (Python) bindings reuse the underlying C memory after a callback
returns. The project stored references to the raw swig wrapper objects
(`CThostFtdcTradingAccountField`, `CThostFtdcDepthMarketDataField`, etc.) in:

- `FutureStore._accumulators` — query results (trader)
- `FutureStore.resolve_direct()` — order-insert confirmations (trader)
- `MdClient._cache` — market-data tick cache

By the time the asyncio event loop read those objects, CTP had already reused
the memory for the next callback (market-data push, another query, etc.),
so the Python wrapper now pointed at overwritten / garbage data.

**Why some requests were correct:** pure luck — the event loop happened to
read the data before CTP recycled the memory.

## Fix

Added `_copy_ctp_struct(item) -> SimpleNamespace` in both `trader_client.py`
and `md_client.py`. This extracts every public, non-callable attribute from
the swig wrapper into a plain Python dict and wraps it in a `SimpleNamespace`,
which preserves dot-access (`item.Balance`, `item.InstrumentID`, etc.).

Three injection points:

| Location | Before | After |
|---|---|---|
| `TraderClient._on_query_callback` | `accumulate(rid, item)` | `accumulate(rid, _copy_ctp_struct(item))` |
| `TraderClient.OnRspOrderInsert` | `resolve_direct(rid, pInputOrder)` | `resolve_direct(rid, _copy_ctp_struct(pInputOrder))` |
| `MdClient.OnRtnDepthMarketData` | `cache[id] = pDepthMarketData` | `cache[id] = _copy_ctp_struct(pDepthMarketData)` |

No API consumers needed changes — `SimpleNamespace` supports the same
dot-access and `getattr()` patterns the routes already use.

## Verification

- `python -m py_compile` passes on both files.
- Dot-access compatibility smoke-tested for `BalanceResponse`, `PriceResponse`,
  and `OrderResponse` field mappings.
- Runtime verification: re-deploy and hit `/account/balance` 20+ times —
  every response now returns consistent, correct values.
