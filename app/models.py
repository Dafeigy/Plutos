"""Pydantic request/response models for the Plutos API."""

from typing import Literal
from pydantic import BaseModel, Field


# ── Market Data ──────────────────────────────────────────────────────────────

class PriceResponse(BaseModel):
    instrument_id: str
    last_price: float
    bid_price1: float
    ask_price1: float
    bid_volume1: int
    ask_volume1: int
    volume: int
    open_interest: float
    update_time: str
    update_millisec: int


# ── Account Balance ──────────────────────────────────────────────────────────

class BalanceResponse(BaseModel):
    user_id: str
    balance: float
    available: float
    frozen_margin: float
    curr_margin: float
    close_profit: float
    position_profit: float
    deposit: float
    withdraw: float


# ── Order ────────────────────────────────────────────────────────────────────

class OrderRequest(BaseModel):
    instrument_id: str
    direction: Literal["buy", "sell"]
    offset_flag: Literal["open", "close", "close_today"] = "open"
    price: float = Field(gt=0, description="Limit price (>0)")
    volume: int = Field(ge=1, description="Order volume (>=1)")


class OrderResponse(BaseModel):
    order_ref: str
    order_sys_id: str
    instrument_id: str
    direction: str
    price: float
    volume: int
    order_status: str
    status_msg: str


# ── Position ──────────────────────────────────────────────────────────────────

class PositionResponse(BaseModel):
    instrument_id: str
    exchange_id: str = ""
    direction: str               # "long" / "short"
    position: int                # total position
    yd_position: int = 0         # yesterday's position
    today_position: int = 0      # today's opened position
    available: int = 0           # position not frozen (open-close available)
    long_frozen: int = 0
    short_frozen: int = 0
    use_margin: float = 0.0
    position_cost: float = 0.0
    open_cost: float = 0.0
    settlement_price: float = 0.0
    close_profit: float = 0.0
    commission: float = 0.0


class TradeItem(BaseModel):
    trade_id: str = ""
    price: float = 0.0
    volume: int = 0
    direction: str = ""
    offset_flag: str = ""
    trade_time: str = ""


class OrderLookupRequest(BaseModel):
    order_sys_id: str = Field(..., min_length=1, description="Exchange-assigned order system ID")


class OrderDetailResponse(BaseModel):
    order_ref: str
    order_sys_id: str
    instrument_id: str
    exchange_id: str = ""
    direction: str
    offset_flag: str = ""
    price: float
    volume_original: int
    volume_traded: int = 0
    order_status: str
    status_msg: str
    insert_time: str = ""
    update_time: str = ""
    cancel_time: str = ""
    trades: list[TradeItem] = []
