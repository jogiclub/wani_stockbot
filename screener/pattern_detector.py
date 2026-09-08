"""세력 추정 매매법 패턴 탐지 엔진.

STEP 1  기준봉(매집봉)  : 바닥권 + 윗꼬리 + 대량 거래량
STEP 2  매물 소화 구간  : 기준봉 이후 1~10영업일, 거래량 감소 + 눌림
STEP 3  돌파 시그널     : 거래량 증가 + '전일 고가' 돌파
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd

from .config import StrategyConfig


# ============================================================== 결과 데이터 구조
@dataclass
class BaseCandle:
    """STEP 1 에서 포착된 기준봉."""

    date: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    upper_tail_ratio: float
    volume_ratio: float          # 20일 평균 대비 배수
    is_volume_max: bool          # 20일 내 최고 거래량 여부
    drawdown: float              # 기간 최고가 대비 하락률
    bars_since: int = 0          # 기준봉 이후 경과 영업일

    def to_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.strftime("%Y-%m-%d")
        return d


@dataclass
class Signal:
    """STEP 3 최종 매수 시그널."""

    symbol: str
    name: str
    signal_date: pd.Timestamp
    base: BaseCandle
    prev_high: float             # 전일 고가 (= 돌파 기준선)
    trigger_price: float         # 돌파 확인 가격 (= 매수 기준가)
    last_price: float            # 현재가/종가
    volume: float
    volume_ratio_prev: float     # 당일 거래량 / 전일 거래량
    entry: float
    stop: float
    targets: tuple
    adjust_days: int
    adjust_vol_ratio: float      # 조정 구간 평균 거래량 / 기준봉 거래량
    notes: list[str] = field(default_factory=list)
    score: object = None         # screener.scoring.Score (스크리너가 채운다)

    @property
    def risk_pct(self) -> float:
        return (self.entry - self.stop) / self.entry * 100 if self.entry else float("nan")

    @property
    def gain_from_trigger(self) -> float:
        return (self.last_price / self.trigger_price - 1) * 100 if self.trigger_price else 0.0

    def to_dict(self) -> dict:
        return {
            "종목코드": self.symbol,
            "종목명": self.name,
            "시그널일": self.signal_date.strftime("%Y-%m-%d"),
            "기준봉일": self.base.date.strftime("%Y-%m-%d"),
            "기준봉_윗꼬리%": round(self.base.upper_tail_ratio * 100, 1),
            "기준봉_거래량배수": round(self.base.volume_ratio, 1),
            "조정일수": self.adjust_days,
            "조정거래량비": round(self.adjust_vol_ratio, 2),
            "전일고가": round(self.prev_high, 2),
            "현재가": round(self.last_price, 2),
            "거래량증가율%": round((self.volume_ratio_prev - 1) * 100, 1),
            "매수가": round(self.entry, 2),
            "손절가": round(self.stop, 2),
            "손절폭%": round(self.risk_pct, 2),
            "목표가": [round(t, 2) for t in self.targets],
            "유의사항": list(self.notes),
            "점수": self.score.total if self.score else None,
            "등급": self.score.grade if self.score else None,
            "판단": self.score.advice if self.score else None,
            "점수상세": self.score.as_dict()["세부"] if self.score else [],
        }

    def format_line(self) -> str:
        head = f"{self.score.total}점 {self.score.grade} " if self.score else ""
        return (f"{head}[{self.symbol} {self.name}] {self.signal_date:%Y-%m-%d} 돌파  "
                f"기준봉 {self.base.date:%m/%d}(윗꼬리 {self.base.upper_tail_ratio*100:.0f}%, "
                f"거래량 x{self.base.volume_ratio:.1f}) → 조정 {self.adjust_days}일  "
                f"전일고가 {self.prev_high:,.0f} 돌파, 현재 {self.last_price:,.0f} "
                f"(거래량 +{(self.volume_ratio_prev-1)*100:.0f}%)  "
                f"매수 {self.entry:,.0f} / 손절 {self.stop:,.0f} ({self.risk_pct:.1f}%) "
                f"/ 목표 {', '.join(f'{t:,.0f}' for t in self.targets)}"
                + (f"  ⚠ {', '.join(self.notes)}" if self.notes else ""))


# ================================================================== 지표 계산
def enrich(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """OHLCV 에 전략 판정용 파생 컬럼을 벡터 연산으로 추가한다."""
    out = df.copy()
    rng = (out["high"] - out["low"]).replace(0, np.nan)
    body_top = out[["open", "close"]].max(axis=1)

    # 윗꼬리 비율 = (고가 - max(시가,종가)) / (고가 - 저가)
    out["upper_tail_ratio"] = ((out["high"] - body_top) / rng).fillna(0.0)
    out["day_range"] = (out["high"] / out["low"].replace(0, np.nan) - 1).fillna(0.0)

    # 거래량: 당일 제외 20일 평균/최고와 비교 (shift(1) 로 미래 정보 차단)
    prev_vol = out["volume"].shift(1)
    out["vol_ma"] = prev_vol.rolling(cfg.vol_lookback, min_periods=cfg.vol_lookback // 2).mean()
    out["vol_max"] = prev_vol.rolling(cfg.vol_lookback, min_periods=cfg.vol_lookback // 2).max()
    out["vol_ratio_ma"] = out["volume"] / out["vol_ma"]
    out["is_vol_max"] = out["volume"] > out["vol_max"]
    out["vol_ratio_prev"] = out["volume"] / prev_vol

    # 바닥권 판별
    out[f"ma{cfg.ma_long}"] = out["close"].rolling(cfg.ma_long, min_periods=cfg.ma_long // 2).mean()
    roll_high = out["close"].rolling(cfg.bottom_lookback, min_periods=cfg.bottom_lookback // 2).max()
    out["drawdown"] = 1 - out["close"] / roll_high
    out["turnover_ma"] = (out["close"] * out["volume"]).rolling(
        cfg.vol_lookback, min_periods=cfg.vol_lookback // 2).mean()

    out["prev_high"] = out["high"].shift(1)
    out["prev_low"] = out["low"].shift(1)
    return out


# ================================================================== 탐지 엔진
class PatternDetector:
    """단일 종목 DataFrame 에 대해 STEP 1~3 을 판정한다."""

    def __init__(self, cfg: StrategyConfig | None = None):
        self.cfg = cfg or StrategyConfig()

    # ------------------------------------------------------- STEP 1
    def base_candle_mask(self, df: pd.DataFrame) -> pd.Series:
        """STEP 1: 기준봉(매집봉) 조건을 만족하는 봉의 불리언 마스크."""
        cfg = self.cfg
        bottom = (df["drawdown"] >= cfg.bottom_drawdown_min) | \
                 (df["close"] <= df[f"ma{cfg.ma_long}"] * cfg.ma_long_band)
        tail = df["upper_tail_ratio"] >= cfg.upper_tail_min
        wide = df["day_range"] >= cfg.base_range_min

        vol_max = df["is_vol_max"]
        vol_mult = df["vol_ratio_ma"] >= cfg.vol_mult_min
        if not cfg.require_vol_max:
            vol = vol_mult
        elif cfg.vol_condition_mode == "and":
            vol = vol_max & vol_mult
        else:
            vol = vol_max | vol_mult

        return (bottom & tail & wide & vol).fillna(False)

    def find_base_candles(self, df: pd.DataFrame) -> list[BaseCandle]:
        """최신 봉 기준 조정 허용 범위 안에 있는 기준봉들을 최근순으로 반환."""
        cfg = self.cfg
        mask = self.base_candle_mask(df)
        idx = np.flatnonzero(mask.to_numpy())
        last = len(df) - 1
        found: list[BaseCandle] = []
        for i in reversed(idx):
            bars_since = last - i
            if bars_since < cfg.adjust_min_days:
                continue
            if bars_since > cfg.adjust_max_days:
                break
            row = df.iloc[i]
            found.append(BaseCandle(
                date=df.index[i], open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]), volume=float(row["volume"]),
                upper_tail_ratio=float(row["upper_tail_ratio"]),
                volume_ratio=float(row["vol_ratio_ma"]) if np.isfinite(row["vol_ratio_ma"]) else float("nan"),
                is_volume_max=bool(row["is_vol_max"]), drawdown=float(row["drawdown"]),
                bars_since=int(bars_since),
            ))
        return found

    # ------------------------------------------------------- STEP 2
    def check_adjustment(self, df: pd.DataFrame, base: BaseCandle,
                         end_pos: int) -> tuple[bool, float, list[str]]:
        """기준봉 다음날 ~ 시그널 전일까지 매물 소화 구간을 검증한다.

        반환: (통과 여부, 조정 구간 평균거래량/기준봉 거래량, 사유 목록)
        """
        cfg = self.cfg
        base_pos = df.index.get_loc(base.date)
        window = df.iloc[base_pos + 1:end_pos]      # 시그널 당일 제외
        reasons: list[str] = []
        if window.empty:
            return False, float("nan"), ["조정 구간 없음"]

        vol_ratio = float(window["volume"].mean() / base.volume) if base.volume else float("nan")
        if vol_ratio > cfg.adjust_vol_max_ratio:
            reasons.append(f"조정 거래량 과다 ({vol_ratio:.2f} > {cfg.adjust_vol_max_ratio})")

        if cfg.adjust_hold_base_low and float(window["close"].min()) < base.low:
            reasons.append("조정 중 기준봉 저가 이탈")

        if float(window["close"].max()) > base.high * (1 + cfg.adjust_max_gain):
            reasons.append("조정 없이 이미 기준봉 고가 상회 (시세 분출)")

        return (not reasons), vol_ratio, reasons

    # ------------------------------------------------------- STEP 3
    def check_breakout(self, df: pd.DataFrame, pos: int,
                       live_price: float | None = None,
                       live_volume: float | None = None) -> tuple[bool, float, float, list[str]]:
        """전일 고가 돌파 + 거래량 증가 조건.

        `live_price`/`live_volume` 을 주면 장중 실시간 값으로 판정한다.
        반환: (통과, 거래량비, 판정에 쓴 가격, 사유)
        """
        cfg = self.cfg
        row = df.iloc[pos]
        prev_high = float(row["prev_high"])
        prev_vol = float(df["volume"].iloc[pos - 1]) if pos > 0 else float("nan")

        price = float(live_price) if live_price is not None else float(row["high"])
        volume = float(live_volume) if live_volume is not None else float(row["volume"])
        vol_ratio = volume / prev_vol if prev_vol else float("nan")

        reasons: list[str] = []
        threshold = prev_high * (1 + cfg.trigger_break_buffer)
        if cfg.trigger_break_prev_high and not price > threshold:
            reasons.append(f"전일 고가({prev_high:,.0f}) 미돌파")
        if not vol_ratio >= cfg.trigger_vol_ratio:
            reasons.append(f"거래량 부족 ({vol_ratio:.2f} < {cfg.trigger_vol_ratio})")
        return (not reasons), vol_ratio, threshold, reasons

    # ------------------------------------------------------- 종합 판정
    def detect(self, df: pd.DataFrame, symbol: str = "", name: str = "",
               live_price: float | None = None, live_volume: float | None = None,
               at: int = -1) -> Optional[Signal]:
        """마지막 봉(또는 `at` 위치)에서 매수 시그널이 발생했는지 판정."""
        cfg = self.cfg
        if len(df) < cfg.min_history:
            return None
        data = enrich(df, cfg) if "upper_tail_ratio" not in df.columns else df
        pos = len(data) + at if at < 0 else at
        if pos < 1:
            return None
        data = data.iloc[:pos + 1]

        ok, vol_ratio, threshold, _ = self.check_breakout(data, pos, live_price, live_volume)
        if not ok:
            return None

        for base in self.find_base_candles(data):
            passed, adj_ratio, _ = self.check_adjustment(data, base, pos)
            if not passed:
                continue
            row = data.iloc[pos]
            price = float(live_price) if live_price is not None else float(row["close"])
            entry = float(row["prev_high"]) * (1 + cfg.trigger_break_buffer)
            stop = self._stop_price(base, data, pos, entry)
            risk = max(entry - stop, 1e-9)
            return Signal(
                symbol=symbol, name=name or symbol, signal_date=data.index[pos], base=base,
                prev_high=float(row["prev_high"]), trigger_price=threshold, last_price=price,
                volume=float(live_volume if live_volume is not None else row["volume"]),
                volume_ratio_prev=vol_ratio, entry=entry, stop=stop,
                targets=tuple(entry + risk * m for m in cfg.target_r_multiples),
                adjust_days=base.bars_since, adjust_vol_ratio=adj_ratio,
            )
        return None

    def _stop_price(self, base: BaseCandle, df: pd.DataFrame, pos: int, entry: float) -> float:
        cfg = self.cfg
        if cfg.stop_mode == "prev_low":
            return float(df["low"].iloc[pos - 1])
        if cfg.stop_mode == "pct":
            return entry * (1 - cfg.stop_pct)
        return base.low          # 기본: 기준봉 저가

    # ------------------------------------------------------- 진단 / 백테스트
    def explain(self, df: pd.DataFrame) -> dict:
        """조건을 왜 통과/탈락했는지 사람이 읽을 수 있게 설명한다."""
        cfg = self.cfg
        if len(df) < cfg.min_history:
            return {"통과": False, "사유": [f"데이터 부족 ({len(df)} < {cfg.min_history})"]}
        data = enrich(df, cfg)
        pos = len(data) - 1
        bases = self.find_base_candles(data)
        info: dict = {"STEP1_기준봉후보": [b.to_dict() for b in bases]}
        if not bases:
            info.update({"통과": False, "사유": ["최근 조정 구간 내 기준봉 없음"]})
            return info
        ok3, vol_ratio, _, why3 = self.check_breakout(data, pos)
        base = bases[0]
        ok2, adj_ratio, why2 = self.check_adjustment(data, base, pos)
        info.update({
            "STEP2_조정": {"통과": ok2, "거래량비": adj_ratio, "사유": why2},
            "STEP3_돌파": {"통과": ok3, "거래량비": vol_ratio, "사유": why3},
            "통과": bool(ok2 and ok3),
        })
        return info

    def backtest_signals(self, df: pd.DataFrame, symbol: str = "", name: str = "") -> list[Signal]:
        """과거 전 구간에 대해 시그널 발생 시점을 모두 찾는다 (전략 검증용)."""
        cfg = self.cfg
        data = enrich(df, cfg)
        out: list[Signal] = []
        for pos in range(cfg.min_history, len(data)):
            sig = self.detect(data.iloc[:pos + 1], symbol, name, at=pos)
            if sig:
                out.append(sig)
        return out
