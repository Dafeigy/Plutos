"""GET /positions — query CTP investor positions."""

import asyncio
import logging
from fastapi import APIRouter, Query, Request, HTTPException

from ..models import PositionResponse
from ..ctp.bridge import CTPError

logger = logging.getLogger(__name__)
router = APIRouter()

# CTP PosiDirection constants
_THOST_FTDC_PD_LONG = "2"
_THOST_FTDC_PD_SHORT = "3"


def _map_direction(posi_direction: str) -> str:
    """Map CTP PosiDirection char to readable label."""
    if posi_direction == _THOST_FTDC_PD_LONG:
        return "long"
    if posi_direction == _THOST_FTDC_PD_SHORT:
        return "short"
    return posi_direction


@router.get("/positions", response_model=list[PositionResponse])
async def get_positions(
    request: Request,
    instrument_id: str = Query(
        default="",
        description="Filter by instrument ID (empty = all positions)",
    ),
):
    trader = request.app.state.trader_client

    try:
        future = trader.query_position(instrument_id)
        records = await asyncio.wrap_future(future)
    except TimeoutError:
        raise HTTPException(
            status_code=408, detail="Position query timed out"
        )
    except CTPError as e:
        raise HTTPException(
            status_code=500,
            detail={"error_id": e.error_id, "error_msg": e.error_msg},
        )

    if not records:
        return []

    return [
        PositionResponse(
            instrument_id=pos.InstrumentID,
            exchange_id=getattr(pos, "ExchangeID", ""),
            direction=_map_direction(pos.PosiDirection),
            position=pos.Position,
            yd_position=pos.YdPosition,
            today_position=pos.TodayPosition,
            available=(
                pos.Position - pos.ShortFrozen
                if pos.PosiDirection == _THOST_FTDC_PD_SHORT
                else pos.Position - pos.LongFrozen
            ),
            long_frozen=getattr(pos, "LongFrozen", 0),
            short_frozen=getattr(pos, "ShortFrozen", 0),
            use_margin=pos.UseMargin,
            position_cost=getattr(pos, "PositionCost", 0.0),
            open_cost=getattr(pos, "OpenCost", 0.0),
            settlement_price=getattr(pos, "SettlementPrice", 0.0),
            close_profit=getattr(pos, "CloseProfit", 0.0),
            commission=getattr(pos, "Commission", 0.0),
        )
        for pos in records
        if pos.Position != 0  # only return non-zero positions
    ]
