"""GET /account/balance — query CTP trading account funds."""

import asyncio
import logging
from fastapi import APIRouter, Request, HTTPException

from ..models import BalanceResponse
from ..ctp.bridge import CTPError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/account/balance", response_model=BalanceResponse)
async def get_balance(request: Request):
    trader = request.app.state.trader_client

    try:
        future = trader.query_account()
        records = await asyncio.wrap_future(future)
    except TimeoutError:
        raise HTTPException(
            status_code=408, detail="Account query timed out"
        )
    except CTPError as e:
        raise HTTPException(
            status_code=500,
            detail={"error_id": e.error_id, "error_msg": e.error_msg},
        )

    if not records:
        raise HTTPException(
            status_code=404, detail="No account data returned"
        )

    # TradingAccount query returns a single record
    acc = records[0]

    return BalanceResponse(
        user_id=trader._settings.USER_ID,
        balance=acc.Balance,
        available=acc.Available,
        frozen_margin=acc.FrozenMargin,
        curr_margin=acc.CurrMargin,
        close_profit=acc.CloseProfit,
        position_profit=acc.PositionProfit,
        deposit=acc.Deposit,
        withdraw=acc.Withdraw,
    )
