"""수급 포착 매매 — 장 초반 거래량 급증 + 수급 연속성 추적.

전략 흐름 (피터 터크만식 시장 수급 분석을 이 저장소 데이터로 옮긴 것)
  1) **1차 포착 (09:00~10:00)** — 거래대금 상위 종목 중, 평소 **같은 시각까지의 누적 거래량**
     대비 오늘 누적 거래량이 `surge_min`(200%) 이상인 종목.
     "평소" 는 과거 `profile_days` 영업일의 같은 분까지 누적 거래량 평균이다
     (장 초반에 거래가 몰리는 일중 패턴을 그대로 반영하려고 선형 비례를 쓰지 않는다).
  2) **재료 검증** — 토스 OpenAPI 에는 뉴스·공시 API 가 없다. 자동 판정은 하지 않고
     갭상승(단발성 호재 의심)·매수 유의종목·외국인/기관 잠정 순매수로 보조 판단만 한다.
  3) **수급 연속성 (장 시작 `confirm_minutes` 분 이후)** — 최근 `flow_window` 분 동안
     매수 추정 거래량 비중, 누적 매수세(CVD) 기울기, VWAP·5분봉 이평 위 유지,
     거래량 급감 여부로 '지속 / 약화 / 이탈' 을 가른다.
  4) **차트 위치** — 120일 가격 위치로 바닥권 탈출과 고점권(설거지 주의)을 구분한다.
  5) **손절선** — 5분봉 20이평 (형성 전에는 VWAP). 이 선을 깨거나 거래량이 줄며
     매도 우위로 바뀌면 '이탈' 이다.

한계 — 문서에 반드시 남길 것
  - **백테스트로 검증된 수치가 없다.** 점수 배점은 경험칙이다.
  - 체결 데이터에 매수/매도 구분이 없어 매수세는 **1분봉 틱 규칙으로 추정**한다
    (체결강도 원자료가 아니다).
  - 재료(뉴스·공시)는 사람이 직접 확인해야 한다.

미래 정보 차단: 모든 판정은 **판정 시각까지의 봉만** 쓴다. 평소 거래량은 오늘을 뺀
과거 영업일로만 계산한다.
"""

from __future__ import annotations

import html
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from typing import Callable

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

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

STATUS_WATCH = "관찰"        # 1차 포착, 연속성 판단 전 (장 시작 직후)
STATUS_HOLD = "지속"         # 수급 연속성 확인 — 진입 후보
STATUS_WEAK = "약화"         # 급증은 있었지만 연속성 조건 일부 미달
STATUS_EXIT = "이탈"         # 손절선 이탈 또는 거래량 감소 + 매도 우위
STATUS_ORDER = {STATUS_HOLD: 0, STATUS_WATCH: 1, STATUS_WEAK: 2, STATUS_EXIT: 3}


@dataclass(frozen=True)
class SupplyConfig:
    """수급 포착 매매 임계값 (한 곳에만 둔다)."""

    # ---- 후보
    candidates: int = 100             # 거래대금 상위 N (랭킹 API 상한 100)
    min_turnover: float = 10e9        # 당일 누적 거래대금 하한 (100억)
    min_price: float = 1_000.0        # 저가주는 거래수량만 부풀어 왜곡된다
    exclude_etf: bool = True
    # ---- 1차: 거래량 급증
    early_minutes: int = 60           # 1차 포착 구간 (09:00 부터 N분)
    profile_days: int = 5             # '평소' 누적 거래량을 구할 과거 영업일 수
    min_profile_days: int = 2         # 이보다 적으면 일평균 기반 근사로 대체
    surge_min: float = 2.0            # 200%
    surge_strong: float = 5.0         # 500%
    entry_score_min: int = 70         # '진입'(들어갈 만한 것) 판정 종합 점수 컷오프
    # ---- 2차: 수급 연속성
    confirm_minutes: int = 30         # 이 시간이 지나야 연속성을 판단한다
    flow_window: int = 30             # 최근 N분
    buy_ratio_min: float = 0.55       # 매수 추정 거래량 비중 하한
    exit_buy_ratio: float = 0.45      # 이보다 낮고 거래량이 줄면 이탈
    volume_fade: float = 0.5          # 최근 창 거래량 < 직전 창 × 이 값 → 급감
    ma_rule: str = "5min"             # 손절 기준 분봉
    ma_period: int = 20
    # ---- 차트 위치 · 재료
    lookback: int = 120
    bottom_position: float = 0.35     # 120일 가격 범위 하위 35% → 바닥권
    near_high: float = 0.95           # 120일 최고가의 95% 이상 → 고점권(설거지 주의)
    gap_warn: float = 0.05            # 시가 갭 5% 이상 → 단발성 재료 의심
    # ---- 보조 조회 (후보만)
    use_investor: bool = True         # 투자자별 잠정 순매수 (3 TPS)
    use_orderbook: bool = True        # 호가 잔량 (표시만)
    exclude_warned: bool = True

    def describe(self) -> dict:
        return asdict(self)


