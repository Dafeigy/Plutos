"""POST /order — submit a futures order (buy/sell, open/close)."""

import asyncio
import logging
import re
from fastapi import APIRouter, Request, HTTPException

from ..models import OrderRequest, OrderResponse, OrderLookupRequest, OrderDetailResponse, TradeItem
from ..ctp.bridge import CTPError

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Exchange inference ──────────────────────────────────────────────────────
# CTP requires ExchangeID for OrderInsert, but the user only provides
# instrument_id.  We infer the exchange from well-known Chinese futures
# instrument prefix conventions.

_EXCHANGE_BY_PREFIX: list[tuple[str, str]] = [
    # CFFEX (China Financial Futures Exchange) — stock index / bonds
    ("IF", "CFFEX"), ("IC", "CFFEX"), ("IH", "CFFEX"), ("IM", "CFFEX"),
    ("T", "CFFEX"), ("TF", "CFFEX"), ("TS", "CFFEX"), ("TL", "CFFEX"),
    # SHFE (Shanghai Futures Exchange) — metals, rubber, etc.
    ("CU", "SHFE"), ("AL", "SHFE"), ("ZN", "SHFE"), ("PB", "SHFE"),
    ("NI", "SHFE"), ("SN", "SHFE"), ("AU", "SHFE"), ("AG", "SHFE"),
    ("RB", "SHFE"), ("WR", "SHFE"), ("HC", "SHFE"), ("SS", "SHFE"),
    ("BU", "SHFE"), ("RU", "SHFE"), ("SP", "SHFE"), ("AO", "SHFE"),
    ("BR", "SHFE"), ("FU", "SHFE"),
    # INE (Shanghai International Energy Exchange)
    ("SC", "INE"), ("LU", "INE"), ("BC", "INE"), ("NR", "INE"),
    # DCE (Dalian Commodity Exchange) — agri, chemicals, etc.
    ("M", "DCE"), ("Y", "DCE"), ("A", "DCE"), ("B", "DCE"),
    ("P", "DCE"), ("J", "DCE"), ("JM", "DCE"), ("I", "DCE"),
    ("L", "DCE"), ("V", "DCE"), ("PP", "DCE"), ("FB", "DCE"),
    ("BB", "DCE"), ("EG", "DCE"), ("EB", "DCE"), ("PG", "DCE"),
    ("LH", "DCE"), ("JD", "DCE"), ("RR", "DCE"), ("C", "DCE"),
    ("CS", "DCE"),
    # CZCE (Zhengzhou Commodity Exchange) — agri, chemical, etc.
    ("FG", "CZCE"), ("SR", "CZCE"), ("TA", "CZCE"), ("MA", "CZCE"),
    ("CF", "CZCE"), ("CY", "CZCE"), ("RM", "CZCE"), ("OI", "CZCE"),
    ("ZC", "CZCE"), ("WH", "CZCE"), ("PM", "CZCE"), ("JR", "CZCE"),
    ("LR", "CZCE"), ("RI", "CZCE"), ("RS", "CZCE"), ("SF", "CZCE"),
    ("SM", "CZCE"), ("AP", "CZCE"), ("CJ", "CZCE"), ("UR", "CZCE"),
    ("SA", "CZCE"), ("PF", "CZCE"), ("PK", "CZCE"), ("SH", "CZCE"),
    # GFEX (Guangzhou Futures Exchange)
    ("SI", "GFEX"), ("LC", "GFEX"),
]


def _infer_exchange(instrument_id: str) -> str:
    """Best-effort exchange inference from instrument ID prefix. Returns '' if unknown."""
    upper = instrument_id.upper()
    # Sort by prefix length descending so longer prefixes match first
    # (e.g., "JM" matches before "J" for coking coal)
    sorted_prefixes = sorted(_EXCHANGE_BY_PREFIX, key=lambda x: len(x[0]), reverse=True)
    for prefix, exchange in sorted_prefixes:
        if re.match(rf"^{prefix}\d+$", upper):
            return exchange
    return ""


# ── Route ────────────────────────────────────────────────────────────────────

