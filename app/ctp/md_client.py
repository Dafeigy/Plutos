"""
MdClient — wraps CTP's MdApi to maintain a thread-safe cache of latest prices.

Market data arrives as push callbacks (OnRtnDepthMarketData) from CTP's
internal thread.  This client stores the latest tick per instrument in a
lock-protected dict and provides async subscribe-on-first-request semantics.
"""

import asyncio
import logging
import os
import threading
from concurrent.futures import Future
from types import SimpleNamespace

from openctp_ctp import thostmduserapi as mdapi

from .bridge import CTPError


def _copy_ctp_struct(item) -> SimpleNamespace:
    """Deep-copy a CTP swig struct into a plain Python object.

    CTP reuses the underlying C memory after callbacks return.  Storing a
    reference to the swig wrapper means later reads may see overwritten /
    garbage data from a subsequent callback.  We must extract every field
    immediately while the callback is still on the stack.

    Returns a SimpleNamespace so existing ``item.SomeField`` dot-access
    continues to work everywhere.
    """
    data = {}
    for attr in dir(item):
        if attr.startswith("_") or attr.startswith("this"):
            continue
        try:
            val = getattr(item, attr)
            if not callable(val):
                data[attr] = val
        except Exception:
            continue
    return SimpleNamespace(**data)

logger = logging.getLogger(__name__)

# Dedicated flow directory so MdApi and TraderApi don't corrupt each other's
# flow files (they share filenames like TradingDay.con, DialogRsp.con, etc.)
_MD_FLOW_DIR = os.path.join(os.getcwd(), "flow", "md")