# ================================================================== 지표
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


def minute_ma(bars: pd.DataFrame, rule: str, period: int) -> pd.Series:
    """N분봉 이동평균을 1분봉 시각에 맞춰 편다 (완성된 N분봉만 사용 — 미래 정보 차단)."""
    if bars.empty:
        return pd.Series(dtype=float)
    closes = bars["close"].resample(rule, label="right", closed="left").last().dropna()
    ma = closes.rolling(period, min_periods=period).mean()
    # label=right 이므로 09:05 값은 09:00~09:04 봉이 완성된 뒤에야 쓸 수 있다.
    return ma.reindex(bars.index, method="ffill")


def expected_cum_volume(profiles: list[pd.Series], offset: int) -> float:
    """과거 영업일들의 '같은 분까지' 누적 거래량 평균."""
    vals = []
    for p in profiles:
        if p.empty:
            continue
        vals.append(float(p[(p.index >= 1) & (p.index <= offset)].sum()))
    vals = [v for v in vals if v > 0]
    return float(np.mean(vals)) if vals else float("nan")


def price_position(daily: pd.DataFrame, price: float, lookback: int) -> tuple[float, float]:
    """(120일 범위 내 위치 0~1, 120일 최고가 대비 비율)."""
    win = daily.tail(lookback)
    if win.empty:
        return float("nan"), float("nan")
    hi, lo = float(win["high"].max()), float(win["low"].min())
    pos = (price - lo) / (hi - lo) if hi > lo else float("nan")
    return pos, price / hi if hi else float("nan")


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


# ================================================================== 판정
@dataclass
class SupplySignal:
    symbol: str
    name: str
    session: date
    as_of: pd.Timestamp
    status: str
    price: float
    prev_close: float
    open_price: float
    turnover: float                   # 당일 누적 거래대금 (원)
    surge_ratio: float                # 1차 구간 기준 평소 대비 누적 거래량 배수
    surge_basis: str                  # "과거 N일 같은 시각" | "일평균 근사"
    surge_at: str                     # 급증 배수를 잰 시각
    day_volume_ratio: float           # 당일 누적 / 20일 평균 일거래량
    buy_ratio: float                  # 최근 창 매수 추정 비중
    buy_ratio_day: float              # 당일 전체 매수 추정 비중
    cvd_slope: float                  # 최근 창 누적 매수세 변화 (주)
    volume_trend: float               # 최근 창 거래량 / 직전 창 거래량
    vwap: float
    ma: float                         # 5분봉 20이평 (없으면 NaN)
    stop: float                       # 손절선
    position: float                   # 120일 가격 위치
    high_ratio: float                 # 120일 최고가 대비
    gap: float                        # 시가 갭
    checks: dict = field(default_factory=dict)   # 단계별 통과 여부
    notes: list[str] = field(default_factory=list)
    investor: dict = field(default_factory=dict)   # 외국인·기관·프로그램 잠정 순매수
    orderbook: dict = field(default_factory=dict)  # 매수/매도 잔량
    score: int = 0
    score_detail: list = field(default_factory=list)
    entry: bool = False                          # 여러 지표 종합 '진입 적합'(들어갈 만한 것)
    entry_reasons: list = field(default_factory=list)   # 진입 판정 근거(충족/미충족)

    @property
    def change(self) -> float:
        return self.price / self.prev_close - 1 if self.prev_close else float("nan")

    @property
    def stop_pct(self) -> float:
        return (self.price - self.stop) / self.price if self.price and self.stop == self.stop else float("nan")

    def to_dict(self) -> dict:
        r = lambda v, d=2: None if v is None or v != v else round(float(v), d)  # noqa: E731
        return {
            "종목코드": self.symbol, "종목명": self.name,
            "기준일": self.session.isoformat(), "판정시각": self.as_of.strftime("%H:%M"),
            "상태": self.status, "진입": self.entry, "진입근거": self.entry_reasons,
            "점수": self.score, "점수상세": self.score_detail,
            "현재가": r(self.price, 0), "등락률%": r(self.change * 100),
            "시가갭%": r(self.gap * 100), "거래대금": r(self.turnover, 0),
            "급증배수": r(self.surge_ratio), "급증기준": self.surge_basis, "급증시각": self.surge_at,
            "일평균대비": r(self.day_volume_ratio),
            "매수비중%": r(self.buy_ratio * 100, 1), "당일매수비중%": r(self.buy_ratio_day * 100, 1),
            "누적매수세": r(self.cvd_slope, 0), "거래량추세": r(self.volume_trend),
            "VWAP": r(self.vwap, 0), "5분이평": r(self.ma, 0),
            "손절가": r(self.stop, 0), "손절폭%": r(self.stop_pct * 100),
            "가격위치%": r(self.position * 100, 0), "고점대비%": r(self.high_ratio * 100, 1),
            "체크": clean(self.checks), "유의사항": self.notes,
            "투자자": clean(self.investor), "호가": clean(self.orderbook),
        }


