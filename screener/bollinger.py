"""볼린저 밴드 반전(Mean Reversion) 매매 지표 · 판정 엔진.

주가가 박스권일 때 볼린저 밴드 상·하한선 터치와 RSI 과매수/과매도를 함께 보고
진입·청산을 판정한다. 외부 지표 라이브러리(TA-Lib 등)에 의존하지 않고
pandas 만으로 계산한다 (폐쇄망·컨테이너 고려).

    매수 : 종가(현재가) <= 하한선  AND  RSI <= 30
    매도 : 종가(현재가) >= 상한선  AND  RSI >= 70            → 전량 청산
    부분익절 : 보유 중 중앙선(20 이평) 도달 시 50% 청산 (옵션)
    손절 : 매수가 대비 -Y% 이탈 시 조건 불문 전량 청산

`PatternDetector`(세력 추정 매매법)와 마찬가지로 **판정 로직은 이 모듈 하나뿐**이다.
봇(`bollinger_bot`)과 백테스트가 같은 함수를 호출해 라이브와 검증이 갈라지지 않게 한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from datetime import time as dtime

import numpy as np
import pandas as pd

# 지원 봉 주기 → (토스 candles interval, pandas resample rule)
INTERVALS: dict[str, tuple[str, str | None]] = {
    "1m": ("1m", None),
    "5m": ("1m", "5min"),
    "15m": ("1m", "15min"),
    "30m": ("1m", "30min"),
    "60m": ("1m", "60min"),
    "1d": ("1d", None),
}

ACTIONS = ("BUY", "SELL", "TAKE_PROFIT", "STOP", "HOLD")

# 국내 정규장 (KST). 시간외 단일가 봉을 지표 계산에서 빼기 위한 창.
_SESSION_OPEN = dtime(9, 0)
_SESSION_CLOSE = dtime(15, 30)


def _clamp(value, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


@dataclass
class BollingerConfig:
    """볼린저 반전 전략 파라미터. 임계값은 전부 여기에만 둔다."""

    # ---- 지표 ----
    bb_period: int = 20            # 볼린저 밴드 기간
    bb_stddev: float = 2.0         # 표준편차 배수
    rsi_period: int = 14           # RSI 기간
    rsi_buy: float = 30.0          # 과매도 (이하일 때 매수 허용)
    rsi_sell: float = 70.0         # 과매수 (이상일 때 매도 허용)

    # ---- 운용 ----
    interval: str = "15m"          # 봉 주기 (INTERVALS 키)
    symbols: tuple = ()            # 감시 종목 (6자리 코드)
    cash_ratio: float = 0.20       # 1회 매수에 쓸 예수금 비율 (X%)
    max_positions: int = 3         # 동시 보유 종목 수 상한
    min_order_amount: float = 50_000.0   # 이 금액 미만이면 주문하지 않는다

    # ---- 리스크 관리 ----
    stop_loss_pct: float = 0.05    # 매수가 대비 -Y% 강제 손절 (밴드 라이딩 방지)
    # 봇이 사지 않은 기존 보유분(계좌에 이미 있던 물량)까지 청산할지.
    # **기본 False** — 감시 목록에 장기 보유 종목을 넣었다가 첫 주기에 전량 털리는 사고를 막는다.
    manage_existing: bool = False
    mid_take_profit: bool = True   # 중앙선 도달 시 부분 익절 사용 여부
    mid_take_ratio: float = 0.50   # 부분 익절 비율 (보유 수량의 50%)

    def min_bars(self) -> int:
        """판정에 필요한 최소 봉 개수."""
        return max(self.bb_period, self.rsi_period + 1) + 5

    def candle_interval(self) -> str:
        return INTERVALS.get(self.interval, INTERVALS["15m"])[0]

    def resample_rule(self) -> str | None:
        return INTERVALS.get(self.interval, INTERVALS["15m"])[1]

    def describe(self) -> dict:
        return {**asdict(self), "symbols": list(self.symbols)}

    @classmethod
    def from_body(cls, body: dict, defaults: "BollingerConfig | None" = None) -> "BollingerConfig":
        """설정 창 요청 본문 → 설정. 범위를 벗어난 값은 잘라내고 예상 밖 키는 버린다."""
        d = defaults or cls()
        raw = body.get("symbols", d.symbols)
        if isinstance(raw, str):
            raw = re.split(r"[\s,]+", raw)
        symbols = tuple(dict.fromkeys(
            s for s in (str(x).strip() for x in (raw or ())) if re.fullmatch(r"\d{6}", s)))
        interval = str(body.get("interval", d.interval))
        if interval not in INTERVALS:
            raise ValueError(f"지원하지 않는 봉 주기입니다: {interval}")
        return cls(
            bb_period=_clamp_int(body.get("bb_period"), d.bb_period, 5, 200),
            bb_stddev=_clamp(body.get("bb_stddev"), d.bb_stddev, 0.5, 5.0),
            rsi_period=_clamp_int(body.get("rsi_period"), d.rsi_period, 2, 100),
            rsi_buy=_clamp(body.get("rsi_buy"), d.rsi_buy, 1.0, 50.0),
            rsi_sell=_clamp(body.get("rsi_sell"), d.rsi_sell, 50.0, 99.0),
            interval=interval,
            symbols=symbols,
            cash_ratio=_clamp(body.get("cash_ratio"), d.cash_ratio, 0.01, 1.0),
            max_positions=_clamp_int(body.get("max_positions"), d.max_positions, 1, 20),
            min_order_amount=_clamp(body.get("min_order_amount"), d.min_order_amount,
                                    1_000.0, 1e9),
            stop_loss_pct=_clamp(body.get("stop_loss_pct"), d.stop_loss_pct, 0.005, 0.5),
            mid_take_profit=bool(body.get("mid_take_profit", d.mid_take_profit)),
            mid_take_ratio=_clamp(body.get("mid_take_ratio"), d.mid_take_ratio, 0.1, 1.0),
            manage_existing=bool(body.get("manage_existing", d.manage_existing)),
        )


@dataclass
class Snapshot:
    """마지막 봉 기준 지표 스냅샷 (화면·로그·알림 공용)."""

    time: str
    close: float
    upper: float
    mid: float
    lower: float
    rsi: float
    percent_b: float               # (종가-하한)/(상한-하한). 0 이하=하단 이탈, 1 이상=상단 이탈
    bandwidth: float               # (상한-하한)/중앙 — 박스권 폭

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in asdict(self).items()}


@dataclass
class Decision:
    """한 종목·한 시점의 판정 결과."""

    symbol: str
    action: str                    # ACTIONS
    reason: str                    # 사람이 읽을 사유
    snapshot: Snapshot
    quantity_ratio: float = 1.0    # 청산 비율 (부분 익절이면 0.5)

    @property
    def is_exit(self) -> bool:
        return self.action in ("SELL", "TAKE_PROFIT", "STOP")

    def as_dict(self) -> dict:
        return {"종목코드": self.symbol, "액션": self.action, "사유": self.reason,
                "비율": self.quantity_ratio, **self.snapshot.as_dict()}


@dataclass
class Position:
    """봇이 기억하는 보유 상태. 수량은 실제 잔고로 계속 덮어쓴다."""

    symbol: str
    quantity: float = 0.0
    entry_price: float = 0.0
    entered_at: str = ""
    mid_taken: bool = False        # 중앙선 부분 익절을 이미 했는지
    adopted: bool = False          # 봇이 산 게 아니라 계좌에 이미 있던 물량
    name: str = ""
    note: str = ""
    history: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------- 지표
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI (Wilder 평활). TA-Lib `RSI` 와 같은 정의다."""
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder 평활 = alpha 1/period 의 지수이동평균
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    # 0 나눗셈 보정: 하락이 없으면 100, 상승·하락이 모두 없으면(완전 횡보) 50.
    # 워밍업 구간은 avg_* 가 NaN 이라 비교가 False → NaN 이 그대로 남는다.
    out = out.mask(avg_loss == 0, 100.0)
    return out.mask((avg_loss == 0) & (avg_gain == 0), 50.0)


