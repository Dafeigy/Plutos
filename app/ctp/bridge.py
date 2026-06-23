"""
Core bridge between CTP's callback-driven threads and FastAPI's async event loop.

CTP delivers results via callbacks running in CTP's own internal threads.
FastAPI route handlers are async coroutines.  The bridge uses
concurrent.futures.Future (thread-safe) + asyncio.wrap_future() to connect them.
"""

import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Exceptions ───────────────────────────────────────────────────────────────

class CTPError(Exception):
    """Carries a CTP error code and message across thread boundaries."""

    def __init__(self, error_id: int, error_msg: str):
        self.error_id = error_id
        self.error_msg = error_msg
        super().__init__(f"CTP Error {error_id}: {error_msg}")


# ── Trader State ─────────────────────────────────────────────────────────────

@dataclass
class TraderState:
    """Login session state shared between TraderClient and CTP callbacks."""

    front_id: int = 0
    session_id: int = 0
    trading_day: str = ""
    order_ref: int = 1
    logged_in: bool = False


# ── Future Store ─────────────────────────────────────────────────────────────

class FutureStore:
    """
    Thread-safe registry of pending Futures, keyed by request_id.

    CTP queries return multiple records: each intermediate callback delivers one
    record, and the final callback (bIsLast=True) signals completion.  This store
    accumulates records in a list and resolves the Future with the full list on
    the final callback.

    A background daemon thread sweeps for expired Futures and times them out.
    """

    def __init__(self, default_timeout: float = 10.0):
        self._default_timeout = default_timeout
        self._lock = threading.Lock()
        self._store: dict[int, Future] = {}
        self._accumulators: dict[int, list[Any]] = {}
        self._timestamps: dict[int, float] = {}
        # Secondary index for order-error mapping (keyed by OrderRef string)
        self._order_ref_map: dict[str, int] = {}
        self._running = False
        self._cleanup_thread: Optional[threading.Thread] = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="future-store-cleanup"
        )
        self._cleanup_thread.start()

    def stop(self) -> None:
        self._running = False

    # ── Future management ────────────────────────────────────────────────

    def create(self, request_id: int, timeout: Optional[float] = None) -> Future:
        """Register a new Future and return it."""
        future: Future = Future()
        with self._lock:
            self._store[request_id] = future
            self._accumulators[request_id] = []
            self._timestamps[request_id] = time.monotonic()
        return future

    def create_with_order_ref(
        self, request_id: int, order_ref: str, timeout: Optional[float] = None
    ) -> Future:
        """Register a new Future and also index it by OrderRef (for order error callbacks)."""
        future = self.create(request_id, timeout=timeout)
        with self._lock:
            self._order_ref_map[order_ref] = request_id
        return future

    def accumulate(self, request_id: int, item: Any) -> None:
        """Append an intermediate result record (called from CTP callback)."""
        with self._lock:
            if request_id in self._accumulators:
                self._accumulators[request_id].append(item)

    def resolve_with_accumulator(self, request_id: int) -> None:
        """Resolve the Future with the accumulated list of records."""
        with self._lock:
            future = self._store.pop(request_id, None)
            self._timestamps.pop(request_id, None)
            items = self._accumulators.pop(request_id, [])
        if future and not future.done():
            future.set_result(items)

    def resolve_direct(self, request_id: int, result: Any) -> None:
        """Resolve the Future with a single value (not an accumulated list)."""
        with self._lock:
            future = self._store.pop(request_id, None)
            self._timestamps.pop(request_id, None)
            self._accumulators.pop(request_id, None)
        if future and not future.done():
            future.set_result(result)

    def reject(self, request_id: int, error_id: int, error_msg: str) -> None:
        """Reject the Future with a CTPError."""
        with self._lock:
            future = self._store.pop(request_id, None)
            self._timestamps.pop(request_id, None)
            self._accumulators.pop(request_id, None)
        if future and not future.done():
            future.set_exception(CTPError(error_id, error_msg))

    def reject_by_order_ref(self, order_ref: str, error_id: int, error_msg: str) -> bool:
        """
        Reject a Future by OrderRef (for OnErrRtnOrderInsert which lacks nRequestID).
        Returns True if a matching Future was found, False otherwise.
        """
        with self._lock:
            request_id = self._order_ref_map.pop(order_ref, None)
        if request_id is not None:
            self.reject(request_id, error_id, error_msg)
            return True
        return False

    def resolve_by_order_ref(self, order_ref: str, result: Any) -> bool:
        """
        Resolve a Future by OrderRef (for OnRtnOrder push notifications).

        Some CTP environments deliver order confirmation via OnRtnOrder pushes
        rather than OnRspOrderInsert responses.  This lets the first status
        push resolve the pending Future so the API doesn't time out at 15 s
        when the order was actually accepted.

        Returns True if a matching pending Future was found, False otherwise
        (e.g. already resolved by OnRspOrderInsert, or stale OrderRef).
        """
        with self._lock:
            request_id = self._order_ref_map.pop(order_ref, None)
        if request_id is not None:
            self.resolve_direct(request_id, result)
            return True
        return False

    # ── Cleanup ──────────────────────────────────────────────────────────

    def _cleanup_loop(self) -> None:
        while self._running:
            timeout = self._default_timeout
            now = time.monotonic()
            with self._lock:
                expired = [
                    rid
                    for rid, ts in self._timestamps.items()
                    if now - ts > timeout
                ]
            for rid in expired:
                with self._lock:
                    future = self._store.pop(rid, None)
                    self._timestamps.pop(rid, None)
                    self._accumulators.pop(rid, None)
                    # Also clean up order_ref_map entries pointing to this rid
                    refs_to_remove = [
                        ref for ref, r in self._order_ref_map.items() if r == rid
                    ]
                    for ref in refs_to_remove:
                        del self._order_ref_map[ref]
                if future and not future.done():
                    future.set_exception(
                        TimeoutError(f"Request {rid} timed out after {timeout}s")
                    )
            time.sleep(1.0)