def evaluate(symbol: str, name: str, bars: pd.DataFrame, daily: pd.DataFrame,
             profiles: list[pd.Series], turnover: float,
             cfg: SupplyConfig) -> SupplySignal | None:
    """한 종목 판정. 1차 급증 조건을 못 넘으면 None.

    bars    : 당일 정규장 1분봉 (판정 시각까지)
    daily   : 일봉 (당일 봉이 섞여 있어도 된다 — 여기서 뺀다)
    profiles: 과거 영업일별 09:00~ 1분 거래량 (index = 분 오프셋)
    """
    if bars.empty:
        return None
    session = bars.index[-1].date()
    bars = session_bars(bars, session)
    if bars.empty:
        return None
    past = daily[daily.index.date < session]
    if past.empty:
        return None

    price = float(bars["close"].iloc[-1])
    prev_close = float(past["close"].iloc[-1])
    open_price = float(bars["open"].iloc[0])
    if price < cfg.min_price or turnover < cfg.min_turnover:
        return None

    as_of = bars.index[-1]
    now_off = minute_offset(as_of)

    # ---- 1차: 장 초반 구간의 급증 배수 (구간이 끝났으면 구간 끝 시각 기준)
    eval_off = min(now_off, cfg.early_minutes)
    early = bars[[minute_offset(t) <= eval_off for t in bars.index]]
    cum_now = float(early["volume"].sum())
    usable = [p for p in profiles if not p.empty and p.sum() > 0]
    avg_daily = float(past["volume"].tail(20).mean())
    if len(usable) >= cfg.min_profile_days:
        expected = expected_cum_volume(usable, eval_off)
        basis = f"과거 {len(usable)}일 같은 시각"
    else:
        # 과거 분봉이 부족하면 일평균 × 경과 비율로 근사한다 (장 초반을 과소평가해 배수가 커진다)
        expected = avg_daily * (eval_off + 1) / SESSION_MINUTES
        basis = "일평균 근사"
    surge = cum_now / expected if expected and expected == expected else float("nan")
    if not (surge == surge and surge >= cfg.surge_min):
        return None
    surge_at = (datetime.combine(session, SESSION_OPEN)
                + pd.Timedelta(minutes=eval_off)).strftime("%H:%M")

    # ---- 2차: 수급 연속성 (동시호가 제외)
    flow = tick_flow(continuous_bars(bars))
    w = cfg.flow_window
    recent, prior = flow.tail(w), flow.iloc[-2 * w:-w] if len(flow) > w else flow.iloc[0:0]
    rtot = float(recent["buy"].sum() + recent["sell"].sum())
    buy_ratio = float(recent["buy"].sum()) / rtot if rtot else float("nan")
    dtot = float(flow["buy"].sum() + flow["sell"].sum())
    buy_ratio_day = float(flow["buy"].sum()) / dtot if dtot else float("nan")
    cvd_slope = float(recent["cvd"].iloc[-1] - recent["cvd"].iloc[0]) if len(recent) > 1 else 0.0
    pvol = float(prior["buy"].sum() + prior["sell"].sum())
    volume_trend = rtot / pvol if pvol else float("nan")

    vw = float(vwap(bars).iloc[-1])
    ma = float(minute_ma(bars, cfg.ma_rule, cfg.ma_period).iloc[-1])
    stop = ma if ma == ma else vw

    position, high_ratio = price_position(past, price, cfg.lookback)
    gap = open_price / prev_close - 1 if prev_close else float("nan")
    day_ratio = float(bars["volume"].sum()) / avg_daily if avg_daily else float("nan")

    above_vwap = price >= vw
    above_stop = price >= stop
    faded = volume_trend == volume_trend and volume_trend < cfg.volume_fade
    checks = {
        "거래량급증": True,
        "거래대금": turnover >= cfg.min_turnover,
        "매수우위": bool(buy_ratio == buy_ratio and buy_ratio >= cfg.buy_ratio_min),
        "누적매수상승": cvd_slope > 0,
        "VWAP위": bool(above_vwap),
        "손절선위": bool(above_stop),
        "거래량유지": not faded,
        "바닥권": bool(position == position and position <= cfg.bottom_position),
        "고점권아님": not (high_ratio == high_ratio and high_ratio >= cfg.near_high),
        "갭과열아님": not (gap == gap and gap >= cfg.gap_warn),
    }
    notes: list[str] = []
    if basis == "일평균 근사":
        notes.append("과거 분봉 부족 — 급증배수는 일평균 근사")
    if not checks["갭과열아님"]:
        notes.append(f"시가 갭 +{gap * 100:.1f}% — 단발성 재료 여부 확인")
    if not checks["고점권아님"]:
        notes.append("120일 고점권 급증 — 물량 넘기기(설거지) 주의")
    if ma != ma:
        notes.append("5분봉 이평 형성 전 — 손절선은 VWAP")

    if now_off < cfg.confirm_minutes:
        status = STATUS_WATCH
    elif not above_stop or (faded and buy_ratio == buy_ratio and buy_ratio < cfg.exit_buy_ratio):
        status = STATUS_EXIT
    elif all(checks[k] for k in ("매수우위", "누적매수상승", "VWAP위", "손절선위", "거래량유지")):
        status = STATUS_HOLD
    else:
        status = STATUS_WEAK

    sig = SupplySignal(
        symbol=symbol, name=name, session=session, as_of=as_of, status=status,
        price=price, prev_close=prev_close, open_price=open_price, turnover=turnover,
        surge_ratio=surge, surge_basis=basis, surge_at=surge_at, day_volume_ratio=day_ratio,
        buy_ratio=buy_ratio, buy_ratio_day=buy_ratio_day, cvd_slope=cvd_slope,
        volume_trend=volume_trend, vwap=vw, ma=ma, stop=stop,
        position=position, high_ratio=high_ratio, gap=gap, checks=checks, notes=notes,
    )
    rescore(sig, cfg)
    decide_entry(sig, cfg)
    return sig


