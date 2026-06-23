# Order Tracking Implementation Plan

## Goal

Add `GET /order/{order_ref}` endpoint so users can poll order status after submission. Uses push-driven memory cache (OnRtnOrder/OnRtnTrade updates) with CTP query fallback.

## Data Flow

```
POST /order → returns { order_ref, order_sys_id, ... }
                    ↓
   OnRtnOrder ──→ OrderCache.update_order()  ← every push, not just first
   OnRtnTrade ──→ OrderCache.add_trade()     ← every push
                    ↓
GET /order/{ref} → cache hit: instant response
                 → cache miss: ReqQryOrder + ReqQryTrade fallback
```

## Changes (5 files)

### 1. `app/ctp/bridge.py` — Add `OrderCache` class

- Thread-safe `dict[str, dict]` for orders (keyed by OrderRef)
- Thread-safe `dict[str, list[dict]]` for trades (keyed by OrderRef)
- Methods: `update_order(snapshot)`, `add_trade(order_ref, snapshot)`, `get(order_ref)`, `get_trades(order_ref)`
- Store `vars(snapshot)` dicts (SimpleNamespace → dict conversion in caller)

### 2. `app/ctp/trader_client.py` — Wire OrderCache into callbacks

- `__init__` accepts `order_cache: OrderCache` parameter
- `OnRtnOrder`: **always** call `order_cache.update_order()` (not just on first push), keep existing `resolve_by_order_ref` logic
- `OnRtnTrade`: call `order_cache.add_trade()` for every trade push

### 3. `app/models.py` — New response models

- `TradeItem`: trade_id, price, volume, direction, offset_flag, trade_time
- `OrderDetailResponse`: extends current fields + volume_traded, exchange_id, offset_flag, insert_time, update_time, cancel_time, trades list

### 4. `app/api/order.py` — New GET endpoint + refactor

- New `GET /order/{order_ref}` endpoint:
  1. Check `order_cache.get(order_ref)` → hit: return immediately with trades
  2. Cache miss: call `trader.query_order("")` + `trader.query_trade("")` sequentially, filter by OrderRef, populate cache with results, return
  3. Not found in either → 404
- Shared `_build_detail()` helper normalizes dict → OrderDetailResponse

### 5. `app/main.py` — Wire OrderCache through lifespan

- Create `OrderCache()` instance
- Pass to `TraderClient(settings, query_store, order_store, order_cache)`
- Store on `app.state.order_cache`

## Key Design Decisions

- **Cache key is OrderRef** — unique within our session. OnRtnOrder always fires before OnRtnTrade for a given order, so trade caching won't hit a missing order key.
- **Store raw `vars()` dicts** not SimpleNamespace — simpler, serializable, no swig memory issues (snapshot already copied via `_copy_ctp_struct`).
- **Fallback queries use empty InstrumentID** — `ReqQryOrder("")` / `ReqQryTrade("")` return ALL orders/trades for the investor. Filter in Python by OrderRef. SimNow accounts have small datasets so this is fine.
- **No breaking changes** — existing POST /order response unchanged. New endpoint is additive.
