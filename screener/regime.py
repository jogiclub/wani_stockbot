"""시장 국면(regime) 필터.

지수가 이동평균 위에 있을 때만 매매하는 단순 스위치. 개별 종목 신호가 아무리 좋아도
시장 전체가 무너지는 국면에서는 돌파가 실패하는 경향이 있어, 그 구간을 통째로 건너뛴다.
"""

from __future__ import annotations

import logging

import pandas as pd

from .data_loader import DataLoader

log = logging.getLogger(__name__)


class MarketRegime:
    """지수 종가 > N일 이동평균 이면 '매매 허용' 국면으로 본다."""

    def __init__(self, loader: DataLoader, symbol: str = "KOSDAQ", ma: int = 20,
                 bars: int = 800):
        self.symbol = symbol
        self.ma = ma
        df = loader.index_ohlcv(symbol, count=bars)
        if df.empty:
            log.warning("지수 %s 데이터를 받지 못했습니다 — 국면 필터를 통과 처리합니다", symbol)
            self.series = pd.Series(dtype=bool)
            self.index = pd.DataFrame()
            return
        ma_series = df["close"].rolling(ma, min_periods=ma).mean()
        self.series = (df["close"] > ma_series).dropna()
        self.index = df

    @property
    def available(self) -> bool:
        return not self.series.empty

    def is_on(self, when=None) -> bool:
        """해당 일자(미지정 시 최신)의 국면. 데이터가 없으면 True(통과)."""
        if not self.available:
            return True
        if when is None:
            return bool(self.series.iloc[-1])
        ts = pd.Timestamp(when).normalize()
        past = self.series.loc[:ts]
        return bool(past.iloc[-1]) if len(past) else True

    def status(self) -> dict:
        """대시보드 표시용 현재 국면 요약."""
        if not self.available:
            return {"available": False}
        last = self.index.iloc[-1]
        ma_val = float(self.index["close"].rolling(self.ma).mean().iloc[-1])
        on = self.is_on()
        return {
            "available": True,
            "symbol": self.symbol,
            "ma": self.ma,
            "close": round(float(last["close"]), 2),
            "ma_value": round(ma_val, 2),
            "on": on,
            "gap_pct": round((float(last["close"]) / ma_val - 1) * 100, 2),
            "date": self.index.index[-1].strftime("%Y-%m-%d"),
        }

    def filter_dates(self, dates: pd.Series) -> pd.Series:
        """진입일 시리즈에 대해 국면 통과 여부 불리언 시리즈를 만든다 (백테스트용)."""
        if not self.available:
            return pd.Series(True, index=dates.index)
        aligned = self.series.reindex(
            pd.DatetimeIndex(pd.to_datetime(dates).dt.normalize())
        ).ffill()
        return pd.Series(aligned.fillna(True).to_numpy().astype(bool), index=dates.index)