def decide_entry(sig: SupplySignal, cfg: SupplyConfig) -> None:
    """여러 지표를 종합해 **'진입'(들어갈 만한 것)** 을 가른다.

    수급 '지속'(매수 우위·누적 매수 상승·VWAP/손절선 위·거래량 유지)에 더해,
    **단발성 재료(갭 과열)·고점권(설거지) 을 걸러내고 종합 점수가 컷오프 이상**일 때만
    진입 적합으로 본다. status(연속성 상태)는 건드리지 않고 별도 플래그로 둔다.

    ⚠ 이 판정도 백테스트로 검증된 컷오프가 아니다 (경험칙). 재료 확인은 사람이 한다.
    """
    reasons: list[dict] = []
    def check(ok: bool, label: str) -> bool:
        reasons.append({"항목": label, "충족": bool(ok)})
        return ok

    ok = True
    ok &= check(sig.status == STATUS_HOLD, "수급 지속(연속성 5조건)")
    ok &= check(sig.checks.get("갭과열아님", False), "갭 과열 아님(단발 재료 회피)")
    ok &= check(sig.checks.get("고점권아님", False), "고점권 아님(설거지 회피)")
    ok &= check(sig.score >= cfg.entry_score_min, f"종합 점수 {cfg.entry_score_min}점 이상")
    sig.entry = bool(ok)
    sig.entry_reasons = reasons