# ── Order Cache ─────────────────────────────────────────────────────────────

class OrderCache:
    """
    Thread-safe cache of order state, updated by OnRtnOrder / OnRtnTrade pushes.

    Stores snapshot dicts (not CTP swig wrappers — snapshots are already copied
    via _copy_ctp_struct before reaching this cache).

    Dual-indexed:
      - by OrderRef (session-scoped, available immediately)
      - by OrderSysID (exchange-assigned, globally unique, survives restarts)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._orders: dict[str, dict] = {}       # OrderRef  → order dict
        self._trades: dict[str, list[dict]] = {} # OrderRef  → trade list
        self._by_sysid: dict[str, str] = {}      # OrderSysID → OrderRef

    # ── Writers (called from CTP callbacks) ───────────────────────────────

    def update_order(self, snapshot) -> None:
        """Store an order snapshot dict (from vars(snapshot) of a copied struct)."""
        data = vars(snapshot) if not isinstance(snapshot, dict) else snapshot
        order_ref = data.get("OrderRef", "")
        if not order_ref:
            return
        with self._lock:
            self._orders[order_ref] = data
            # Populate sysid index once the exchange assigns an ID
            sysid = data.get("OrderSysID", "")
            if sysid:
                self._by_sysid[sysid] = order_ref

    def add_trade(self, order_ref: str, snapshot) -> None:
        """Append a trade snapshot dict, keyed by OrderRef."""
        if not order_ref:
            return
        data = vars(snapshot) if not isinstance(snapshot, dict) else snapshot
        with self._lock:
            self._trades.setdefault(order_ref, []).append(data)

    # ── Readers (called from async route handlers) ────────────────────────

    def get(self, order_ref: str) -> dict | None:
        """Return the latest order snapshot by OrderRef, or None."""
        with self._lock:
            return self._orders.get(order_ref)

    def get_by_sysid(self, sysid: str) -> dict | None:
        """Return the latest order snapshot by OrderSysID, or None."""
        with self._lock:
            order_ref = self._by_sysid.get(sysid)
            if order_ref:
                return self._orders.get(order_ref)
            return None

    def get_trades(self, order_ref: str) -> list[dict]:
        """Return all trade snapshots for an order (empty list if none)."""
        with self._lock:
            return list(self._trades.get(order_ref, []))

    def get_trades_by_sysid(self, sysid: str) -> list[dict]:
        """Return trades for an order, looked up by OrderSysID."""
        with self._lock:
            order_ref = self._by_sysid.get(sysid)
            if order_ref:
                return list(self._trades.get(order_ref, []))
            return []

    # ── Explicit storage (fallback query path) ────────────────────────────

    def put(self, order_ref: str, order_data: dict) -> None:
        """Explicitly store an order dict (used by fallback query path)."""
        with self._lock:
            self._orders[order_ref] = order_data
            sysid = order_data.get("OrderSysID", "")
            if sysid:
                self._by_sysid[sysid] = order_ref

    def put_trades(self, order_ref: str, trade_list: list[dict]) -> None:
        """Explicitly store trade dicts (used by fallback query path)."""
        with self._lock:
            self._trades[order_ref] = list(trade_list)
