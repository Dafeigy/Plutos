"""
TraderClient — wraps CTP's TraderApi to provide Future-based query/order methods.

The login sequence follows the exact flow from td_demo.py:
  OnFrontConnected → ReqAuthenticate → OnRspAuthenticate → ReqUserLogin →
  OnRspUserLogin (save session) → ReqSettlementInfoConfirm →
  OnRspSettlementInfoConfirm (resolve login Future)

All query methods return a concurrent.futures.Future that gets resolved in
the corresponding CTP callback.  Route handlers use asyncio.wrap_future().
"""

import logging
import threading
from concurrent.futures import Future

from openctp_ctp import thosttraderapi as tdapi

from .bridge import CTPError, FutureStore, TraderState

logger = logging.getLogger(__name__)


class TraderClient(tdapi.CThostFtdcTraderSpi):
    """
    CTP trader SPI implementation.

    Each instance maintains its own CTP API connection and session state.
    Two FutureStore instances separate query timeouts (configurable, default 10s)
    from order timeouts (15s).
    """

    def __init__(
        self,
        settings,
        query_store: FutureStore,
        order_store: FutureStore,
    ):
        super().__init__()

        self._settings = settings
        self._query_store = query_store
        self._order_store = order_store
        self._state = TraderState()

        self._request_counter = 0
        self._counter_lock = threading.Lock()
        self._state_lock = threading.Lock()

        self._login_future: Future | None = None

        # Create and configure the CTP API instance
        self.api: tdapi.CThostFtdcTraderApi = (
            tdapi.CThostFtdcTraderApi.CreateFtdcTraderApi()
        )
        self.api.RegisterSpi(self)
        self.api.RegisterFront(settings.TRADE_FRONT)
        self.api.SubscribePrivateTopic(tdapi.THOST_TERT_QUICK)
        self.api.SubscribePublicTopic(tdapi.THOST_TERT_QUICK)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def init(self) -> None:
        """Initiate connection to CTP (blocking call, returns when connected)."""
        self.api.Init()

    def release(self) -> None:
        """Release CTP resources."""
        self.api.RegisterSpi(None)
        self.api.Release()

    def await_login(self, timeout: float = 30.0) -> None:
        """
        Block until the full login sequence completes, or raise on failure.

        Must be called after init().  The CTP login sequence is:
        authenticate → login → settlement confirm.
        """
        self._login_future = Future()
        result = self._login_future.result(timeout=timeout)
        if isinstance(result, Exception):
            raise result

    # ── Request ID allocation ────────────────────────────────────────────────

    def _next_request_id(self) -> int:
        with self._counter_lock:
            self._request_counter += 1
            return self._request_counter

    # ══════════════════════════════════════════════════════════════════════════
    # CTP Callbacks — Login Sequence
    # ══════════════════════════════════════════════════════════════════════════

    def OnFrontConnected(self) -> None:
        logger.info("Trader front connected. Sending authenticate request.")
        req = tdapi.CThostFtdcReqAuthenticateField()
        req.BrokerID = self._settings.BROKER_ID
        req.UserID = self._settings.USER_ID
        req.AppID = self._settings.APP_ID
        req.AuthCode = self._settings.AUTH_CODE
        self.api.ReqAuthenticate(req, 0)

    def OnFrontDisconnected(self, nReason: int) -> None:
        logger.error(f"Trader front disconnected. Reason: {nReason}")
        # On disconnect, reject the login future if still pending
        lf = self._login_future
        if lf and not lf.done():
            lf.set_exception(
                CTPError(-1, f"Trader front disconnected (reason={nReason})")
            )

    def OnRspAuthenticate(
        self,
        pRspAuthenticateField,
        pRspInfo,
        nRequestID: int,
        bIsLast: bool,
    ):
        if pRspInfo is not None and pRspInfo.ErrorID != 0:
            logger.error(f"Authentication failed: {pRspInfo.ErrorMsg}")
            lf = self._login_future
            if lf and not lf.done():
                lf.set_exception(CTPError(pRspInfo.ErrorID, pRspInfo.ErrorMsg))
            return

        logger.info("Authenticate succeeded. Sending login request.")
        req = tdapi.CThostFtdcReqUserLoginField()
        req.BrokerID = self._settings.BROKER_ID
        req.UserID = self._settings.USER_ID
        req.Password = self._settings.PASSWORD
        req.UserProductInfo = "plutos"
        self.api.ReqUserLogin(req, 0)

    def OnRspUserLogin(
        self,
        pRspUserLogin,
        pRspInfo,
        nRequestID: int,
        bIsLast: bool,
    ):
        if pRspInfo is not None and pRspInfo.ErrorID != 0:
            logger.error(f"Login failed: {pRspInfo.ErrorMsg}")
            lf = self._login_future
            if lf and not lf.done():
                lf.set_exception(CTPError(pRspInfo.ErrorID, pRspInfo.ErrorMsg))
            return

        logger.info(
            f"Login succeeded. TradingDay={pRspUserLogin.TradingDay}"
        )
        with self._state_lock:
            self._state.front_id = pRspUserLogin.FrontID
            self._state.session_id = pRspUserLogin.SessionID
            self._state.trading_day = pRspUserLogin.TradingDay
            self._state.order_ref = 1
            self._state.logged_in = True

        if bIsLast:
            # Proceed to settlement info confirmation
            self._confirm_settlement()

    def _confirm_settlement(self) -> None:
        req = tdapi.CThostFtdcSettlementInfoConfirmField()
        req.BrokerID = self._settings.BROKER_ID
        req.InvestorID = self._settings.USER_ID
        self.api.ReqSettlementInfoConfirm(req, 0)

    def OnRspSettlementInfoConfirm(
        self,
        pSettlementInfoConfirm,
        pRspInfo,
        nRequestID: int,
        bIsLast: bool,
    ):
        if pRspInfo is not None and pRspInfo.ErrorID != 0:
            logger.error(f"Settlement confirm failed: {pRspInfo.ErrorMsg}")
            lf = self._login_future
            if lf and not lf.done():
                lf.set_exception(CTPError(pRspInfo.ErrorID, pRspInfo.ErrorMsg))
            return

        logger.info("Settlement info confirmed.")
        if bIsLast:
            lf = self._login_future
            if lf and not lf.done():
                lf.set_result(True)

    # ══════════════════════════════════════════════════════════════════════════
    # CTP Callbacks — Generic Query Handler
    # ══════════════════════════════════════════════════════════════════════════

    def _on_query_callback(self, item, pRspInfo, nRequestID: int, bIsLast: bool):
        """Shared handler for all OnRspQry* callbacks."""
        if pRspInfo is not None and pRspInfo.ErrorID != 0:
            logger.error(f"Query {nRequestID} failed: {pRspInfo.ErrorMsg}")
            self._query_store.reject(nRequestID, pRspInfo.ErrorID, pRspInfo.ErrorMsg)
            return
        if item is not None:
            self._query_store.accumulate(nRequestID, item)
        if bIsLast:
            self._query_store.resolve_with_accumulator(nRequestID)

    # ══════════════════════════════════════════════════════════════════════════
    # CTP Callbacks — Query Responses
    # ══════════════════════════════════════════════════════════════════════════

    def OnRspQryTradingAccount(self, pTradingAccount, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pTradingAccount, pRspInfo, nRequestID, bIsLast)

    def OnRspQryInvestorPosition(self, pInvestorPosition, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pInvestorPosition, pRspInfo, nRequestID, bIsLast)

    def OnRspQryInvestorPositionDetail(self, pInvestorPositionDetail, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pInvestorPositionDetail, pRspInfo, nRequestID, bIsLast)

    def OnRspQryOrder(self, pOrder, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pOrder, pRspInfo, nRequestID, bIsLast)

    def OnRspQryTrade(self, pTrade, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pTrade, pRspInfo, nRequestID, bIsLast)

    def OnRspQryInstrument(self, pInstrument, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pInstrument, pRspInfo, nRequestID, bIsLast)

    def OnRspQryExchange(self, pExchange, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pExchange, pRspInfo, nRequestID, bIsLast)

    def OnRspQryProduct(self, pProduct, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pProduct, pRspInfo, nRequestID, bIsLast)

    def OnRspQryDepthMarketData(self, pDepthMarketData, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pDepthMarketData, pRspInfo, nRequestID, bIsLast)

    def OnRspQryInvestor(self, pInvestor, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pInvestor, pRspInfo, nRequestID, bIsLast)

    def OnRspQryInstrumentCommissionRate(self, pInstrumentCommissionRate, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pInstrumentCommissionRate, pRspInfo, nRequestID, bIsLast)

    def OnRspQryInstrumentMarginRate(self, pInstrumentMarginRate, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pInstrumentMarginRate, pRspInfo, nRequestID, bIsLast)

    def OnRspQryInstrumentOrderCommRate(self, pInstrumentOrderCommRate, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pInstrumentOrderCommRate, pRspInfo, nRequestID, bIsLast)

    def OnRspQryTradingCode(self, pTradingCode, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pTradingCode, pRspInfo, nRequestID, bIsLast)

    def OnRspQrySettlementInfo(self, pSettlementInfo, pRspInfo, nRequestID, bIsLast):
        self._on_query_callback(pSettlementInfo, pRspInfo, nRequestID, bIsLast)

    # ══════════════════════════════════════════════════════════════════════════
    # CTP Callbacks — Order Responses & Push Notifications
    # ══════════════════════════════════════════════════════════════════════════

    def OnRspOrderInsert(self, pInputOrder, pRspInfo, nRequestID, bIsLast):
        """Response to ReqOrderInsert — confirms acceptance / immediate rejection."""
        if pRspInfo is not None and pRspInfo.ErrorID != 0:
            logger.error(f"Order insert failed: {pRspInfo.ErrorMsg}")
            self._order_store.reject(nRequestID, pRspInfo.ErrorID, pRspInfo.ErrorMsg)
            return
        if pInputOrder is not None and bIsLast:
            self._order_store.resolve_direct(nRequestID, pInputOrder)

    def OnErrRtnOrderInsert(self, pInputOrder, pRspInfo):
        """
        Asynchronous order insert error (CTP push, not tied to nRequestID).
        We attempt to map by OrderRef to reject the pending Future.
        """
        if pRspInfo is not None and pRspInfo.ErrorID != 0:
            logger.error(f"OnErrRtnOrderInsert: {pRspInfo.ErrorMsg}")
        if pInputOrder is not None:
            found = self._order_store.reject_by_order_ref(
                pInputOrder.OrderRef, pRspInfo.ErrorID, pRspInfo.ErrorMsg
            )
            if not found:
                logger.warning(
                    f"OnErrRtnOrderInsert for unknown OrderRef={pInputOrder.OrderRef}"
                )

    def OnRspOrderAction(self, pInputOrderAction, pRspInfo, nRequestID, bIsLast):
        """Response to ReqOrderAction (cancel)."""
        if pRspInfo is not None and pRspInfo.ErrorID != 0:
            logger.error(f"Order action failed: {pRspInfo.ErrorMsg}")
            self._order_store.reject(nRequestID, pRspInfo.ErrorID, pRspInfo.ErrorMsg)
            return
        if pInputOrderAction is not None and bIsLast:
            self._order_store.resolve_direct(nRequestID, pInputOrderAction)

    def OnErrRtnOrderAction(self, pOrderAction, pRspInfo):
        """Asynchronous order action error."""
        if pRspInfo is not None and pRspInfo.ErrorID != 0:
            logger.error(f"OnErrRtnOrderAction: {pRspInfo.ErrorMsg}")

    def OnRtnOrder(self, pOrder):
        """Push notification: order status update (trade confirmation, cancel, etc.)."""
        logger.info(
            f"OnRtnOrder: InstrumentID={pOrder.InstrumentID} "
            f"OrderRef={pOrder.OrderRef} OrderSysID={pOrder.OrderSysID} "
            f"OrderStatus={pOrder.OrderStatus} StatusMsg={pOrder.StatusMsg}"
        )

    def OnRtnTrade(self, pTrade):
        """Push notification: trade fill."""
        logger.info(
            f"OnRtnTrade: InstrumentID={pTrade.InstrumentID} "
            f"Price={pTrade.Price} Volume={pTrade.Volume} "
            f"OrderRef={pTrade.OrderRef}"
        )

    def OnRtnInstrumentStatus(self, pInstrumentStatus):
        """Push notification: instrument trading status change."""
        logger.info(
            f"OnRtnInstrumentStatus: ExchangeID={pInstrumentStatus.ExchangeID} "
            f"InstrumentID={pInstrumentStatus.InstrumentID} "
            f"InstrumentStatus={pInstrumentStatus.InstrumentStatus}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Public API — Query Methods
    # ══════════════════════════════════════════════════════════════════════════

    def _make_query_request(self, req, request_id: int) -> Future:
        """Helper: create Future, call the CTP Req* method, return Future."""
        future = self._query_store.create(request_id)
        return future

    def query_account(self) -> Future:
        rid = self._next_request_id()
        future = self._query_store.create(rid)
        req = tdapi.CThostFtdcQryTradingAccountField()
        req.BrokerID = self._settings.BROKER_ID
        req.InvestorID = self._settings.USER_ID
        self.api.ReqQryTradingAccount(req, rid)
        return future

    def query_position(self, instrument_id: str = "") -> Future:
        rid = self._next_request_id()
        future = self._query_store.create(rid)
        req = tdapi.CThostFtdcQryInvestorPositionField()
        req.BrokerID = self._settings.BROKER_ID
        req.InvestorID = self._settings.USER_ID
        req.InstrumentID = instrument_id
        self.api.ReqQryInvestorPosition(req, rid)
        return future

    def query_order(self, instrument_id: str = "") -> Future:
        rid = self._next_request_id()
        future = self._query_store.create(rid)
        req = tdapi.CThostFtdcQryOrderField()
        req.BrokerID = self._settings.BROKER_ID
        req.InvestorID = self._settings.USER_ID
        req.InstrumentID = instrument_id
        self.api.ReqQryOrder(req, rid)
        return future

    def query_trade(self, instrument_id: str = "") -> Future:
        rid = self._next_request_id()
        future = self._query_store.create(rid)
        req = tdapi.CThostFtdcQryTradeField()
        req.BrokerID = self._settings.BROKER_ID
        req.InvestorID = self._settings.USER_ID
        req.InstrumentID = instrument_id
        self.api.ReqQryTrade(req, rid)
        return future

    # ══════════════════════════════════════════════════════════════════════════
    # Public API — Order Methods
    # ══════════════════════════════════════════════════════════════════════════

    def insert_order(
        self,
        exchange_id: str,
        instrument_id: str,
        direction: str,
        offset_flag: str,
        price: float,
        volume: int,
    ) -> Future:
        """
        Submit an order.  Returns a Future resolved with the CThostFtdcInputOrderField
        from OnRspOrderInsert, or rejected with CTPError / TimeoutError.

        direction: "buy" or "sell"
        offset_flag: "open", "close", or "close_today"
        """
        rid = self._next_request_id()

        # Build the CTP request struct
        req = tdapi.CThostFtdcInputOrderField()
        req.BrokerID = self._settings.BROKER_ID
        req.UserID = self._settings.USER_ID
        req.InvestorID = self._settings.USER_ID
        req.ExchangeID = exchange_id
        req.InstrumentID = instrument_id

        # Direction
        if direction == "buy":
            req.Direction = tdapi.THOST_FTDC_D_Buy
        else:
            req.Direction = tdapi.THOST_FTDC_D_Sell

        # Offset flag
        offset_map = {
            "open": tdapi.THOST_FTDC_OF_Open,
            "close": tdapi.THOST_FTDC_OF_Close,
            "close_today": tdapi.THOST_FTDC_OF_CloseToday,
        }
        req.CombOffsetFlag = offset_map[offset_flag]

        req.CombHedgeFlag = tdapi.THOST_FTDC_HF_Speculation
        req.OrderPriceType = tdapi.THOST_FTDC_OPT_LimitPrice
        req.LimitPrice = price
        req.VolumeTotalOriginal = volume
        req.TimeCondition = tdapi.THOST_FTDC_TC_GFD
        req.VolumeCondition = tdapi.THOST_FTDC_VC_AV
        req.MinVolume = 1
        req.ForceCloseReason = tdapi.THOST_FTDC_FCC_NotForceClose
        req.ContingentCondition = tdapi.THOST_FTDC_CC_Immediately

        # Atomic OrderRef allocation
        with self._state_lock:
            order_ref = str(self._state.order_ref)
            self._state.order_ref += 1
        req.OrderRef = order_ref

        # Register Future indexed by both request_id and OrderRef
        future = self._order_store.create_with_order_ref(rid, order_ref, timeout=15.0)

        self.api.ReqOrderInsert(req, rid)
        return future