def rescore(sig: SupplySignal, cfg: SupplyConfig) -> None:
    """수급 점수 (100점). **경험칙 배점이며 백테스트로 검증되지 않았다.**"""
    parts: list[tuple[str, int, int, str]] = []    # (요인, 점수, 만점, 설명)

    s = sig.surge_ratio
    pts = 0 if s < cfg.surge_min else 12 if s < 3 else 18 if s < cfg.surge_strong else 25 if s < 10 else 20
    parts.append(("거래량 급증", pts, 25, f"평소 대비 {s * 100:.0f}%" + (" (과열)" if s >= 10 else "")))

    t = sig.turnover
    pts = 15 if t >= 100e9 else 10 if t >= 30e9 else 5 if t >= cfg.min_turnover else 0
    parts.append(("거래대금", pts, 15, f"{t / 1e8:,.0f}억"))

    b = sig.buy_ratio
    pts = 0 if b != b else int(round(max(0.0, min(1.0, (b - 0.5) / 0.2)) * 20))
    parts.append(("매수 우위", pts, 20, "–" if b != b else f"최근 {cfg.flow_window}분 매수 {b * 100:.0f}%"))

    pts = 15 if sig.checks["VWAP위"] and sig.checks["손절선위"] else 8 if sig.checks["VWAP위"] else 0
    parts.append(("가격 위치(VWAP·이평)", pts, 15, "위" if pts == 15 else "VWAP 위" if pts else "아래"))

    pts = 10 if sig.cvd_slope > 0 else 0
    parts.append(("누적 매수세", pts, 10, "우상향" if pts else "꺾임"))

    if not sig.checks["고점권아님"]:
        pts, why = 0, "고점권"
    elif sig.checks["바닥권"]:
        pts, why = 15, "바닥권 탈출"
    else:
        pts, why = 8, "중간"
    parts.append(("차트 위치", pts, 15, why))

    total = sum(p[1] for p in parts)
    if not sig.checks["갭과열아님"]:
        total -= 10
        parts.append(("갭상승 감점", -10, 0, "단발성 재료 의심"))
    net = sig.investor.get("외국인+기관") if sig.investor else None
    if net is not None and net > 0:
        total += 5
        parts.append(("외국인·기관 순매수", 5, 0, "잠정치 순매수"))
    sig.score = int(max(0, min(100, total)))
    sig.score_detail = [{"요인": n, "점수": p, "만점": m, "설명": d} for n, p, m, d in parts]


# ================================================================== 스캐너
@dataclass
class SupplyReport:
    signals: list[SupplySignal]
    scanned: int
    skipped: int
    errors: int
    session: date | None = None
    blocked: list[dict] = field(default_factory=list)


