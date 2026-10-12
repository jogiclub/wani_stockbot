"""금요일 마감 돌파 매매 — P0 일봉 대리 백테스트.

분봉 이력은 토스 API 로 길게 받을 수 없으므로, 가설의 **경제성(주말을 넘긴 갭이 비용을 넘는가,
그리고 '금요일'이라는 전제가 실제로 차이를 만드는가)** 만 일봉으로 먼저 본다.

대리 신호 (일봉)
  - 직전 `quiet_days`(4) 영업일 변동폭 ≤ `quiet_range_max`, 평균 거래량 ≤ 20일 평균 × `quiet_vol_max` (조용한 주)
  - 당일 거래량 ≥ 20일 평균 × `vol_mult_min` (shift(1) — 미래 정보 차단)
  - 종가가 당일 고가 부근(`close_near_high`) AND 직전 `quiet_days` 일 고가 상향 돌파 (마감 돌파 대리)
수익 : 신호일 **종가 → 다음 영업일 시가** 갭 (비용·슬리피지 차감)

비교군 (필수)
  - 같은 신호가 **주중**(다음 영업일이 같은 주)에 난 경우의 익일 시가 갭
  - 신호 없는 모든 주말 종가 → 월요일 시가 / 모든 주중 종가 → 익일 시가

⚠ 한계 — 결과표에 반드시 병기한다
  - 일봉 '고가 부근 마감' 은 14:30~15:15 에 거래량이 터지며 고점을 돌파한 종목과 다르다.
    마감 동시호가(15:20~15:30)에서 종가가 밀려 올라간 종목도 섞여 있고, 실제 진입가는
    종가가 아니라 동시호가 이전 가격이다. 진입가를 종가로 놓으면 낙관 편향이다.
  - 같은 날짜 신호는 시장 전체 움직임을 공유해 독립이 아니다 → 종목 단위 t 값은 과대하다.
    그래서 **날짜별 평균**의 t 값(`날짜t`)을 함께 낸다. 판정은 날짜t 를 기준으로 한다.
  - 금요일 효과가 주중 대조군과 다르지 않으면 '금요일' 이라는 전제를 버려야 한다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .data_loader import candles_to_df

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProxyParams:
    """대리 백테스트 파라미터 (라이브 `FridayConfig` 와 의미를 맞춘다)."""

    quiet_days: int = 4              # 조용한 주 판단 기간 (직전 N영업일)
    quiet_range_max: float = 0.12    # (N일 최고가-최저가)/전일 종가 상한
    quiet_vol_max: float = 1.0       # N일 평균 거래량 / 20일 평균 상한
    vol_mult_min: float = 2.0        # 당일 거래량 / 20일 평균 하한
    close_near_high: float = 0.02    # 종가 ≥ 당일 고가 × (1 - 이 값)
    max_change: float = 0.20         # 당일 등락률 상한 (상한가 추격 제외)
    min_turnover: float = 1e9        # 20일 평균 거래대금 하한 (유동성)
    min_price: float = 1_000.0
    slippage: float = 0.003          # 한쪽 슬리피지 (기존 ExitPolicy 와 동일)
    fee: float = 0.0015              # 왕복 수수료·세금

    @property
    def cost(self) -> float:
        """왕복 비용 = 수수료 + 진입·청산 슬리피지."""
        return self.fee + 2 * self.slippage


def proxy_frame(df: pd.DataFrame, p: ProxyParams) -> pd.DataFrame:
    """한 종목 일봉 → 봉별 (week_end, signal, gap) 프레임. 마지막 봉은 익일이 없어 뺀다."""
    if len(df) < 40:
        return pd.DataFrame()
    o, h, l, c, v = (df[k] for k in ("open", "high", "low", "close", "volume"))
    avg20 = v.rolling(20, min_periods=20).mean().shift(1)
    k = p.quiet_days
    prior_hi = h.rolling(k).max().shift(1)
    prior_lo = l.rolling(k).min().shift(1)
    quiet_range = (prior_hi - prior_lo) / c.shift(1)
    quiet_vol = v.rolling(k).mean().shift(1) / avg20
    vol_ratio = v / avg20
    chg = c / c.shift(1) - 1

    sig = (
        (quiet_range <= p.quiet_range_max)
        & (quiet_vol <= p.quiet_vol_max)
        & (vol_ratio >= p.vol_mult_min)
        & (c >= h * (1 - p.close_near_high))
        & (c > prior_hi)
        & (chg > 0) & (chg <= p.max_change)
        & (c >= p.min_price)
        & (c * avg20 >= p.min_turnover)
    )
    nxt_open = o.shift(-1)
    nxt_date = pd.Series(df.index, index=df.index).shift(-1)
    # '주말을 넘기는 날' = 다음 영업일이 다른 ISO 주에 있는 날 (금요일, 금요일 휴장이면 목요일)
    cur_iso = df.index.isocalendar()
    nxt_iso = pd.DatetimeIndex(nxt_date).isocalendar()
    week_end = ((cur_iso["year"].to_numpy() != nxt_iso["year"].to_numpy())
                | (cur_iso["week"].to_numpy() != nxt_iso["week"].to_numpy()))
    out = pd.DataFrame({
        "date": df.index,
        "week_end": week_end,
        "signal": sig.fillna(False).to_numpy(),
        "gap": (nxt_open / c - 1).to_numpy(),
    })
    out = out[nxt_open.notna().to_numpy() & nxt_date.notna().to_numpy()]
    return out.dropna(subset=["gap"])


def _stats(gap: pd.Series, cost: float) -> dict:
    n = len(gap)
    if n == 0:
        return {"건수": 0}
    net = gap - cost
    sd = float(net.std(ddof=1)) if n > 1 else float("nan")
    return {
        "건수": n,
        "승률%": round(float((net > 0).mean() * 100), 1),
        "평균갭%": round(float(gap.mean() * 100), 3),
        "평균순수익%": round(float(net.mean() * 100), 3),
        "중앙값순%": round(float(net.median() * 100), 3),
        "표준편차%": round(sd * 100, 2) if sd == sd else None,
        "t(종목)": round(float(net.mean() / (sd / np.sqrt(n))), 2) if sd and sd == sd else None,
    }


def _date_t(sub: pd.DataFrame, cost: float) -> tuple[int, float | None]:
    """날짜별 평균 순수익의 t 값 — 같은 날 신호끼리의 상관을 제거한다."""
    if sub.empty:
        return 0, None
    daily = (sub["gap"] - cost).groupby(sub["date"]).mean()
    n = len(daily)
    if n < 3 or daily.std(ddof=1) == 0:
        return n, None
    return n, round(float(daily.mean() / (daily.std(ddof=1) / np.sqrt(n))), 2)


def _welch(a: pd.Series, b: pd.Series) -> float | None:
    if len(a) < 3 or len(b) < 3:
        return None
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    if va + vb == 0:
        return None
    return round(float((a.mean() - b.mean()) / np.sqrt(va + vb)), 2)


# 판정 기준 (코드에 박아 두고 결과표에도 함께 찍는다)
PASS_RULE = ("통과: ①주말 신호 평균 순수익 > 0 ②날짜t ≥ 2.0 ③주중 신호 대비 우위(Welch t ≥ 1.65) "
             "④연도별로 순수익 부호가 같은 해가 2/3 이상")


@dataclass
class ProxyResult:
    params: ProxyParams
    groups: pd.DataFrame
    by_year: pd.DataFrame
    by_regime: pd.DataFrame
    sensitivity: pd.DataFrame
    summary: dict
    symbols: int
    bars: int

    def verdict(self) -> str:
        return self.summary["판정"]

    def to_text(self) -> str:
        pd.set_option("display.width", 200)
        pd.set_option("display.max_columns", 30)
        lines = [
            f"검사 종목 {self.symbols:,} · 봉 {self.bars:,} · 왕복 비용 {self.params.cost * 100:.2f}%"
            f" (수수료 {self.params.fee * 100:.2f}% + 슬리피지 {self.params.slippage * 100:.1f}%×2)",
            "", "== 그룹별 (수익 = 신호일 종가 → 다음 영업일 시가) ==", self.groups.to_string(index=False),
            "", "== 주말 신호 연도별 ==", self.by_year.to_string(index=False),
            "", "== 주말 신호 시장 국면별 (KOSDAQ MA20) ==", self.by_regime.to_string(index=False),
            "", "== 주말 신호 비용 민감도 (왕복 비용을 바꿔 가며) ==", self.sensitivity.to_string(index=False),
            f"   손익분기 왕복 비용 = {self.summary['손익분기비용%']}% (평균 갭)",
            "", f"주말−주중 신호 평균 차이 {self.summary['주말-주중 차이%']}%p (Welch t {self.summary['주말-주중 t']})",
            f"주말 신호 − 신호 없는 주말 {self.summary['신호-기본 차이%']}%p (Welch t {self.summary['신호-기본 t']})",
            "", f"판정: {self.summary['판정']}", f"  {self.summary['판정근거']}", f"  기준 — {PASS_RULE}",
            "", "※ 일봉 '고가 부근 마감' 대리이며 실제 14:30~15:15 돌파·진입가와 다릅니다 (모듈 docstring 참고).",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"params": asdict(self.params), "groups": self.groups.to_dict("records"),
                "by_year": self.by_year.to_dict("records"),
                "by_regime": self.by_regime.to_dict("records"),
                "sensitivity": self.sensitivity.to_dict("records"),
                "summary": self.summary, "symbols": self.symbols, "bars": self.bars}


def analyze(frames: list[pd.DataFrame], p: ProxyParams, regime: pd.Series | None = None,
            symbols: int | None = None) -> ProxyResult:
    """종목별 프레임을 합쳐 그룹 통계·판정을 만든다. `regime` = 날짜 → 국면 on(bool)."""
    frames = [f for f in frames if not f.empty]
    all_ = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["date", "week_end", "signal", "gap"])
    all_["year"] = pd.DatetimeIndex(all_["date"]).year
    if regime is not None and len(regime):
        r = regime.copy()
        r.index = pd.DatetimeIndex(r.index).normalize()
        all_["regime"] = (r.reindex(pd.DatetimeIndex(all_["date"]).normalize())
                          .ffill().fillna(True).to_numpy().astype(bool))
    else:
        all_["regime"] = True
    cost = p.cost

    masks = {
        "신호 · 주말 (가설)": all_["signal"] & all_["week_end"],
        "신호 · 주중 (대조군)": all_["signal"] & ~all_["week_end"],
        "전체 · 주말 (기본)": all_["week_end"],
        "전체 · 주중 (기본)": ~all_["week_end"],
    }
    rows = []
    for name, m in masks.items():
        sub = all_[m]
        nd, dt = _date_t(sub, cost)
        rows.append({"그룹": name, **_stats(sub["gap"], cost), "날짜수": nd, "날짜t": dt})
    groups = pd.DataFrame(rows)

    hyp = all_[masks["신호 · 주말 (가설)"]]
    ctl = all_[masks["신호 · 주중 (대조군)"]]
    base = all_[masks["전체 · 주말 (기본)"] & ~all_["signal"]]

    yrows = []
    for y, sub in hyp.groupby("year"):
        nd, dt = _date_t(sub, cost)
        yrows.append({"연도": int(y), **_stats(sub["gap"], cost), "날짜수": nd, "날짜t": dt})
    by_year = pd.DataFrame(yrows)
    rrows = []
    for on, sub in hyp.groupby("regime"):
        nd, dt = _date_t(sub, cost)
        rrows.append({"국면": "우호(지수>MA20)" if on else "이탈", **_stats(sub["gap"], cost),
                      "날짜수": nd, "날짜t": dt})
    by_regime = pd.DataFrame(rrows)

    srows = []
    for c_ in (0.0, 0.003, 0.005, 0.0075, 0.01):
        nd_, dt_ = _date_t(hyp, c_)
        net_ = hyp["gap"] - c_
        srows.append({"왕복비용%": round(c_ * 100, 2),
                      "평균순수익%": round(float(net_.mean() * 100), 3) if len(net_) else None,
                      "승률%": round(float((net_ > 0).mean() * 100), 1) if len(net_) else None,
                      "날짜t": dt_})
    sensitivity = pd.DataFrame(srows)

    hyp_net, ctl_net, base_net = hyp["gap"] - cost, ctl["gap"] - cost, base["gap"] - cost
    nd, dt = _date_t(hyp, cost)
    welch_ctl, welch_base = _welch(hyp_net, ctl_net), _welch(hyp_net, base_net)
    year_pos = (by_year["평균순수익%"] > 0).mean() if len(by_year) else 0.0
    mean_net = float(hyp_net.mean()) if len(hyp_net) else float("nan")

    checks = {
        "평균순수익>0": bool(mean_net == mean_net and mean_net > 0),
        "날짜t≥2": bool(dt is not None and dt >= 2.0),
        "주중대비우위": bool(welch_ctl is not None and welch_ctl >= 1.65),
        "연도일관": bool(len(by_year) and year_pos >= 2 / 3),
    }
    if len(hyp) < 30:
        verdict, why = "표본부족", f"주말 신호 {len(hyp)}건 — 30건 미만이라 판단하지 않는다"
    elif all(checks.values()):
        verdict, why = "통과", "4개 기준 모두 충족 — P1(분봉 포워드 기록)로 진행"
    elif (not checks["평균순수익>0"] and float(hyp["gap"].mean()) > 0
          and welch_ctl is not None and welch_ctl >= 1.65
          and welch_base is not None and welch_base >= 1.65):
        verdict, why = "조건부", (
            f"총수익(갭)은 주중 신호·신호 없는 주말보다 유의하게 높지만 평균 {hyp['gap'].mean() * 100:.2f}% 라 "
            f"왕복 비용 {cost * 100:.2f}% 에서는 순손실. 손익분기 비용 {hyp['gap'].mean() * 100:.2f}% 이하일 때만 성립 "
            "— P1 포워드 기록으로 실제 체결 비용과 갭 감소 추세(연도별)를 확인")
    elif not checks["평균순수익>0"]:
        verdict, why = "기각", "비용을 빼면 평균이 0 이하 — 이 가설은 일봉 대리로는 성립하지 않는다"
    elif not checks["주중대비우위"]:
        verdict, why = "재정의", ("주말 신호가 주중 신호보다 낫다는 증거가 없다 — '금요일' 전제를 버리고 "
                               "일반 '마감 강세 → 익일 갭' 전략으로 다시 정의")
    else:
        verdict, why = "부분통과", "일부 기준 미달 — 미달 항목: " + ", ".join(k for k, ok in checks.items() if not ok)
    summary = {
        "주말신호건수": int(len(hyp)), "주말-주중 차이%": round(float(hyp_net.mean() - ctl_net.mean()) * 100, 3)
        if len(hyp_net) and len(ctl_net) else None,
        "주말-주중 t": welch_ctl,
        "신호-기본 차이%": round(float(hyp_net.mean() - base_net.mean()) * 100, 3)
        if len(hyp_net) and len(base_net) else None,
        "신호-기본 t": welch_base, "날짜t": dt, "기준충족": checks,
        "손익분기비용%": round(float(hyp["gap"].mean()) * 100, 3) if len(hyp) else None,
        "판정": verdict, "판정근거": why,
    }
    return ProxyResult(p, groups, by_year, by_regime, sensitivity, summary,
                       symbols if symbols is not None else len(frames), len(all_))


def load_cached_daily(cache_dir: str | Path, bars: int = 880) -> dict[str, pd.DataFrame]:
    """`cache/candles/{종목}_{bars}.json` 을 TTL 과 상관없이 읽는다 (오프라인 백테스트용)."""
    out: dict[str, pd.DataFrame] = {}
    for path in sorted((Path(cache_dir) / "candles").glob(f"*_{bars}.json")):
        try:
            df = candles_to_df(json.loads(path.read_text()))
        except Exception as exc:                                  # 깨진 캐시 파일은 건너뛴다
            log.warning("%s 읽기 실패: %s", path.name, exc)
            continue
        if not df.empty:
            out[path.name.split("_")[0]] = df
    return out


def regime_series(index_df: pd.DataFrame, ma: int = 20) -> pd.Series:
    """지수 일봉 → 날짜별 '종가 > MA' 불리언 (MarketRegime 과 같은 정의)."""
    if index_df.empty:
        return pd.Series(dtype=bool)
    m = index_df["close"].rolling(ma, min_periods=ma).mean()
    return (index_df["close"] > m).dropna()


def run_proxy(daily: dict[str, pd.DataFrame], p: ProxyParams | None = None,
              index_df: pd.DataFrame | None = None) -> ProxyResult:
    p = p or ProxyParams()
    frames = [proxy_frame(df, p) for df in daily.values()]
    reg = regime_series(index_df) if index_df is not None else None
    return analyze(frames, p, reg, symbols=len(daily))


def run_proxy_from_cache(cache_dir: str | Path, p: ProxyParams | None = None,
                         bars: int = 880) -> ProxyResult:
    cache_dir = Path(cache_dir)
    daily = load_cached_daily(cache_dir, bars)
    idx_path = cache_dir / "index_KOSDAQ_800.json"
    index_df = candles_to_df(json.loads(idx_path.read_text())) if idx_path.exists() else None
    return run_proxy(daily, p, index_df)


__all__ = ["ProxyParams", "ProxyResult", "proxy_frame", "analyze", "run_proxy",
           "run_proxy_from_cache", "load_cached_daily", "regime_series", "PASS_RULE"]
