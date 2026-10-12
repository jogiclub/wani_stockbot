from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


KRW_100_MILLION = 100_000_000

AllowedSource = Literal["KRX", "FSS", "NAVER", "BROKER"]


class Market(str, Enum):
    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"


class InstrumentType(str, Enum):
    STOCK = "STOCK"
    ETF = "ETF"
    ETN = "ETN"


class Advice(str, Enum):
    BUY_ON_PULLBACK = "\ubd84\ud560\ub9e4\uc218 \uac00\ub2a5"
    WAIT_FOR_BREAKOUT = "\ub3cc\ud30c \ub300\uae30"
    WATCH = "\uad00\ub9dd"
    SELL_CONSIDER = "\ub9e4\ub3c4 \uace0\ub824"
    WARNING = "\uacbd\uace0"
    DO_NOT_BUY = "\uc2e0\uaddc \ub9e4\uc218 \uae08\uc9c0"


class SourceSnapshot(BaseModel):
    source: AllowedSource
    captured_at: datetime
    foreign_net_buy_1d_krw: int = 0
    foreign_net_buy_3d_krw: int = 0
    institution_net_buy_1d_krw: int = 0
    institution_net_buy_3d_krw: int = 0


class StockSnapshot(BaseModel):
    code: str = Field(min_length=6, max_length=12)
    name: str
    market: Market
    instrument_type: InstrumentType = InstrumentType.STOCK
    is_managed: bool = False
    is_halted: bool = False
    foreign_positive_days_last_3: int = Field(ge=0, le=3)
    foreign_positive_streak_last_3: int = Field(ge=0, le=3)
    foreign_net_buy_1d_krw: int
    foreign_net_buy_3d_krw: int
    foreign_sell_streak_days: int = Field(default=0, ge=0)
    institution_positive_days_last_3: int = Field(default=0, ge=0, le=3)
    institution_net_buy_1d_krw: int
    institution_net_buy_3d_krw: int
    institution_rank_percentile: float = Field(ge=0.0, le=1.0)
    institution_turned_to_sell: bool = False
    avg_trading_value_last_3d_krw: int
    avg_trading_value_prev_3d_krw: int = 0
    market_cap_krw: int
    current_price: float = Field(gt=0)
    moving_average_20: float = Field(gt=0)
    highest_price_20d: float = Field(gt=0)
    price_change_5d_pct: float
    volume_spike: bool = False
    source_snapshots: list[SourceSnapshot] = Field(min_length=1)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()


class StockEvaluation(BaseModel):
    code: str
    name: str
    score: int
    selected: bool
    reasons: list[str]
    warnings: list[str]
    advice: Advice
    source_summary: str
    foreign_trend: str
    institution_trend: str
    trading_value_summary: str
    price_position_summary: str


class RecommendationRecord(BaseModel):
    generated_at: datetime
    run_date: str
    candidates: list[StockEvaluation]


class ScreeningRequest(BaseModel):
    run_date: str | None = None
    snapshots: list[StockSnapshot]

    model_config = {
        "json_schema_extra": {
            "example": {
                "run_date": "2026-04-17",
                "snapshots": [
                    {
                        "code": "005930",
                        "name": "Samsung Electronics",
                        "market": "KOSPI",
                        "instrument_type": "STOCK",
                        "is_managed": False,
                        "is_halted": False,
                        "foreign_positive_days_last_3": 3,
                        "foreign_positive_streak_last_3": 3,
                        "foreign_net_buy_1d_krw": 4200000000,
                        "foreign_net_buy_3d_krw": 24000000000,
                        "foreign_sell_streak_days": 0,
                        "institution_positive_days_last_3": 2,
                        "institution_net_buy_1d_krw": 1800000000,
                        "institution_net_buy_3d_krw": 5200000000,
                        "institution_rank_percentile": 0.12,
                        "institution_turned_to_sell": False,
                        "avg_trading_value_last_3d_krw": 78000000000,
                        "avg_trading_value_prev_3d_krw": 60000000000,
                        "market_cap_krw": 430000000000000,
                        "current_price": 84500,
                        "moving_average_20": 83200,
                        "highest_price_20d": 87000,
                        "price_change_5d_pct": 4.2,
                        "volume_spike": False,
                        "source_snapshots": [
                            {
                                "source": "KRX",
                                "captured_at": "2026-04-17T16:05:00+09:00",
                                "foreign_net_buy_1d_krw": 4200000000,
                                "foreign_net_buy_3d_krw": 24000000000,
                                "institution_net_buy_1d_krw": 1800000000,
                                "institution_net_buy_3d_krw": 5200000000,
                            },
                            {
                                "source": "NAVER",
                                "captured_at": "2026-04-17T16:07:00+09:00",
                                "foreign_net_buy_1d_krw": 4200000000,
                                "foreign_net_buy_3d_krw": 24000000000,
                                "institution_net_buy_1d_krw": 1800000000,
                                "institution_net_buy_3d_krw": 5200000000,
                            },
                        ],
                    }
                ],
            }
        }
    }


class OutputItem(BaseModel):
    stock_name: str = Field(serialization_alias="\uc885\ubaa9\uba85")
    foreign_flow: str = Field(serialization_alias="\uc678\uad6d\uc778\ub3d9\ud5a5")
    institution_flow: str = Field(serialization_alias="\uae30\uad00\ub3d9\ud5a5")
    trading_value: str = Field(serialization_alias="\uac70\ub798\ub300\uae08")
    price_position: str = Field(serialization_alias="\ud604\uc7ac\uc704\uce58")
    score: int = Field(serialization_alias="\uc810\uc218")
    opinion: str = Field(serialization_alias="\ub9e4\ub9e4\uc758\uacac")
    data_source: str = Field(serialization_alias="\ub370\uc774\ud130\ucd9c\ucc98")
    reasons: list[str] = Field(serialization_alias="\uc120\uc815\uc774\uc720")
    warnings: list[str] = Field(serialization_alias="\ub9ac\uc2a4\ud06c")


class ScreeningResponse(BaseModel):
    generated_at: datetime
    run_date: str
    items: list[OutputItem]