class SupplyScanner:
    """거래대금 상위 → 1분봉 판정 → 후보만 보조 조회."""

    def __init__(self, loader, cfg: SupplyConfig | None = None, max_workers: int = 6):
        self.loader = loader
        self.cfg = cfg or SupplyConfig()
        self.max_workers = max_workers

    def candidates(self) -> list[tuple[str, str, float]]:
        """(종목코드, 종목명, 당일 거래대금). ETF·우선주·거래정지는 뺀다."""
        cfg = self.cfg
        leaders = self.loader.turnover_leaders(cfg.candidates)
        metas = self.loader.metas([s for s, _ in leaders])
        out = []
        for sym, amount in leaders:
            m = metas.get(sym)
            if m is None or m.suspended or not m.is_common:
                continue
            if cfg.exclude_etf and m.security_type and m.security_type != "STOCK":
                continue
            out.append((sym, m.name, amount))
        return out

    def scan(self) -> SupplyReport:
        cands = self.candidates()
        signals: list[SupplySignal] = []
        skipped = errors = 0
        session: date | None = None
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futs = {pool.submit(self._one, s, n, a): s for s, n, a in cands}
            for f in as_completed(futs):
                try:
                    sig = f.result()
                except Exception as exc:
                    errors += 1
                    log.warning("%s 수급 판정 실패: %s", futs[f], exc)
                    continue
                if sig is None:
                    skipped += 1
                else:
                    signals.append(sig)
                    session = session or sig.session

        signals, blocked = self._enrich(signals)
        signals.sort(key=lambda s: (0 if s.entry else 1,
                                    STATUS_ORDER.get(s.status, 9), -s.score, -s.surge_ratio))
        log.info("수급 스캔 완료: 후보 %d, 포착 %d (지속 %d · 진입 %d), 제외 %d, 오류 %d",
                 len(cands), len(signals), sum(s.status == STATUS_HOLD for s in signals),
                 sum(s.entry for s in signals), len(blocked), errors)
        return SupplyReport(signals, len(cands), skipped, errors, session, blocked)

    def _one(self, symbol: str, name: str, turnover: float) -> SupplySignal | None:
        cfg = self.cfg
        bars = self.loader.session_minutes(symbol)
        if bars.empty:
            return None
        session = bars.index[-1].date()
        daily = self.loader.ohlcv(symbol, DAILY_BARS)
        past_days = [d for d in pd.Index(daily.index.date).unique() if d < session][-cfg.profile_days:]
        profiles = [self.loader.minute_profile(symbol, d, cfg.early_minutes) for d in past_days]
        return evaluate(symbol, name, bars, daily, profiles, turnover, cfg)

    def _enrich(self, signals: list[SupplySignal]) -> tuple[list[SupplySignal], list[dict]]:
        """포착된 종목만 유의사항·투자자·호가를 조회한다 (전수 조회 금지)."""
        from .config import StrategyConfig
        from .screener import WARNING_LABELS
        cfg = self.cfg
        kept: list[SupplySignal] = []
        blocked: list[dict] = []
        # 차단·표시 목록은 세력추정매매와 같은 기준을 쓴다 (한 곳에서만 정의)
        blocking = set(StrategyConfig.blocking_warnings)
        notice = set(StrategyConfig.notice_warnings)
        for sig in signals:
            if cfg.exclude_warned:
                try:
                    items = self.loader.warnings(sig.symbol)
                except Exception as exc:
                    log.warning("%s 유의사항 조회 실패(통과 처리): %s", sig.symbol, exc)
                    items = []
                types = {i.get("warningType") for i in items}
                hit = sorted(t for t in types if t in blocking)
                if hit:
                    blocked.append({"종목코드": sig.symbol, "종목명": sig.name,
                                    "사유": ", ".join(WARNING_LABELS.get(t, t) for t in hit)})
                    continue
                sig.notes += [WARNING_LABELS.get(t, t) for t in sorted(t for t in types if t in notice)]
            if cfg.use_investor:
                try:
                    sig.investor = self.loader.investor_flow(sig.symbol, sig.session)
                except Exception as exc:
                    log.debug("%s 투자자 동향 조회 실패: %s", sig.symbol, exc)
            if cfg.use_orderbook:
                try:
                    sig.orderbook = self.loader.orderbook_summary(sig.symbol)
                except Exception as exc:
                    log.debug("%s 호가 조회 실패: %s", sig.symbol, exc)
            rescore(sig, cfg)
            decide_entry(sig, cfg)     # 보조 조회(투자자 +5점)까지 반영한 최종 진입 판정
            kept.append(sig)
        return kept, blocked

    def chart(self, symbol: str) -> dict:
        """상세 차트용 시계열 — 판정과 **같은 함수**로 만든다."""
        cfg = self.cfg
        bars = self.loader.session_minutes(symbol)
        if bars.empty:
            return {"bars": []}
        session = bars.index[-1].date()
        bars = session_bars(bars, session)
        flow = tick_flow(continuous_bars(bars))
        vw = vwap(bars)
        ma = minute_ma(bars, cfg.ma_rule, cfg.ma_period)
        daily = self.loader.ohlcv(symbol, DAILY_BARS)
        past_days = [d for d in pd.Index(daily.index.date).unique() if d < session][-cfg.profile_days:]
        profiles = [p for p in (self.loader.minute_profile(symbol, d, cfg.early_minutes)
                                for d in past_days) if not p.empty and p.sum() > 0]
        early = [{"m": k, "expected": expected_cum_volume(profiles, k) if profiles else None}
                 for k in range(1, cfg.early_minutes + 1)]
        cvd = flow["cvd"].reindex(bars.index)          # 동시호가 봉은 흐름 계산에서 빠져 비어 있다
        return clean({
            "session": session.isoformat(),
            "early_minutes": cfg.early_minutes,
            "profile_days": len(profiles),
            "bars": [{"t": ts.strftime("%H:%M"), "m": minute_offset(ts),
                      "o": row.open, "h": row.high, "l": row.low, "c": row.close,
                      "v": row.volume, "vwap": round(vw.iloc[i], 2), "ma": round(ma.iloc[i], 2),
                      "cvd": cvd.iloc[i]}
                     for i, (ts, row) in enumerate(bars.iterrows())],
            "profile": early,
        })


