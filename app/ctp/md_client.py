"""
MdClient — wraps CTP's MdApi to maintain a thread-safe cache of latest prices.

Market data arrives as push callbacks (OnRtnDepthMarketData) from CTP's
internal thread.  This client stores the latest tick per instrument in a
lock-protected dict and provides async subscribe-on-first-request semantics.
"""

import asyncio
import logging
import threading
from concurrent.futures import Future

from openctp_ctp import thostmduserapi as mdapi

from .bridge import CTPError

logger = logging.getLogger(__name__)


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

        # Thread-safe price cache
        self._cache: dict[str, mdapi.CThostFtdcDepthMarketDataField] = {}
        self._cache_lock = threading.Lock()

        # Pending subscription futures (instrument_id → Future)
        # Deduplicates concurrent subscription requests for the same instrument
        self._pending_subs: dict[str, Future] = {}
        self._pending_lock = threading.Lock()

        self._login_future: Future | None = None

        # Create and configure the CTP MdApi instance
        self.api: mdapi.CThostFtdcMdApi = mdapi.CThostFtdcMdApi.CreateFtdcMdApi()
        self.api.RegisterSpi(self)
        self.api.RegisterFront(settings.MD_FRONT)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def init(self) -> None:
        """Initiate connection to market data front (blocking)."""
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
        self._login_future = Future()
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

        logger.info(f"Market data login succeeded. TradingDay={pRspUserLogin.TradingDay}")
        if bIsLast:
            lf = self._login_future
            if lf and not lf.done():
                lf.set_result(True)

    def OnRtnDepthMarketData(self, pDepthMarketData) -> None:
        """Push callback: a new market data tick arrived."""
        instrument_id = pDepthMarketData.InstrumentID
        with self._cache_lock:
            self._cache[instrument_id] = pDepthMarketData

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
