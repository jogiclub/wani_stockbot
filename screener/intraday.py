"""장중 1분봉 공통 도구 — 정규장 필터·틱 규칙 매수세·VWAP.

수급포착매매(2026-10-06 제거)에서 떼어 낸 것으로, 금요일 마감 돌파(`friday.py`)와 `TossDataLoader` 가 함께 쓴다.
"""

from __future__ import annotations

from datetime import date
from datetime import time as dtime

import numpy as np
import pandas as pd

# 토스 1분봉 시각은 **봉이 끝나는 시각**이다 (09:00~09:01 거래 → `09:01` 봉).
#   - `09:00` 이하 봉은 장전(NXT) 거래라 정규장이 아니다.
#   - 접속매매 마지막 봉은 `15:20`, 종가 동시호가 체결은 종목에 따라 `15:30` 또는 `15:31`
#     봉에 한꺼번에 들어온다 (2026-09-16 실측: 한선엔지니어링 15:30, 삼성전자 15:31).
SESSION_OPEN = dtime(9, 0)            # 이 시각 **초과**부터 정규장
SESSION_CLOSE = dtime(15, 31)         # 이 시각까지 (종가 동시호가 포함)
# 동시호가 체결은 틱 규칙 매수세 추정을 크게 흔들므로 수급 흐름 계산에서는 뺀다.
CONTINUOUS_END = dtime(15, 20)
SESSION_MINUTES = 390
DAILY_BARS = 260                 # 세력추정매매 스캐너와 일봉 캐시를 같이 쓰려고 같은 개수로 받는다


def session_bars(df: pd.DataFrame, day: date | None = None) -> pd.DataFrame:
    """정규장 1분봉(`09:01`~`15:31` 봉)만 남긴다. `day` 를 주면 그 날짜만."""
    if df.empty:
        return df
    t = df.index.time
    out = df[(t > SESSION_OPEN) & (t <= SESSION_CLOSE)]
    if day is not None:
        out = out[out.index.date == day]
    return out


def minute_offset(ts: pd.Timestamp) -> int:
    """09:00 기준 경과 분. 첫 정규장 봉(`09:01`)이 1 이다."""
    return (ts.hour - 9) * 60 + ts.minute


def continuous_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """접속매매 구간(`09:01`~`15:20` 봉)만. 종가 동시호가 봉을 뺀다."""
    return bars[bars.index.time <= CONTINUOUS_END] if not bars.empty else bars


def tick_flow(bars: pd.DataFrame) -> pd.DataFrame:
    """1분봉 틱 규칙으로 매수/매도 추정 거래량을 나눈다.

    직전 봉보다 종가가 오르면 매수, 내리면 매도, 같으면 봉 방향(종가 vs 시가)으로,
    그것도 같으면 반반. 체결강도 원자료가 없어서 쓰는 **추정치**다.
    """
    close = bars["close"]
    prev = close.shift(1)
    sign = np.sign(close - prev)
    body = np.sign(bars["close"] - bars["open"])
    sign = sign.where(sign != 0, body).fillna(body)
    vol = bars["volume"]
    buy = vol.where(sign > 0, 0.0) + vol.where(sign == 0, 0.0) / 2
    sell = vol - buy
    out = pd.DataFrame({"buy": buy, "sell": sell}, index=bars.index)
    out["cvd"] = (out["buy"] - out["sell"]).cumsum()
    return out


def vwap(bars: pd.DataFrame) -> pd.Series:
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    cum_vol = bars["volume"].cumsum()
    return (typical * bars["volume"]).cumsum() / cum_vol.where(cum_vol > 0)


def clean(v):
    """JSON 으로 내보낼 값 정리 — NaN/inf → None, numpy 수 → 파이썬 수."""
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return float(v) if np.isfinite(v) else None
    return v
