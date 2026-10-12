"""금요일 마감 돌파 매매 — 주말을 넘기는 날 14:30~15:15 거래량 급증 + 당일 고점 돌파 포착.

가설 (유튜브 '수익탐정사무소' 데빈 베이커 인터뷰 요약 → 한국장으로 옮긴 것)
  "일주일간 조용하던 종목이 금요일 마감 무렵 거래량을 동반해 당일 고점을 뚫는다
   → 주말을 넘길 큰 자금이 들어온 것 → 월요일 시초 갭상승."

한국장 조정
  - 마감 30분 전 = 15:00. 15:20~15:30 은 종가 단일가(동시호가)라 돌파를 추격할 수 없다.
    그래서 포착 창은 `window_start`(14:30) ~ `window_end`(15:15) 이고 15:20 이후 봉은 쓰지 않는다.
  - '금요일' = 주말을 넘기는 마지막 영업일. 금요일이 휴장이면 목요일이다 — 휴장 캘린더가 없어서
    `extra_days` 에 날짜를 직접 넣는다 (`main.py web --friday-extra-days 2026-10-01`).

판정 (`evaluate`)
  후보  = 창 구간 거래량이 과거 같은 구간의 `surge_min` 배 이상 AND 현재가가 창 이전 당일 고점 위
  진입  = 후보 AND 조용한 한 주 AND VWAP 위 AND 매수 우위 AND 고점 부근 AND 과열 아님
          AND 지수 국면이 꺾이지 않음 AND 종합 점수 ≥ `entry_score_min`

⚠ 한계 — 화면·문서에 반드시 남길 것
  - **백테스트로 검증된 수치가 없다.** P0 일봉 대리 백테스트(`friday_backtest.py`)는 '조건부'였다:
    총수익은 대조군보다 높지만 왕복 비용 0.75% 에서는 순손실, 손익분기 비용 약 0.63%.
    점수 배점·컷오프는 경험칙이다. 분봉 이력이 없어 실제 매매 성과는 **포워드 기록**(`FridayRecorder`)으로만 쌓는다.
  - 주말 동안의 뉴스·공시·미국 야간장은 통제할 수 없고 이 시스템에 공시 API 가 없다 — 사람이 확인한다.
  - 틱 규칙 매수세는 추정치다 (HTS 체결강도와 다름).
  - 이 화면은 **주문을 내지 않는다** (관찰 전용).

미래 정보 차단: 판정은 판정 시각까지의 봉만, '평소'는 오늘을 뺀 과거 영업일만 쓴다.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .friday_backtest import regime_series
from .supply import (DAILY_BARS, clean, continuous_bars, minute_offset, session_bars,
                     tick_flow, vwap)

log = logging.getLogger(__name__)

PROFILE_END = 375                  # 과거 '평소' 구간 끝 = 15:15 (09:00 + 375분)
OUTCOME_COST = 0.0075              # 결과표의 기본 왕복 비용 (P0 와 동일: 수수료 0.15% + 슬리피지 0.3%×2)


@dataclass(frozen=True)
class FridayConfig:
    """금요일 마감 돌파 임계값 (한 곳에만 둔다). P0 `ProxyParams` 와 의미를 맞췄다."""

    # ---- 언제
    weekdays: tuple[int, ...] = (4,)                 # 월=0 … 금=4
    extra_days: tuple[str, ...] = ()                 # 금요일이 휴장일 때의 목요일 등 (YYYY-MM-DD)
    window_start: dtime = dtime(14, 30)
    window_end: dtime = dtime(15, 15)                # 이후 봉은 동시호가 직전이라 판정에 쓰지 않는다
    early_cut: dtime = dtime(15, 0)                  # 영상 기준 '마감 30분 전' — 포착 시각 비교용
    # ---- 후보
    candidates: int = 100
    min_turnover: float = 3e9                        # 당일 누적 거래대금 하한 (30억)
    min_price: float = 1_000.0
    exclude_etf: bool = True
    # ---- 조용한 한 주 (직전 N영업일)
    quiet_days: int = 4
    quiet_range_max: float = 0.12
    quiet_vol_max: float = 1.0
    # ---- 창 거래량 급증 · 돌파
    profile_days: int = 5
    min_profile_days: int = 2
    surge_min: float = 2.0
    near_high: float = 0.99                          # 현재가 ≥ 당일 고가 × 이 값 (돌파 뒤 밀리지 않음)
    # ---- 수급
    flow_window: int = 30
    buy_ratio_min: float = 0.55
    hot_change: float = 0.20                         # 당일 +20% 이상은 상한가 추격 위험
    # ---- 시장 국면
    use_regime: bool = True
    regime_ma: int = 20
    # ---- 종합
    entry_score_min: int = 70
    exclude_warned: bool = True

    def is_record_day(self, day: date) -> bool:
        return day.weekday() in self.weekdays or day.isoformat() in self.extra_days

    def in_window(self, now: datetime) -> bool:
        """지금이 실시간 감시 구간인가 (창 시작 5분 전 ~ 창 끝 5분 후)."""
        if not self.is_record_day(now.date()):
            return False
        t = now.time()
        lo = (datetime.combine(now.date(), self.window_start) - pd.Timedelta(minutes=5)).time()
        hi = (datetime.combine(now.date(), self.window_end) + pd.Timedelta(minutes=5)).time()
        return lo <= t <= hi

    def describe(self) -> dict:
        d = asdict(self)
        for k in ("window_start", "window_end", "early_cut"):
            d[k] = d[k].strftime("%H:%M")
        return d


# ================================================================== 판정
@dataclass
class FridaySignal:
    symbol: str
    name: str
    market: str
    session: date
    as_of: pd.Timestamp
    price: float
    prev_close: float
    pre_high: float                   # 창 시작 이전 당일 고점 (돌파선)
    day_high: float
    vwap: float
    surge_ratio: float                # 창 구간 거래량 / 과거 같은 구간 평균
    surge_days: int
    day_volume_ratio: float           # 당일 누적 / 20일 평균
    turnover: float
    quiet_range: float                # 직전 N일 변동폭 / 전일 종가
    quiet_vol: float                  # 직전 N일 평균 거래량 / 20일 평균
    week_high_break: bool             # 직전 N일 최고가 상향 돌파
    buy_ratio: float
    volume_trend: float
    high_age: int                     # 당일 고가를 마지막으로 찍은 뒤 경과 분
    regime_on: bool | None            # None = 지수 데이터 없음
    checks: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    score: int = 0
    score_detail: list = field(default_factory=list)
    entry: bool = False
    entry_reasons: list = field(default_factory=list)

    @property
    def change(self) -> float:
        return self.price / self.prev_close - 1 if self.prev_close else float("nan")

    @property
    def key(self) -> str:
        return f"{self.session:%Y%m%d}-{self.symbol}"

    def to_dict(self) -> dict:
        r = lambda v, d=2: None if v is None or v != v else round(float(v), d)  # noqa: E731
        return {
            "키": self.key, "종목코드": self.symbol, "종목명": self.name, "시장": self.market,
            "기준일": self.session.isoformat(), "판정시각": self.as_of.strftime("%H:%M"),
            "진입": self.entry, "진입근거": self.entry_reasons,
            "점수": self.score, "점수상세": self.score_detail,
            "현재가": r(self.price, 0), "등락률%": r(self.change * 100),
            "돌파선": r(self.pre_high, 0), "당일고가": r(self.day_high, 0), "VWAP": r(self.vwap, 0),
            "급증배수": r(self.surge_ratio), "급증기준일": self.surge_days,
            "일평균대비": r(self.day_volume_ratio), "거래대금": r(self.turnover, 0),
            "주간변동폭%": r(self.quiet_range * 100), "주간거래량비": r(self.quiet_vol),
            "주간고점돌파": self.week_high_break,
            "매수비중%": r(self.buy_ratio * 100, 1), "거래량추세": r(self.volume_trend),
            "고점경과분": self.high_age, "국면": self.regime_on,
            "체크": clean(self.checks), "유의사항": self.notes,
        }


def evaluate(symbol: str, name: str, market: str, bars: pd.DataFrame, daily: pd.DataFrame,
             profiles: list[pd.Series], turnover: float, cfg: FridayConfig,
             regime_on: bool | None = None) -> FridaySignal | None:
    """한 종목 판정. 후보 조건(창 거래량 급증 + 창 이전 당일 고점 돌파)을 못 넘으면 None.

    bars    : 정규장 1분봉 (판정 시각까지; `window_end` 이후 봉은 여기서 자른다)
    daily   : 일봉 (당일 봉이 섞여 있어도 된다 — 여기서 뺀다)
    profiles: 과거 영업일별 분 거래량 (index = 분 오프셋, 창 구간을 덮어야 한다)
    """
    if bars.empty:
        return None
    session = bars.index[-1].date()
    bars = session_bars(bars, session)
    bars = bars[bars.index.time <= cfg.window_end]
    if bars.empty:
        return None
    past = daily[daily.index.date < session]
    if len(past) < max(cfg.quiet_days, 20):
        return None

    as_of = bars.index[-1]
    now_off = minute_offset(as_of)
    start_off = minute_offset(pd.Timestamp.combine(session, cfg.window_start))
    if now_off <= start_off:                       # 창이 아직 열리지 않았다
        return None

    price = float(bars["close"].iloc[-1])
    prev_close = float(past["close"].iloc[-1])
    if price < cfg.min_price or turnover < cfg.min_turnover:
        return None

    # ---- 후보 조건 1: 창 구간 거래량 급증 (과거 같은 구간 평균 대비)
    window = bars[[minute_offset(t) > start_off for t in bars.index]]
    win_vol = float(window["volume"].sum())
    expected = []
    for p in profiles:
        if p.empty or p.sum() <= 0:
            continue
        v = float(p[(p.index > start_off) & (p.index <= now_off)].sum())
        if v > 0:
            expected.append(v)
    if len(expected) < cfg.min_profile_days:
        return None                                # 평소를 모르면 급증을 말할 수 없다 (근사하지 않는다)
    surge = win_vol / float(np.mean(expected))
    if not surge >= cfg.surge_min:
        return None

    # ---- 후보 조건 2: 창 이전 당일 고점 돌파
    pre = bars[[minute_offset(t) <= start_off for t in bars.index]]
    if pre.empty:
        return None
    pre_high = float(pre["high"].max())
    if not price > pre_high:
        return None

    day_high = float(bars["high"].max())
    last_high_idx = bars.index[bars["high"].to_numpy() >= day_high][-1]
    high_age = now_off - minute_offset(last_high_idx)

    # ---- 조용한 한 주 (오늘 직전 N영업일)
    wk = past.tail(cfg.quiet_days)
    avg20 = float(past["volume"].tail(20).mean())
    quiet_range = float((wk["high"].max() - wk["low"].min()) / prev_close) if prev_close else float("nan")
    quiet_vol = float(wk["volume"].mean() / avg20) if avg20 else float("nan")
    week_high_break = bool(price > float(wk["high"].max()))
    day_ratio = float(bars["volume"].sum()) / avg20 if avg20 else float("nan")

    # ---- 수급 (동시호가 제외 — 이미 15:15 이전 봉만 있다)
    flow = tick_flow(continuous_bars(bars))
    w = cfg.flow_window
    recent = flow.tail(w)
    prior = flow.iloc[-2 * w:-w] if len(flow) > w else flow.iloc[0:0]
    rtot = float(recent["buy"].sum() + recent["sell"].sum())
    buy_ratio = float(recent["buy"].sum()) / rtot if rtot else float("nan")
    ptot = float(prior["buy"].sum() + prior["sell"].sum())
    volume_trend = rtot / ptot if ptot else float("nan")
    vw = float(vwap(bars).iloc[-1])
    change = price / prev_close - 1 if prev_close else float("nan")

    checks = {
        "거래량급증": True,
        "고점돌파": True,
        "고점부근": bool(price >= day_high * cfg.near_high),
        "VWAP위": bool(price >= vw),
        "매수우위": bool(buy_ratio == buy_ratio and buy_ratio >= cfg.buy_ratio_min),
        "조용한한주": bool(quiet_range == quiet_range and quiet_range <= cfg.quiet_range_max
                       and quiet_vol == quiet_vol and quiet_vol <= cfg.quiet_vol_max),
        "과열아님": not (change == change and change >= cfg.hot_change),
        "국면우호": regime_on is not False,
    }
    notes: list[str] = []
    if regime_on is None:
        notes.append("지수 데이터 없음 — 시장 국면 미확인")
    elif regime_on is False:
        notes.append("지수가 이동평균 아래 — 주말 수급 효과가 약한 국면")
    if not checks["조용한한주"]:
        notes.append(f"직전 {cfg.quiet_days}일이 조용하지 않음 (변동폭 {quiet_range * 100:.1f}%, 거래량 {quiet_vol:.1f}배)")
    if not checks["과열아님"]:
        notes.append(f"당일 +{change * 100:.1f}% — 상한가 추격 위험")
    if high_age > 10:
        notes.append(f"당일 고가 이후 {high_age}분 경과 — 돌파 신선도 낮음")

    sig = FridaySignal(
        symbol=symbol, name=name, market=market, session=session, as_of=as_of, price=price,
        prev_close=prev_close, pre_high=pre_high, day_high=day_high, vwap=vw,
        surge_ratio=surge, surge_days=len(expected), day_volume_ratio=day_ratio, turnover=turnover,
        quiet_range=quiet_range, quiet_vol=quiet_vol, week_high_break=week_high_break,
        buy_ratio=buy_ratio, volume_trend=volume_trend, high_age=high_age,
        regime_on=regime_on, checks=checks, notes=notes,
    )
    rescore(sig, cfg)
    decide_entry(sig, cfg)
    return sig


def rescore(sig: FridaySignal, cfg: FridayConfig) -> None:
    """점수 100점. **경험칙 배점이며 백테스트로 검증되지 않았다.** (거래량 25·수축 20·신선도 15·매수 15·VWAP 10·국면 15)"""
    parts: list[tuple[str, int, int, str]] = []

    s = sig.surge_ratio
    pts = 0 if s < cfg.surge_min else 15 if s < 3 else 20 if s < 5 else 25 if s < 10 else 20
    parts.append(("창 거래량 급증", pts, 25, f"평소 대비 {s:.1f}배" + (" (과열)" if s >= 10 else "")))

    rng, vol = sig.quiet_range, sig.quiet_vol
    if rng != rng or vol != vol:
        pts, why = 0, "–"
    elif rng <= cfg.quiet_range_max * 0.6 and vol <= cfg.quiet_vol_max:
        pts, why = 20, f"변동폭 {rng * 100:.1f}% · 거래량 {vol:.1f}배"
    elif rng <= cfg.quiet_range_max and vol <= cfg.quiet_vol_max:
        pts, why = 14, f"변동폭 {rng * 100:.1f}% · 거래량 {vol:.1f}배"
    else:
        pts, why = 0, f"조용하지 않음 (변동폭 {rng * 100:.1f}% · 거래량 {vol:.1f}배)"
    parts.append(("변동성 수축", pts, 20, why))

    pts = 15 if sig.high_age <= 5 else 8 if sig.high_age <= 15 else 0
    if sig.week_high_break and pts:
        pts = min(15, pts + 3)
    parts.append(("돌파 신선도", pts, 15, f"고가 {sig.high_age}분 전" + (" · 주간 고점 돌파" if sig.week_high_break else "")))

    b = sig.buy_ratio
    pts = 0 if b != b else int(round(max(0.0, min(1.0, (b - 0.5) / 0.2)) * 15))
    parts.append(("매수 우위", pts, 15, "–" if b != b else f"최근 {cfg.flow_window}분 매수 {b * 100:.0f}%"))

    gap = sig.price / sig.vwap - 1 if sig.vwap else float("nan")
    pts = 0 if gap != gap or gap < 0 else 10 if gap >= 0.01 else 6 if gap >= 0.003 else 3
    parts.append(("VWAP 이격", pts, 10, "–" if gap != gap else f"VWAP 대비 {gap * 100:+.1f}%"))

    pts = 7 if sig.regime_on is None else 15 if sig.regime_on else 0
    parts.append(("시장 국면", pts, 15, "미확인" if sig.regime_on is None else "우호" if sig.regime_on else "이탈"))

    sig.score = int(max(0, min(100, sum(p[1] for p in parts))))
    sig.score_detail = [{"요인": n, "점수": p, "만점": m, "설명": d} for n, p, m, d in parts]


def decide_entry(sig: FridaySignal, cfg: FridayConfig) -> None:
    """후보 중 **진입 적합**을 가른다. 이 컷오프도 경험칙이며 포워드 기록으로 재산정한다."""
    reasons: list[dict] = []

    def check(ok: bool, label: str) -> bool:
        reasons.append({"항목": label, "충족": bool(ok)})
        return bool(ok)

    ok = True
    ok &= check(sig.checks["조용한한주"], f"조용한 한 주(직전 {cfg.quiet_days}일 변동폭 {cfg.quiet_range_max * 100:.0f}% 이하)")
    ok &= check(sig.checks["VWAP위"], "VWAP 위")
    ok &= check(sig.checks["매수우위"], f"매수 우위({cfg.buy_ratio_min * 100:.0f}% 이상)")
    ok &= check(sig.checks["고점부근"], "당일 고가 부근 유지")
    ok &= check(sig.checks["과열아님"], "과열 아님(상한가 추격 회피)")
    ok &= check(sig.checks["국면우호"], "시장 국면 우호(지수>MA)")
    ok &= check(sig.score >= cfg.entry_score_min, f"종합 점수 {cfg.entry_score_min}점 이상")
    sig.entry = bool(ok)
    sig.entry_reasons = reasons


# ================================================================== 포워드 기록 (P1) · 결과 채우기 (P3)
class FridayRecorder:
    """금요일 신호를 JSONL 로 쌓고, 다음 영업일 결과(시가·09:30·종가)를 따로 채운다.

    - 신호 기록 `signals.jsonl`: 종목당 **첫 포착**(kind=candidate) 과 **첫 진입 판정**(kind=entry) 한 줄씩.
      가격은 그 시각 현재가라 사실상 '그때 샀다면' 의 체결가 가정이다.
    - 결과 `outcomes.json`: {키: {다음 영업일, 시가, 09:30 가격, 종가, 고가, 저가}}. 시세 사실이라
      신호와 분리해 두고 한 번 채우면 다시 받지 않는다.
    """

    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path else None
        self.outcome_path = self.path.with_name("friday_outcomes.json") if self.path else None
        self._lock = threading.Lock()
        self._seen: set[tuple[str, str]] = set()
        if self.path and self.path.exists():
            for rec in self.records():
                self._seen.add((rec["key"], rec["kind"]))

    # -- 신호 --------------------------------------------------------------
    def record(self, signals: list[FridaySignal], cfg: FridayConfig) -> int:
        """창 안에서 나온 신호만 쌓는다. 새로 쓴 줄 수를 돌려준다."""
        if not self.path:
            return 0
        lines = []
        with self._lock:
            for s in signals:
                if not cfg.is_record_day(s.session):
                    continue
                for kind in ("candidate", "entry"):
                    if kind == "entry" and not s.entry:
                        continue
                    if (s.key, kind) in self._seen:
                        continue
                    self._seen.add((s.key, kind))
                    lines.append(json.dumps(clean({
                        "event": "friday_signal", "key": s.key, "kind": kind,
                        "session": s.session.isoformat(), "symbol": s.symbol, "name": s.name,
                        "market": s.market, "at": s.as_of.strftime("%H:%M"),
                        "early": s.as_of.time() < cfg.early_cut,
                        "price": s.price, "score": s.score, "surge": s.surge_ratio,
                        "change": s.change, "regime": s.regime_on, "checks": s.checks,
                        "recorded_at": datetime.now().isoformat(timespec="seconds"),
                    }), ensure_ascii=False))
            if lines:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
        return len(lines)

    def records(self) -> list[dict]:
        if not self.path or not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "friday_signal":
                out.append(rec)
        return out

    # -- 결과 --------------------------------------------------------------
    def outcomes(self) -> dict[str, dict]:
        if not self.outcome_path or not self.outcome_path.exists():
            return {}
        try:
            return json.loads(self.outcome_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def fill_outcomes(self, loader, today: date, now: datetime | None = None) -> int:
        """다음 영업일이 끝난 신호의 결과를 채운다. 새로 채운 건수를 돌려준다.

        다음 영업일 일봉이 아직 없거나(주말·미마감) 오늘 15:40 전이면 건너뛰고 다음에 다시 본다.
        """
        if not self.outcome_path:
            return 0
        now = now or datetime.now()
        done = self.outcomes()
        keys: dict[str, tuple[str, date]] = {}
        for rec in self.records():
            sess = date.fromisoformat(rec["session"])
            if rec["key"] not in done and sess < today:
                keys[rec["key"]] = (rec["symbol"], sess)
        filled = 0
        for key, (symbol, sess) in keys.items():
            try:
                daily = loader.ohlcv(symbol, DAILY_BARS)
                nxt = daily[daily.index.date > sess]
                if nxt.empty:
                    continue
                row = nxt.iloc[0]
                nday = nxt.index[0].date()
                if nday > today or (nday == today and now.time() < dtime(15, 40)):
                    continue
                p930 = loader.price_at(symbol, nday, 30)
                done[key] = clean({
                    "next_date": nday.isoformat(), "next_open": float(row["open"]),
                    "p0930": p930, "next_close": float(row["close"]),
                    "next_high": float(row["high"]), "next_low": float(row["low"]),
                })
                filled += 1
            except Exception as exc:
                log.warning("%s 금요일 결과 채우기 실패: %s", key, exc)
        if filled:
            with self._lock:
                self.outcome_path.parent.mkdir(parents=True, exist_ok=True)
                self.outcome_path.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
        return filled

    # -- 리포트 (P3) ---------------------------------------------------------
    def report(self, cost: float = OUTCOME_COST) -> dict:
        return build_report(self.records(), self.outcomes(), cost)


def _group_stats(rows: list[dict], cost: float) -> dict:
    """rows: 결과가 채워진 기록. 수익은 기록 시점 가격 기준."""
    if not rows:
        return {"건수": 0}

    def col(k):
        return np.array([r[k] for r in rows if r.get(k) is not None], dtype=float)

    gap, r30, rcl = col("gap"), col("r0930"), col("rclose")
    net = gap - cost
    out = {
        "건수": len(rows),
        "승률%(갭,비용후)": round(float((net > 0).mean() * 100), 1),
        "평균갭%": round(float(gap.mean() * 100), 2),
        "평균순갭%": round(float(net.mean() * 100), 2),
        "중앙값순갭%": round(float(np.median(net) * 100), 2),
        "평균09:30%": round(float(r30.mean() * 100), 2) if len(r30) else None,
        "평균종가%": round(float(rcl.mean() * 100), 2) if len(rcl) else None,
        "최악갭%": round(float(gap.min() * 100), 2),
    }
    if len(gap) >= 2:
        sd = float(net.std(ddof=1))
        out["t"] = round(float(net.mean() / (sd / np.sqrt(len(net)))), 2) if sd else None
    return out


def build_report(records: list[dict], outcomes: dict[str, dict], cost: float = OUTCOME_COST) -> dict:
    """주간 리포트. 후보 전체 / 진입 / 15:00 이전·이후 포착을 나눠 본다."""
    first: dict[str, dict] = {}
    entry: dict[str, dict] = {}
    for rec in records:
        (first if rec["kind"] == "candidate" else entry).setdefault(rec["key"], rec)

    def resolve(rec: dict) -> dict | None:
        o = outcomes.get(rec["key"])
        if not o or not rec.get("price"):
            return None
        p = rec["price"]
        return {
            "key": rec["key"], "session": rec["session"], "symbol": rec["symbol"], "name": rec["name"],
            "at": rec["at"], "early": rec.get("early"), "price": p, "score": rec.get("score"),
            "gap": o["next_open"] / p - 1,
            "r0930": o["p0930"] / p - 1 if o.get("p0930") else None,
            "rclose": o["next_close"] / p - 1 if o.get("next_close") else None,
            "low": o["next_low"] / p - 1 if o.get("next_low") else None,
            "next_date": o["next_date"],
        }

    cand = [x for x in map(resolve, first.values()) if x]
    ent = [x for x in map(resolve, entry.values()) if x]
    groups = {
        "후보 전체": _group_stats(cand, cost),
        "진입 판정": _group_stats(ent, cost),
        "후보 · 15:00 이전 포착": _group_stats([x for x in cand if x["early"]], cost),
        "후보 · 15:00 이후 포착": _group_stats([x for x in cand if not x["early"]], cost),
        "진입 · 15:00 이전 포착": _group_stats([x for x in ent if x["early"]], cost),
        "진입 · 15:00 이후 포착": _group_stats([x for x in ent if not x["early"]], cost),
    }
    by_session = []
    for sess in sorted({x["session"] for x in cand}):
        c = [x for x in cand if x["session"] == sess]
        e = [x for x in ent if x["session"] == sess]
        by_session.append({"기준일": sess, "후보": _group_stats(c, cost), "진입": _group_stats(e, cost)})
    trades = sorted(cand, key=lambda x: (x["session"], x["at"]), reverse=True)
    entered = {x["key"] for x in ent}
    for t in trades:
        t["entry"] = t["key"] in entered
    pending = sum(1 for k in first if k not in outcomes)
    weeks = len({x["session"] for x in cand})
    ready = len(ent) >= 100 or len(cand) >= 100
    return clean({
        "cost": cost, "weeks": weeks, "recorded": len(first), "recorded_entries": len(entry),
        "pending": pending, "groups": groups, "by_session": by_session,
        "trades": [{k: v for k, v in t.items()} for t in trades[:200]],
        "ready_for_review": ready,
        "note": ("표본이 100건 이상 쌓였습니다 — 실주문 검토 가능한 최소 표본을 넘었습니다." if ready else
                 f"표본 {len(cand)}건 (후보) / {len(ent)}건 (진입) — 100건 전에는 실주문을 검토하지 않습니다 "
                 "(매주 금요일 1회, 약 10~12주 필요)."),
    })


# ================================================================== 스캐너
@dataclass
class FridayReport:
    signals: list[FridaySignal]
    scanned: int
    skipped: int
    errors: int
    session: date | None = None
    blocked: list[dict] = field(default_factory=list)
    regimes: dict = field(default_factory=dict)
    recorded: int = 0


class FridayScanner:
    """거래대금 상위 → 분봉 판정 → 후보만 유의사항 조회 → (창 안이면) 기록."""

    def __init__(self, loader, cfg: FridayConfig | None = None, max_workers: int = 6,
                 recorder: FridayRecorder | None = None):
        self.loader = loader
        self.cfg = cfg or FridayConfig()
        self.max_workers = max_workers
        self.recorder = recorder

    def candidates(self) -> list[tuple[str, str, str, float]]:
        """(종목코드, 종목명, 시장, 당일 거래대금). ETF·우선주·거래정지는 뺀다."""
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
            out.append((sym, m.name, m.market, amount))
        return out

    def regimes(self) -> dict[str, bool | None]:
        """시장별 국면 (지수 종가 > MA). 지수를 못 받으면 None(미확인)."""
        out: dict[str, bool | None] = {}
        for market in ("KOSPI", "KOSDAQ"):
            if not self.cfg.use_regime:
                out[market] = None
                continue
            try:
                ser = regime_series(self.loader.index_ohlcv(market, 800), self.cfg.regime_ma)
                out[market] = bool(ser.iloc[-1]) if len(ser) else None
            except Exception as exc:
                log.warning("%s 지수 국면 조회 실패(미확인 처리): %s", market, exc)
                out[market] = None
        return out

    def scan(self) -> FridayReport:
        cands = self.candidates()
        regimes = self.regimes()
        signals: list[FridaySignal] = []
        skipped = errors = 0
        session: date | None = None
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futs = {pool.submit(self._one, s, n, m, a, regimes): s for s, n, m, a in cands}
            for f in as_completed(futs):
                try:
                    sig = f.result()
                except Exception as exc:
                    errors += 1
                    log.warning("%s 금요일 판정 실패: %s", futs[f], exc)
                    continue
                if sig is None:
                    skipped += 1
                else:
                    signals.append(sig)
                    session = session or sig.session

        signals, blocked = self._enrich(signals)
        signals.sort(key=lambda s: (0 if s.entry else 1, -s.score, -s.surge_ratio))
        recorded = self.recorder.record(signals, self.cfg) if self.recorder else 0
        log.info("금요일 스캔 완료: 후보 %d, 포착 %d (진입 %d), 제외 %d, 오류 %d, 기록 %d",
                 len(cands), len(signals), sum(s.entry for s in signals), len(blocked), errors, recorded)
        return FridayReport(signals, len(cands), skipped, errors, session, blocked, regimes, recorded)

    def _one(self, symbol: str, name: str, market: str, turnover: float,
             regimes: dict) -> FridaySignal | None:
        cfg = self.cfg
        bars = self.loader.session_minutes(symbol)
        if bars.empty:
            return None
        session = bars.index[-1].date()
        daily = self.loader.ohlcv(symbol, DAILY_BARS)
        past_days = [d for d in pd.Index(daily.index.date).unique() if d < session][-cfg.profile_days:]
        profiles = [self.loader.minute_profile(symbol, d, PROFILE_END) for d in past_days]
        return evaluate(symbol, name, market, bars, daily, profiles, turnover, cfg,
                        regimes.get(market))

    def _enrich(self, signals: list[FridaySignal]) -> tuple[list[FridaySignal], list[dict]]:
        """포착된 종목만 유의사항을 조회한다 (전수 조회 금지). 세력추정·수급과 같은 차단 목록."""
        from .config import StrategyConfig
        from .screener import WARNING_LABELS
        kept: list[FridaySignal] = []
        blocked: list[dict] = []
        blocking = set(StrategyConfig.blocking_warnings)
        notice = set(StrategyConfig.notice_warnings)
        for sig in signals:
            if self.cfg.exclude_warned:
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
            kept.append(sig)
        return kept, blocked

    def chart(self, symbol: str) -> dict:
        """상세 차트용 시계열 — 판정과 **같은 함수**로 만든다 (돌파선·창 구간 표시 포함)."""
        cfg = self.cfg
        bars = self.loader.session_minutes(symbol)
        if bars.empty:
            return {"bars": []}
        session = bars.index[-1].date()
        bars = session_bars(bars, session)
        bars = bars[bars.index.time <= cfg.window_end]
        if bars.empty:
            return {"bars": []}
        start_off = minute_offset(pd.Timestamp.combine(session, cfg.window_start))
        pre = bars[[minute_offset(t) <= start_off for t in bars.index]]
        flow = tick_flow(continuous_bars(bars))
        vw = vwap(bars)
        cvd = flow["cvd"].reindex(bars.index)
        return clean({
            "session": session.isoformat(),
            "window_start": start_off,
            "window_end": minute_offset(pd.Timestamp.combine(session, cfg.window_end)),
            "pre_high": float(pre["high"].max()) if not pre.empty else None,
            "bars": [{"t": ts.strftime("%H:%M"), "m": minute_offset(ts),
                      "o": row.open, "h": row.high, "l": row.low, "c": row.close, "v": row.volume,
                      "vwap": round(vw.iloc[i], 2), "cvd": cvd.iloc[i]}
                     for i, (ts, row) in enumerate(bars.iterrows())],
        })


# ================================================================== 서비스
@dataclass
class FridayState:
    signals: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    session: str | None = None
    scanned: int = 0
    skipped: int = 0
    errors: int = 0
    recorded: int = 0
    regimes: dict = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    duration: float = 0.0
    running: bool = False
    error: str | None = None
    in_window: bool = False
    record_day: bool = False

    def as_dict(self) -> dict:
        return {**self.__dict__}


class FridayService:
    """주기 스캔 + 상태 보관.

    기록 창(금요일 14:25~15:20)에는 `interval` 초마다 돌고, 그 밖에는 `idle_interval` 초에 한 번만
    본다 (화면용 마지막 판정 유지 + 월요일 결과 채우기). 장 밖 스캔은 가장 최근 정규장의 15:15 시점을 보여 준다.
    """

    def __init__(self, scanner: FridayScanner, interval: int = 60, idle_interval: int = 1800,
                 clock: Callable[[], datetime] = datetime.now):
        self.scanner = scanner
        self.interval = interval
        self.idle_interval = idle_interval
        self.clock = clock
        self.state = FridayState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_scan = 0.0
        self._last_fill = -1e9

    @property
    def cfg(self) -> FridayConfig:
        return self.scanner.cfg

    @property
    def recorder(self) -> FridayRecorder | None:
        return self.scanner.recorder

    def in_window(self) -> bool:
        return self.cfg.in_window(self.clock())

    def scan_once(self) -> FridayState:
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
            self.state.regimes = report.regimes
            self.state.recorded += report.recorded
            self.state.session = report.session.isoformat() if report.session else None
            self.state.scanned, self.state.skipped, self.state.errors = (
                report.scanned, report.skipped, report.errors)
            self.state.duration = round(time.monotonic() - t0, 1)
            self.state.finished_at = _now()
        except Exception as exc:
            self.state.error = f"{type(exc).__name__}: {exc}"
            self.state.finished_at = _now()
            log.exception("금요일 스캔 실패")
        finally:
            now = self.clock()
            self.state.in_window = self.cfg.in_window(now)
            # 화면이 보여 주는 판정이 기록 대상 날짜(금요일 등)의 것인지
            shown = date.fromisoformat(self.state.session) if self.state.session else now.date()
            self.state.record_day = self.cfg.is_record_day(shown)
            self._last_scan = time.monotonic()
            self.state.running = False
            self._lock.release()
        return self.state

    def due(self) -> bool:
        if self.state.finished_at is None:
            return True
        wait = self.interval if self.in_window() else self.idle_interval
        return time.monotonic() - self._last_scan >= wait

    def maybe_fill(self) -> int:
        """30분에 한 번 지난 신호의 다음 영업일 결과를 채운다."""
        if not self.recorder or time.monotonic() - self._last_fill < 1800:
            return 0
        self._last_fill = time.monotonic()
        now = self.clock()
        try:
            return self.recorder.fill_outcomes(self.scanner.loader, now.date(), now)
        except Exception:
            log.exception("금요일 결과 채우기 실패")
            return 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="friday-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.due():
                self.scan_once()
            self.maybe_fill()
            self._stop.wait(15)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
