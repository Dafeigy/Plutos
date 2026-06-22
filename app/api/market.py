"""GET /market/{instrument_id}/price — get latest price for a futures instrument."""

import asyncio
import logging
from fastapi import APIRouter, Request, HTTPException

from ..models import PriceResponse
from ..ctp.bridge import CTPError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/market/{instrument_id}/price", response_model=PriceResponse)
async def get_price(instrument_id: str, request: Request):
    md_client = request.app.state.md_client

    # Auto-subscribe on first request (no-op if already subscribed)
    try:
        await md_client.ensure_subscribed(instrument_id)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=408,
            detail=f"No market data for {instrument_id} within timeout",
        )
    except CTPError as e:
        raise HTTPException(
            status_code=500,
            detail={"error_id": e.error_id, "error_msg": e.error_msg},
        )

    data = md_client.get_cached(instrument_id)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"No market data for {instrument_id}",
        )

    return PriceResponse(
        instrument_id=data.InstrumentID,
        last_price=data.LastPrice,
        bid_price1=data.BidPrice1,
        ask_price1=data.AskPrice1,
        bid_volume1=data.BidVolume1,
        ask_volume1=data.AskVolume1,
        volume=data.Volume,
        open_interest=data.OpenInterest,
        update_time=data.UpdateTime,
        update_millisec=data.UpdateMillisec,
    )