@router.post("/order", response_model=OrderResponse)
async def place_order(order: OrderRequest, request: Request):
    trader = request.app.state.trader_client

    exchange_id = _infer_exchange(order.instrument_id)

    try:
        future = trader.insert_order(
            exchange_id=exchange_id,
            instrument_id=order.instrument_id,
            direction=order.direction,
            offset_flag=order.offset_flag,
            price=order.price,
            volume=order.volume,
        )
        result = await asyncio.wrap_future(future)
    except TimeoutError:
        raise HTTPException(
            status_code=408, detail="Order insertion timed out"
        )
    except CTPError as e:
        raise HTTPException(
            status_code=500,
            detail={"error_id": e.error_id, "error_msg": e.error_msg},
        )

    # result is either a CThostFtdcInputOrderField (OnRspOrderInsert path)
    # or a CThostFtdcOrderField (OnRtnOrder push path).  Both are normalised
    # to SimpleNamespace with a unified OrderSubmitStatus field by the client.
    p = result

    # Map direction enum back to string
    direction_str = "buy" if p.Direction == "0" else "sell"

    # Map order status.  Values come from either:
    #   OrderSubmitStatus (InputOrderField)  or
    #   OrderStatus        (OrderField, aliased to OrderSubmitStatus above)
    status_map = {
        "0": "全部成交",
        "1": "部分成交",
        "2": "未成交",
        "3": "未成交队列中",
        "5": "已撤单",
        "a": "报单已提交",  # broker acknowledgment (OnRtnOrder first push)
    }
    order_status = status_map.get(str(p.OrderSubmitStatus), "未知")

    return OrderResponse(
        order_ref=p.OrderRef,
        order_sys_id=getattr(p, "OrderSysID", ""),
        instrument_id=p.InstrumentID,
        direction=direction_str,
        price=p.LimitPrice,
        volume=p.VolumeTotalOriginal,
        order_status=order_status,
        status_msg=getattr(p, "StatusMsg", "委托已提交"),
    )


# ── Shared field mappers ──────────────────────────────────────────────────────

def _map_direction(c: str) -> str:
    """CTP direction enum → human label."""
    return "buy" if c == "0" else "sell"


def _map_offset(c: str) -> str:
    """CTP offset flag enum → human label."""
    return {"0": "open", "1": "close", "3": "close_today", "4": "close_yesterday"}.get(
        str(c), str(c)
    )


_ORDER_STATUS_MAP: dict[str, str] = {
    "0": "全部成交",
    "1": "部分成交",
    "2": "未成交",
    "3": "未成交队列中",
    "5": "已撤单",
    "a": "报单已提交",
}


def _map_status(c) -> str:
    return _ORDER_STATUS_MAP.get(str(c), "未知")


def _build_detail(order_data: dict, trades: list[dict]) -> OrderDetailResponse:
    """Build OrderDetailResponse from cached (or fallback-queried) dict data."""
    o = order_data

    # Direction may come as raw CTP char or already-mapped string
    direction_raw = str(o.get("Direction", "0"))
    direction = _map_direction(direction_raw)

    offset_flag = _map_offset(str(o.get("CombOffsetFlag", "0")))

    # Status: prefer OrderSubmitStatus (set by our normalisation), fall back to
    # OrderStatus.  OnRtnOrder snapshots have both fields aliased.
    raw_status = o.get("OrderSubmitStatus", o.get("OrderStatus", ""))
    status = _map_status(raw_status)

    trade_items = [
        TradeItem(
            trade_id=str(t.get("TradeID", "")),
            price=float(t.get("Price", 0)),
            volume=int(t.get("Volume", 0)),
            direction=_map_direction(str(t.get("Direction", "0"))),
            offset_flag=_map_offset(str(t.get("OffsetFlag", "0"))),
            trade_time=str(t.get("TradeTime", "")),
        )
        for t in trades
    ]

    return OrderDetailResponse(
        order_ref=str(o.get("OrderRef", "")),
        order_sys_id=str(o.get("OrderSysID", "")),
        instrument_id=str(o.get("InstrumentID", "")),
        exchange_id=str(o.get("ExchangeID", "")),
        direction=direction,
        offset_flag=offset_flag,
        price=float(o.get("LimitPrice", 0)),
        volume_original=int(o.get("VolumeTotalOriginal", 0)),
        volume_traded=int(o.get("VolumeTraded", 0)),
        order_status=status,
        status_msg=str(o.get("StatusMsg", "")),
        insert_time=str(o.get("InsertTime", "")),
        update_time=str(o.get("UpdateTime", "")),
        cancel_time=str(o.get("CancelTime", "")),
        trades=trade_items,
    )