# ================================================================== 서비스
@dataclass
class SupplyState:
    signals: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    session: str | None = None
    scanned: int = 0
    skipped: int = 0
    errors: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    duration: float = 0.0
    running: bool = False
    error: str | None = None
    market_open: bool = False

    def as_dict(self) -> dict:
        return {**self.__dict__}


class SupplyService:
    """주기 스캔 + 상태 보관.

    장중에는 `interval` 초마다 돈다. 장 밖에서는 데이터가 바뀌지 않으므로
    `idle_interval` 초에 한 번만 다시 본다 (토스 API 호출을 아끼려고).
    """

    def __init__(self, scanner: SupplyScanner, interval: int = 60, idle_interval: int = 1800,
                 notify: Callable[[str], object] | None = None,
                 clock: Callable[[], datetime] = datetime.now):
        self.scanner = scanner
        self.interval = interval
        self.idle_interval = idle_interval
        self.notify = notify
        self.clock = clock
        self.state = SupplyState()
        self._alerted: set[tuple[str, str]] = set()   # (기준일, 종목) — 같은 날 한 번만 알린다
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_scan = 0.0

    @property
    def cfg(self) -> SupplyConfig:
        return self.scanner.cfg

    def market_open(self) -> bool:
        from .bollinger_bot import market_is_open
        return market_is_open(self.clock())

    def scan_once(self) -> SupplyState:
        if not self._lock.acquire(blocking=False):
            return self.state
        try:
            self.state.running = True
            self.state.error = None
            self.state.started_at = _now()
            t0 = time.monotonic()
            report = self.scanner.scan()
            self.state.signals = [clean(s.to_dict()) for s in report.signals]
            self.state.blocked = report.blocked
            self.state.session = report.session.isoformat() if report.session else None
            self.state.scanned, self.state.skipped, self.state.errors = (
                report.scanned, report.skipped, report.errors)
            self.state.duration = round(time.monotonic() - t0, 1)
            self.state.finished_at = _now()
            self._alert(report.signals)
        except Exception as exc:
            self.state.error = f"{type(exc).__name__}: {exc}"
            self.state.finished_at = _now()
            log.exception("수급 스캔 실패")
        finally:
            self.state.market_open = self.market_open()
            self._last_scan = time.monotonic()
            self.state.running = False
            self._lock.release()
        return self.state

    def _alert(self, signals: list[SupplySignal]) -> None:
        """여러 지표를 통과해 **'진입'(들어갈 만한 것)** 이 된 종목만 알린다 (장중에만).

        예전에는 '지속' 종목을 모두 보냈으나, 단발 재료·고점권까지 걸러낸 진입 후보만
        보내도록 바꿨다 (기준일·종목당 한 번). 화면에는 지속·약화 등 전체가 그대로 보인다.
        """
        if self.notify is None or not self.market_open():
            return
        fresh = [s for s in signals if s.entry
                 and (s.session.isoformat(), s.symbol) not in self._alerted]
        if not fresh:
            return
        lines = ["🔥 [수급포착] 진입 적합 종목 — 재료(뉴스·공시)는 직접 확인하세요"]
        for s in fresh:
            self._alerted.add((s.session.isoformat(), s.symbol))
            # 토스 종목 페이지 링크를 함께 보낸다. TelegramNotifier 는 HTML 모드라
            # 종목명은 이스케이프하고(F&F 같은 & 안전), 링크는 raw URL 로 둬 눈에 보이게 한다.
            url = f"https://www.tossinvest.com/stocks/{s.symbol}"
            lines.append(f"· {html.escape(s.name)}({s.symbol}) "
                         f"{s.price:,.0f}원 {s.change * 100:+.1f}% · "
                         f"급증 {s.surge_ratio * 100:.0f}% · 매수 {s.buy_ratio * 100:.0f}% · "
                         f"손절 {s.stop:,.0f} · {s.score}점\n  {url}")
        try:
            self.notify("\n".join(lines))
        except Exception as exc:
            log.warning("수급 알림 실패: %s", exc)

    def due(self) -> bool:
        """지금 다시 스캔할 차례인가."""
        if self.state.finished_at is None:
            return True
        wait = self.interval if self.market_open() else self.idle_interval
        return time.monotonic() - self._last_scan >= wait

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="supply-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.due():
                self.scan_once()
            self._stop.wait(15)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