def bollinger_bands(close: pd.Series, period: int = 20,
                    stddev: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """볼린저 밴드 (상한, 중앙=SMA, 하한). 표준편차는 모집단(ddof=0) 기준."""
    mid = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std(ddof=0)
    return mid + sd * stddev, mid, mid - sd * stddev


def enrich(df: pd.DataFrame, cfg: BollingerConfig) -> pd.DataFrame:
    """OHLCV 에 밴드·RSI 컬럼을 붙인다."""
    out = df.copy()
    upper, mid, lower = bollinger_bands(out["close"], cfg.bb_period, cfg.bb_stddev)
    out["band_upper"], out["band_mid"], out["band_lower"] = upper, mid, lower
    out["rsi"] = rsi(out["close"], cfg.rsi_period)
    width = (upper - lower).replace(0.0, np.nan)
    out["percent_b"] = (out["close"] - lower) / width
    out["bandwidth"] = (upper - lower) / mid.replace(0.0, np.nan)
    return out


def resample(df: pd.DataFrame, rule: str | None) -> pd.DataFrame:
    """1분봉 → N분봉. 토스 캔들 API 는 1m/1d 만 주므로 중간 주기는 직접 만든다."""
    if not rule or df.empty:
        return df
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df.resample(rule, label="left", closed="left").agg(agg).dropna(subset=["close"])


def regular_session(df: pd.DataFrame) -> pd.DataFrame:
    """정규장(09:00~15:30) 봉만 남긴다.

    토스 1분봉에는 시간외 단일가 체결분도 섞여 온다. 유동성이 전혀 다른 이 봉들을
    같이 넣으면 이동평균·표준편차가 왜곡돼 밴드가 엉뚱한 자리에 그려진다.
    """
    if df.empty:
        return df
    t = df.index.time
    return df[(t >= _SESSION_OPEN) & (t <= _SESSION_CLOSE)]


def prepare(df: pd.DataFrame, cfg: BollingerConfig) -> pd.DataFrame:
    """원본 캔들 → 판정에 쓸 봉. 분봉이면 정규장만 걸러 리샘플한다.

    판정(`evaluate`)과 차트가 **같은 함수**를 쓰게 해 화면과 주문 근거가 갈라지지 않게 한다.
    """
    if cfg.candle_interval() == "1m":
        df = regular_session(df)
    return resample(df, cfg.resample_rule())


def snapshot(df: pd.DataFrame, live_price: float | None = None) -> Snapshot | None:
    """`enrich` 된 DataFrame 의 마지막 봉으로 스냅샷을 만든다.

    `live_price` 를 주면 종가 대신 현재가로 판정한다 (장중 실시간).
    밴드·RSI 자체는 봉 기준 값이라 갱신하지 않는다.
    """
    if df.empty:
        return None
    row = df.iloc[-1]
    if not np.isfinite(row.get("band_lower", np.nan)) or not np.isfinite(row.get("rsi", np.nan)):
        return None
    close = float(live_price) if live_price and np.isfinite(live_price) else float(row["close"])
    upper, mid, lower = float(row["band_upper"]), float(row["band_mid"]), float(row["band_lower"])
    width = upper - lower
    return Snapshot(
        time=str(df.index[-1]),
        close=close, upper=upper, mid=mid, lower=lower, rsi=float(row["rsi"]),
        percent_b=(close - lower) / width if width else float("nan"),
        bandwidth=width / mid if mid else float("nan"),
    )


# --------------------------------------------------------------------- 판정
def decide(symbol: str, snap: Snapshot, cfg: BollingerConfig,
           position: Position | None = None) -> Decision:
    """스냅샷 하나로 매매 판정. 우선순위는 **손절 > 청산 > 부분익절 > 진입**.

    손절을 가장 먼저 보는 이유: 하한선을 타고 내리 밀리는 '밴드 라이딩' 구간에서는
    매수 조건이 계속 참이라, 손절을 뒤에 두면 물타기로 빠져든다.
    """
    holding = bool(position and position.quantity > 0)

    if holding and position.entry_price > 0:
        loss = snap.close / position.entry_price - 1
        if loss <= -cfg.stop_loss_pct:
            return Decision(symbol, "STOP",
                            f"손절 {loss * 100:.2f}% (기준 −{cfg.stop_loss_pct * 100:.1f}%) — 전량 시장가 매도",
                            snap, 1.0)

    if holding:
        if snap.close >= snap.upper and snap.rsi >= cfg.rsi_sell:
            return Decision(symbol, "SELL",
                            f"상한선 {snap.upper:,.0f} 도달 + RSI {snap.rsi:.1f} ≥ {cfg.rsi_sell:.0f} — 전량 청산",
                            snap, 1.0)
        if cfg.mid_take_profit and not position.mid_taken and snap.close >= snap.mid \
                and snap.close > position.entry_price:
            return Decision(symbol, "TAKE_PROFIT",
                            f"중앙선 {snap.mid:,.0f} 도달 — {cfg.mid_take_ratio * 100:.0f}% 부분 익절",
                            snap, cfg.mid_take_ratio)
        return Decision(symbol, "HOLD",
                        f"보유 중 (RSI {snap.rsi:.1f}, %B {snap.percent_b:.2f})", snap, 0.0)

    if snap.close <= snap.lower and snap.rsi <= cfg.rsi_buy:
        return Decision(symbol, "BUY",
                        f"하한선 {snap.lower:,.0f} 이탈 + RSI {snap.rsi:.1f} ≤ {cfg.rsi_buy:.0f} — 시장가 매수",
                        snap, 1.0)

    return Decision(symbol, "HOLD",
                    f"조건 미충족 (RSI {snap.rsi:.1f}, %B {snap.percent_b:.2f})", snap, 0.0)


def evaluate(symbol: str, df: pd.DataFrame, cfg: BollingerConfig,
             position: Position | None = None,
             live_price: float | None = None) -> Decision | None:
    """원본 OHLCV → (리샘플 →) 지표 → 판정. 데이터가 모자라면 None."""
    bars = prepare(df, cfg)
    if len(bars) < cfg.min_bars():
        return None
    snap = snapshot(enrich(bars, cfg), live_price)
    if snap is None:
        return None
    return decide(symbol, snap, cfg, position)


def position_size(cash: float, price: float, cfg: BollingerConfig,
                  max_amount: float) -> int:
    """예수금 비율과 1회 주문 한도를 함께 적용한 매수 수량."""
    if price <= 0:
        return 0
    budget = min(cash * cfg.cash_ratio, max_amount)
    if budget < cfg.min_order_amount:
        return 0
    return int(budget // price)