# ── Order detail ──────────────────────────────────────────────────────────────

async def _fallback_lookup(request: Request, key: str, *, by: str = "order_ref"):
    """
    Query CTP for all orders/trades and filter by *key*.

    *by* must be ``"order_ref"`` or ``"sysid"`` — controls which field is
    matched on the returned CThostFtdcOrderField records.  Trades are matched
    by OrderRef regardless (CTP's CThostFtdcTradeField carries OrderRef, not
    OrderSysID).

    Returns ``(order_dict, trades_list)`` or raises HTTPException.
    """
    trader = request.app.state.trader_client

    try:
        orders_future = trader.query_order("")
        orders = await asyncio.wrap_future(orders_future)
    except TimeoutError:
        raise HTTPException(status_code=408, detail="Order query timed out")
    except CTPError as e:
        raise HTTPException(
            status_code=500,
            detail={"error_id": e.error_id, "error_msg": e.error_msg},
        )

    match: dict | None = None
    for o in orders:
        if by == "sysid":
            if str(getattr(o, "OrderSysID", "")) == key:
                match = vars(o)
                break
        else:
            if str(o.OrderRef) == key:
                match = vars(o)
                break

    if match is None:
        raise HTTPException(
            status_code=404, detail=f"Order '{key}' not found"
        )

    order_ref = match.get("OrderRef", "")
    trades: list[dict] = []
    try:
        trades_future = trader.query_trade("")
        all_trades = await asyncio.wrap_future(trades_future)
        trades = [
            vars(t) for t in all_trades if str(t.OrderRef) == order_ref
        ]
    except (TimeoutError, CTPError):
        logger.warning(
            f"Trade query failed for order {key}, returning without trades"
        )

    return match, trades


@router.get("/order/{order_ref}", response_model=OrderDetailResponse)
async def get_order(order_ref: str, request: Request):
    """
    Return the latest state for a previously-submitted order, keyed by
    OrderRef (session-scoped, available immediately on submission).

    Reads from the push-driven memory cache first.  Falls back to a CTP
    query if the order is not cached (e.g. after a service restart).

    Prefer ``POST /order/lookup`` for cross-session lookups
    — OrderSysID is exchange-assigned and globally unique.
    """
    cache = request.app.state.order_cache

    # 1. Check cache
    cached = cache.get(order_ref)
    if cached is not None:
        trades = cache.get_trades(order_ref)
        return _build_detail(cached, trades)

    # 2. Fallback: query CTP
    logger.info(f"OrderRef={order_ref} not in cache, querying CTP...")
    match, trades = await _fallback_lookup(request, order_ref, by="order_ref")

    # Populate cache so subsequent GETs hit the fast path
    cache.put(order_ref, match)
    cache.put_trades(match.get("OrderRef", ""), trades)

    return _build_detail(match, trades)


@router.post("/order/lookup", response_model=OrderDetailResponse)
async def lookup_order(body: OrderLookupRequest, request: Request):
    """
    Return order state keyed by OrderSysID (exchange-assigned, globally unique).

    OrderSysID survives service restarts — use this endpoint for reliable
    cross-session lookups.  It is only available after the exchange accepts
    the order (unlike OrderRef which is available immediately).

    The ID is passed in the request body because exchange-assigned identifiers
    may contain whitespace, which is fragile in URL path segments.
    """
    sysid = body.order_sys_id
    cache = request.app.state.order_cache

    # 1. Check cache via sysid index
    cached = cache.get_by_sysid(sysid)
    if cached is not None:
        trades = cache.get_trades_by_sysid(sysid)
        return _build_detail(cached, trades)

    # 2. Fallback: query CTP
    logger.info(f"OrderSysID={sysid} not in cache, querying CTP...")
    match, trades = await _fallback_lookup(request, sysid, by="sysid")

    # Populate cache under both keys
    order_ref = match.get("OrderRef", "")
    cache.put(order_ref, match)
    cache.put_trades(order_ref, trades)

    return _build_detail(match, trades)
