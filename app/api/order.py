"""POST /order — submit a futures order (buy/sell, open/close)."""

import asyncio
import logging
import re
from fastapi import APIRouter, Request, HTTPException

from ..models import OrderRequest, OrderResponse
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

    # result is CThostFtdcInputOrderField from OnRspOrderInsert callback
    p = result

    # Map direction enum back to string
    direction_str = "buy" if p.Direction == "0" else "sell"

    # Map order status (may be 0 for newly submitted)
    status_map = {
        "0": "全部成交",
        "1": "部分成交",
        "2": "未成交",
        "3": "未成交队列中",
        "5": "已撤单",
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
