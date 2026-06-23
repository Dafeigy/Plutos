# Order tracking via push-driven cache with CTP query fallback

**Date:** 2026-06-23  
**Severity:** Medium — `POST /order` reports only initial acknowledgment; caller has no way to retrieve final fill/cancel status.  
**Files changed:** `app/ctp/bridge.py`, `app/models.py`, `app/ctp/trader_client.py`, `app/api/order.py`, `app/main.py`

## Symptom

`POST /order` returned a snapshot of the order at submission time — usually
`"报单已提交"` or `"未成交队列中"`.  The caller had no mechanism to discover
whether the order later filled, partially filled, or was cancelled.  The only
visibility into subsequent status changes was the server's log output from
`OnRtnOrder` / `OnRtnTrade` push callbacks.

## Design

Two CTP mechanisms combine to provide complete order lifecycle tracking:

### Push path (primary — hot cache)

`OnRtnOrder` fires every time an order's status changes (submit→ack→queued→
filled/cancelled).  `OnRtnTrade` fires for each individual trade fill.  Both
already existed in `TraderClient` but only the first `OnRtnOrder` was acted on
(to resolve the pending Future).  Subsequent pushes were discarded.

A new `OrderCache` (thread-safe `dict[str, dict]`) now captures **every** push.
`GET /order/{order_ref}` reads directly from this cache — zero latency.

### Pull path (fallback — cold cache)

CTP provides `ReqQryOrder` and `ReqQryTrade` query APIs, already wrapped by
`TraderClient.query_order()` / `query_trade()`.  These return all orders/trades
for the investor (CTP does not support filtering by OrderRef at the API level).

When `GET /order/{order_ref}` misses the cache (e.g. after service restart),
it falls back to these queries, filters by OrderRef in Python, and populates
the cache so subsequent requests take the fast path.

## Implementation

All changes are additive — existing `POST /order` response is unchanged.

### 1. `OrderCache` class (bridge.py, L195-L262)

```python
class OrderCache:
    update_order(snapshot)     # called from OnRtnOrder / OnRspOrderInsert
    add_trade(order_ref, snap) # called from OnRtnTrade
    get(order_ref) -> dict     # read current order state
    get_trades(order_ref) -> list[dict]  # read accumulated trade fills
    put(order_ref, data)       # explicit store (used by fallback query path)
    put_trades(order_ref, lst) # explicit store (used by fallback query path)
```

All access serialised with `threading.Lock` — CTP callbacks run on CTP's
internal threads, route handlers run on the asyncio event loop.

### 2. `TraderClient` callback wiring (trader_client.py)

- `OnRspOrderInsert`: `_order_cache.update_order(snapshot)` before resolving the Future.
- `OnRtnOrder`: `_order_cache.update_order(snapshot)` on **every** push, not just the first.
- `OnRtnTrade`: `_order_cache.add_trade(snapshot.OrderRef, snapshot)` for every fill.

### 3. `GET /order/{order_ref}` endpoint (order.py, L199-L263)

New endpoint.  Three code paths:

| Scenario | What happens |
|---|---|
| Cache hit | `cache.get(ref)` → `_build_detail()` → instant response with trades |
| Cache miss, query hit | `ReqQryOrder("")` + `ReqQryTrade("")` → filter by OrderRef → populate cache → respond |
| Cache miss, query miss | HTTP 404 |

Shared helper `_build_detail(order_dict, trades_list) -> OrderDetailResponse`
normalises the raw CTP enums into human-readable labels and maps both
`CThostFtdcInputOrderField` and `CThostFtdcOrderField` dicts (which use
different field names for order status).

### 4. Response models (models.py)

- `TradeItem`: trade_id, price, volume, direction, offset_flag, trade_time
- `OrderDetailResponse`: all `OrderResponse` fields + exchange_id, offset_flag,
  volume_traded (key progress field), insert_time, update_time, cancel_time,
  trades list

### 5. Lifespan wiring (main.py)

`OrderCache` instance created at startup, passed to `TraderClient`, stored on
`app.state.order_cache` for route access.

## Verification

- All files pass `ast.parse()` syntax checks.
- `OrderCache` unit-testable in isolation (plain Python, no CTP .so required).
- Runtime verification path:
  ```
  POST /order → { "order_ref": "5", "order_status": "报单已提交", ... }
  GET  /order/5 → { "order_ref": "5", "volume_traded": 0, "trades": [], ... }
  # ... wait for fill via OnRtnTrade push ...
  GET  /order/5 → { "order_ref": "5", "volume_traded": 1, "order_status": "部分成交",
                     "trades": [{"trade_id": "T001", "price": 8300, ...}] }
  ```

## Follow-up: OrderSysID lookup endpoint

After the initial implementation it became clear that `OrderRef`-based lookups
have a session-scope problem:

- `OrderRef` is a client-side counter that resets to 1 on every login.
- After a service restart, `GET /order/5` could match a stale order from a
  previous session (the CTP fallback query returns all orders regardless of
  session).
- The stable, globally-unique identifier is `OrderSysID` — assigned by the
  exchange when the order is accepted.

### Changes

**`OrderCache` dual-index** (bridge.py):

Added `_by_sysid: dict[str, str]` mapping `OrderSysID → OrderRef`.  Populated
automatically by `update_order()` and `put()` when `OrderSysID` is non-empty
(the field is empty in early callbacks and gets filled after exchange
acceptance).

New reader methods: `get_by_sysid()`, `get_trades_by_sysid()`.

**`GET /order/by-sysid/{order_sys_id}`** (order.py) → later replaced by **`POST /order/lookup`**:

Mirrors `GET /order/{order_ref}` but indexes by `OrderSysID`.  Cache hit: uses
the sysid→ref index.  Cache miss: fallback queries iterate all orders and match
on `OrderSysID` instead of `OrderRef`.

The sysid endpoint was changed from `GET /order/by-sysid/{order_sys_id}` to
`POST /order/lookup` with a JSON body `{"order_sys_id": "..."}` because
exchange-assigned OrderSysID values can contain whitespace, which is fragile in
URL path segments and requires percent-encoding.  JSON bodies don't have this
problem.

**Shared `_fallback_lookup()` helper** (order.py):

Extracted the duplicated query-logic into a single async helper parameterised
by `by="order_ref" | "sysid"`.  Both endpoints now delegate to this helper on
cache miss, avoiding copy-paste drift.

### Result — two endpoints, two use-cases

| Endpoint | Key | Scope | Best for |
|---|---|---|---|
| `GET /order/{order_ref}` | `OrderRef` | Session | Immediate polling right after submit (OrderRef is available instantly) |
| `POST /order/lookup` | `OrderSysID` (body) | Global | Cross-restart lookups, persistent storage, audit trails |

Callers should switch from OrderRef to OrderSysID once the exchange assigns
one — typically by the second `OnRtnOrder` push.
