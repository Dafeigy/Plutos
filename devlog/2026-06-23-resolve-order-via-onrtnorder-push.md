# Resolve order Future via OnRtnOrder push to prevent false 408 timeouts

**Date:** 2026-06-23  
**Severity:** High — order endpoint returns HTTP 408 even though orders are accepted and filled.  
**Files changed:** `app/ctp/bridge.py`, `app/ctp/trader_client.py`, `app/api/order.py`

## Symptom

`POST /order` returned HTTP 408 ("Order insertion timed out") after 15 s, but the
CTP log showed the order was actually accepted and later filled:

```
00:15:43 OnRtnOrder OrderRef=2 OrderSysID=       Status=a  报单已提交
00:15:43 OnRtnOrder OrderRef=2 OrderSysID=35996   Status=3  未成交队列中
00:15:48 API → 408 Timeout
00:15:53 OnRtnOrder OrderRef=2 OrderSysID=35996   Status=0  全部成交
```

The money was deducted, the position was opened, but the caller got an error.

## Root Cause

`insert_order()` registered its Future only with `OnRspOrderInsert` as the
resolution path.  In some CTP environments / network conditions the
`OnRspOrderInsert` callback does not fire (or fires without `bIsLast=True`),
but order status is delivered exclusively through `OnRtnOrder` push
notifications.  Because no callback ever called `future.set_result()`, the
Future sat until the 15 s cleanup daemon expired it.

## Fix

Three changes, all backward-compatible:

### 1. `FutureStore.resolve_by_order_ref()` (bridge.py)

Mirrors the existing `reject_by_order_ref()`.  Looks up the pending Future
via the `OrderRef → request_id` secondary index and resolves it.

### 2. `OnRtnOrder` now resolves the pending Future (trader_client.py)

On the first push for an OrderRef the callback:
1. Copies the `CThostFtdcOrderField` into a `SimpleNamespace` (per the
   earlier memory-reuse fix) — this also prevents subsequent pushes from
   overwriting the resolved result.
2. Aliases `OrderStatus → OrderSubmitStatus` so `order.py` can read the
   status field under the same name regardless of which CTP path delivered it.
3. Calls `resolve_by_order_ref()`.  If the Future was already resolved by
   `OnRspOrderInsert`, this is a harmless no-op (`future.done()` guard).

### 3. Status map updated (order.py)

Added `"a": "报单已提交"` for the broker-acknowledgment push that arrives
before the exchange assigns an `OrderSysID`.

## Result

- `OnRspOrderInsert` still works as the primary resolution path when it fires.
- `OnRtnOrder` now acts as a safety net — the first status push resolves the
  Future so the API returns promptly.
- The caller gets a real status (`报单已提交`, `未成交队列中`, etc.) instead
  of a 408 error.
- Subsequent `OnRtnOrder` pushes (fill confirmations, cancels) are logged but
  no longer leave the caller hanging.