class MdClient(mdapi.CThostFtdcMdSpi):
    """
    CTP market data SPI implementation.

    Maintains instrument_id → latest DepthMarketData cache with threading.Lock.
    Dynamic subscription is triggered on first API request and runs via
    run_in_executor to avoid blocking the asyncio event loop.
    """

    def __init__(self, settings):
        super().__init__()
        self._settings = settings

        # Thread-safe price cache — stores SimpleNamespace snapshots (not raw
        # CTP swig wrappers) so values survive across callback memory reuse.
        self._cache: dict[str, SimpleNamespace] = {}
        self._cache_lock = threading.Lock()

        # Pending subscription futures (instrument_id → Future)
        # Deduplicates concurrent subscription requests for the same instrument
        self._pending_subs: dict[str, Future] = {}
        self._pending_lock = threading.Lock()

        self._login_future: Future | None = None

        # Ensure the flow directory exists before creating the API — otherwise
        # CTP segfaults trying to open flow files in a non-existent directory.
        os.makedirs(_MD_FLOW_DIR, exist_ok=True)

        # Create and configure the CTP MdApi instance.
        # RegisterFront must come BEFORE RegisterSpi for MdApi — the official
        # md_demo.py does it in this order, and reversing it causes
        # OnFrontConnected to never fire.
        self.api: mdapi.CThostFtdcMdApi = mdapi.CThostFtdcMdApi.CreateFtdcMdApi(
            _MD_FLOW_DIR
        )
        self.api.RegisterFront(settings.MD_FRONT)
        self.api.RegisterSpi(self)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def init(self) -> None:
        """Initiate connection to market data front (blocking)."""
        # Create the login future BEFORE Init() so it's guaranteed to exist
        # when CTP callbacks fire (they run on CTP's internal threads).
        if self._login_future is None:
            self._login_future = Future()
        self.api.Init()

    def release(self) -> None:
        """Release CTP resources."""
        self.api.RegisterSpi(None)
        self.api.Release()

    def await_login(self, timeout: float = 15.0) -> None:
        """
        Block until login completes (or fails).

        Market data channel does not authenticate — login is sent with empty
        fields and simply confirms the connection is ready for subscriptions.
        """
        if self._login_future is None:
            raise RuntimeError("init() must be called before await_login()")
        result = self._login_future.result(timeout=timeout)
        if isinstance(result, Exception):
            raise result

    # ══════════════════════════════════════════════════════════════════════════
    # CTP Callbacks
    # ══════════════════════════════════════════════════════════════════════════

    def OnFrontConnected(self) -> None:
        logger.info("Market data front connected. Sending login request.")
        # Market data channel does not check userid/password
        req = mdapi.CThostFtdcReqUserLoginField()
        self.api.ReqUserLogin(req, 0)

    def OnFrontDisconnected(self, nReason: int) -> None:
        logger.error(f"Market data front disconnected. Reason: {nReason}")
        lf = self._login_future
        if lf and not lf.done():
            lf.set_exception(
                CTPError(-1, f"Market data front disconnected (reason={nReason})")
            )

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID: int, bIsLast: bool):
        if pRspInfo is not None and pRspInfo.ErrorID != 0:
            logger.error(f"Market data login failed: {pRspInfo.ErrorMsg}")
            lf = self._login_future
            if lf and not lf.done():
                lf.set_exception(CTPError(pRspInfo.ErrorID, pRspInfo.ErrorMsg))
            return

        trading_day = pRspUserLogin.TradingDay if pRspUserLogin else "?"
        logger.info(f"Market data login succeeded. TradingDay={trading_day}")
        # NOTE: Resolve immediately on the first successful response, matching
        # md_demo.py behaviour.  Do NOT wait for bIsLast — some CTP builds
        # deliver the single login response with bIsLast=False.
        lf = self._login_future
        if lf and not lf.done():
            lf.set_result(True)

    def OnRtnDepthMarketData(self, pDepthMarketData) -> None:
        """Push callback: a new market data tick arrived."""
        # Copy immediately — CTP reuses the underlying C memory on the next
        # tick, so storing the raw swig wrapper would corrupt cached data.
        snapshot = _copy_ctp_struct(pDepthMarketData)
        instrument_id = snapshot.InstrumentID
        with self._cache_lock:
            self._cache[instrument_id] = snapshot

        # Resolve any pending subscription Future for this instrument
        with self._pending_lock:
            future = self._pending_subs.pop(instrument_id, None)
        if future and not future.done():
            future.set_result(True)

    def OnRspSubMarketData(
        self, pSpecificInstrument, pRspInfo, nRequestID: int, bIsLast: bool
    ):
        """Response to SubscribeMarketData — confirms subscription request."""
        instrument_id = (
            pSpecificInstrument.InstrumentID if pSpecificInstrument else "unknown"
        )
        if pRspInfo is not None and pRspInfo.ErrorID != 0:
            logger.error(
                f"Subscribe failed for {instrument_id}: {pRspInfo.ErrorMsg}"
            )
            with self._pending_lock:
                future = self._pending_subs.pop(instrument_id, None)
            if future and not future.done():
                future.set_exception(
                    CTPError(pRspInfo.ErrorID, pRspInfo.ErrorMsg)
                )
        else:
            if bIsLast:
                logger.info(f"Subscribe confirmed: {instrument_id}")

    # ══════════════════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════════════════

    def get_cached(self, instrument_id: str):
        """Return the latest market data for an instrument, or None."""
        with self._cache_lock:
            return self._cache.get(instrument_id)

    def subscribe(self, instrument_ids: list[str]) -> None:
        """
        Synchronous subscribe call.  BLOCKING — must be called via run_in_executor.

        CTP's SubscribeMarketData is a blocking network call.
        Instrument IDs must be UTF-8 encoded as shown in md_demo.py.
        """
        if not instrument_ids:
            return
        encoded = [iid.encode("utf-8") for iid in instrument_ids]
        self.api.SubscribeMarketData(encoded, len(encoded))

    async def ensure_subscribed(
        self, instrument_id: str, timeout: float = 8.0
    ) -> None:
        """
        Ensure an instrument is subscribed, waiting for the first market data tick.

        Called from async FastAPI route handlers.  If the instrument is already
        cached, returns immediately.  Otherwise initiates a subscription via
        run_in_executor and awaits the first OnRtnDepthMarketData callback.

        Concurrent requests for the same instrument share a single subscription.
        """
        # Fast path: already cached
        with self._cache_lock:
            if instrument_id in self._cache:
                return

        # Check / create pending subscription Future
        with self._pending_lock:
            if instrument_id in self._pending_subs:
                # Another request already initiated subscription — share its Future
                future = self._pending_subs[instrument_id]
            else:
                future = Future()
                self._pending_subs[instrument_id] = future

        # Initiate subscription in a thread to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.subscribe, [instrument_id])

        # Wait for the first tick to populate the cache
        try:
            await asyncio.wait_for(
                asyncio.wrap_future(future), timeout=timeout
            )
        except asyncio.TimeoutError:
            with self._pending_lock:
                self._pending_subs.pop(instrument_id, None)
            raise
