"""과거 데이터로 전략의 승률·손익비·최대낙폭을 측정하는 백테스터.

라이브 스크리너와 **같은 탐지 엔진**(`PatternDetector.detect`)을 그대로 쓴다.
진입 이후의 청산은 아래 규칙을 그대로 시뮬레이션한다.

  진입   : 시그널 당일 '전일 고가'(= 돌파선)에 체결됐다고 가정 (+슬리피지)
  손절   : 종가가 손절가(기준봉 저가) 아래로 마감하면 다음날 시가에 청산.
           장중 저가가 손절가를 이탈하면 손절가에 체결(갭하락이면 시가에 체결).
  1차 익절: 1R 도달 시 절반 청산 후 남은 물량 손절가를 본전으로 상향
  최종 익절: 3R(설정 가능) 도달 시 전량 청산
  시간 손절: `max_hold` 영업일 경과 시 종가 청산
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field

import numpy as np
import pandas as pd

from .config import StrategyConfig
from .data_loader import DataLoader, StockMeta
from .pattern_detector import PatternDetector, Signal, enrich

log = logging.getLogger(__name__)


@dataclass
class ExitPolicy:
    """청산 규칙 파라미터."""

    take_profit_r: float = 1.0       # 1차 익절 지점 (R 배수)
    take_profit_frac: float = 0.5    # 1차 익절 비중
    final_target_r: float = 3.0      # 최종 목표 (R 배수)
    breakeven_after_tp: bool = True  # 1차 익절 후 손절선을 본전으로
    max_hold: int = 15               # 시간 손절 (영업일)
    slippage: float = 0.003          # 진입/청산 슬리피지 (0.3%)
    fee: float = 0.0015              # 왕복 수수료+세금 근사 (0.15%)


@dataclass
class Trade:
    symbol: str
    name: str
    entry_date: str
    exit_date: str
    entry: float
    stop: float
    exit_price: float
    r_multiple: float          # 수수료·슬리피지 반영 손익 (R 단위)
    pnl_pct: float
    bars_held: int
    reason: str                # 청산 사유
    base_date: str
    upper_tail: float
    base_vol_ratio: float
    adjust_days: int
    adjust_vol_ratio: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    symbols_tested: int = 0
    bars_tested: int = 0

    # ------------------------------------------------------------ 지표
    def metrics(self) -> dict:
        if not self.trades:
            return {"거래수": 0}
        r = np.array([t.r_multiple for t in self.trades])
        wins, losses = r[r > 0], r[r <= 0]
        equity = np.cumsum(r)
        peak = np.maximum.accumulate(np.concatenate([[0.0], equity]))
        mdd = float(np.max(peak - np.concatenate([[0.0], equity])))

        streak = worst_streak = 0
        for x in r:
            streak = streak + 1 if x <= 0 else 0
            worst_streak = max(worst_streak, streak)

        avg_win = float(wins.mean()) if len(wins) else 0.0
        avg_loss = float(abs(losses.mean())) if len(losses) else 0.0
        return {
            "거래수": len(r),
            "승률%": round(len(wins) / len(r) * 100, 1),
            "평균손익R": round(float(r.mean()), 3),
            "기대값R/거래": round(float(r.mean()), 3),
            "총손익R": round(float(r.sum()), 1),
            "평균이익R": round(avg_win, 2),
            "평균손실R": round(avg_loss, 2),
            "손익비": round(avg_win / avg_loss, 2) if avg_loss else float("inf"),
            "수익팩터": round(float(wins.sum() / abs(losses.sum())), 2) if len(losses) and losses.sum() else float("inf"),
            "최대낙폭R": round(mdd, 1),
            "최대연속손실": worst_streak,
            "평균보유일": round(float(np.mean([t.bars_held for t in self.trades])), 1),
            "평균손익%": round(float(np.mean([t.pnl_pct for t in self.trades])), 2),
            "검사종목": self.symbols_tested,
        }

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([t.as_dict() for t in self.trades])

    def by_bucket(self, column: str, bins: list[float], labels: list[str]) -> pd.DataFrame:
        """조건별(윗꼬리·거래량배수 등) 성과 분해 — 파라미터 튜닝 근거."""
        df = self.to_frame()
        if df.empty:
            return df
        df["구간"] = pd.cut(df[column], bins=bins, labels=labels, include_lowest=True)
        g = df.groupby("구간", observed=True)["r_multiple"]
        out = pd.DataFrame({
            "거래수": g.size(),
            "승률%": (g.apply(lambda s: (s > 0).mean() * 100)).round(1),
            "평균R": g.mean().round(3),
            "총R": g.sum().round(1),
        })
        return out.reset_index()


class Backtester:
    """유니버스 전체에 대해 시그널을 재현하고 청산까지 시뮬레이션한다."""

    def __init__(self, loader: DataLoader, cfg: StrategyConfig | None = None,
                 policy: ExitPolicy | None = None, max_workers: int = 8):
        self.loader = loader
        self.cfg = cfg or StrategyConfig()
        self.policy = policy or ExitPolicy()
        self.detector = PatternDetector(self.cfg)
        self.max_workers = max_workers

    # ------------------------------------------------------------ 실행
    def run(self, symbols: list[StockMeta] | list[str], bars: int = 750) -> BacktestResult:
        metas = [m if isinstance(m, StockMeta) else StockMeta(m, m) for m in symbols]
        result = BacktestResult()
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._run_one, m, bars): m for m in metas}
            for fut in as_completed(futures):
                meta = futures[fut]
                try:
                    trades, n_bars = fut.result()
                except Exception as exc:
                    log.warning("%s 백테스트 실패: %s", meta.symbol, exc)
                    continue
                if n_bars:
                    result.symbols_tested += 1
                    result.bars_tested += n_bars
                    result.trades.extend(trades)
        result.trades.sort(key=lambda t: t.entry_date)
        log.info("백테스트 완료: %d 종목 / %d 봉 / 거래 %d건",
                 result.symbols_tested, result.bars_tested, len(result.trades))
        return result

    def _run_one(self, meta: StockMeta, bars: int) -> tuple[list[Trade], int]:
        df = self.loader.ohlcv(meta.symbol, count=bars)
        cfg = self.cfg
        if len(df) < cfg.min_history + self.policy.max_hold + 10:
            return [], 0

        data = enrich(df, cfg)
        trades: list[Trade] = []
        pos = cfg.min_history
        # 청산까지 관찰할 기간이 남아있는 시그널만 센다.
        # (데이터 끝에 걸린 미결 거래를 포함하면 성과가 왜곡된다)
        n = len(data) - self.policy.max_hold

        # 돌파 조건(거래량 + 전일 고가)은 벡터로 미리 걸러 탐색 공간을 줄인다.
        breakout = self._breakout_mask(data)

        while pos < n:
            if not breakout[pos]:
                pos += 1
                continue
            sig = self.detector.detect(data, meta.symbol, meta.name, at=pos)
            if sig is None:
                pos += 1
                continue
            trade, exit_pos = self._simulate(data, sig, pos)
            if trade:
                trades.append(trade)
                pos = max(exit_pos + 1, pos + 1)   # 청산 전까지는 재진입하지 않음
            else:
                pos += 1
        return trades, n

    def _breakout_mask(self, data: pd.DataFrame) -> np.ndarray:
        cfg = self.cfg
        price = data["high"].to_numpy()
        thresh = data["prev_high"].to_numpy() * (1 + cfg.trigger_break_buffer)
        vol_ok = data["vol_ratio_prev"].to_numpy() >= cfg.trigger_vol_ratio
        with np.errstate(invalid="ignore"):
            return np.nan_to_num((price > thresh) & vol_ok, nan=False).astype(bool)

    # ------------------------------------------------------------ 청산
    def _simulate(self, data: pd.DataFrame, sig: Signal, pos: int) -> tuple[Trade | None, int]:
        p = self.policy
        entry = sig.entry * (1 + p.slippage)
        stop = sig.stop
        risk = entry - stop
        if risk <= 0:
            return None, pos

        tp = entry + risk * p.take_profit_r
        final = entry + risk * p.final_target_r
        realized = 0.0        # 이미 실현한 손익 (가격 단위, 비중 가중)
        remaining = 1.0
        cur_stop = stop
        n = len(data)
        last = min(pos + p.max_hold, n - 1)

        for i in range(pos, last + 1):
            row = data.iloc[i]
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])

            if i > pos and o <= cur_stop:                     # 갭하락 → 시가 청산
                return self._close(data, sig, pos, i, entry, stop, realized, remaining, o,
                                   "갭하락 손절"), i
            if l <= cur_stop:                                  # 장중 손절 이탈
                return self._close(data, sig, pos, i, entry, stop, realized, remaining, cur_stop,
                                   "손절" if cur_stop == stop else "본전 청산"), i
            if remaining == 1.0 and h >= tp:                   # 1차 익절
                realized += p.take_profit_frac * (tp - entry)
                remaining -= p.take_profit_frac
                if p.breakeven_after_tp:
                    cur_stop = entry
            if h >= final:                                     # 최종 목표
                return self._close(data, sig, pos, i, entry, stop, realized, remaining, final,
                                   "목표 도달"), i
            if i == last:                                      # 시간 손절
                return self._close(data, sig, pos, i, entry, stop, realized, remaining, c,
                                   "시간 청산"), i
        return None, pos

    def _close(self, data: pd.DataFrame, sig: Signal, pos: int, i: int, entry: float,
               stop: float, realized: float, remaining: float, price: float,
               reason: str) -> Trade:
        p = self.policy
        exit_price = price * (1 - p.slippage) if remaining else price
        gain = realized + remaining * (exit_price - entry)
        gain -= entry * p.fee                                  # 왕복 비용
        risk = entry - stop
        return Trade(
            symbol=sig.symbol, name=sig.name,
            entry_date=data.index[pos].strftime("%Y-%m-%d"),
            exit_date=data.index[i].strftime("%Y-%m-%d"),
            entry=round(entry, 2), stop=round(stop, 2), exit_price=round(exit_price, 2),
            r_multiple=round(gain / risk, 3), pnl_pct=round(gain / entry * 100, 2),
            bars_held=i - pos, reason=reason,
            base_date=sig.base.date.strftime("%Y-%m-%d"),
            upper_tail=round(sig.base.upper_tail_ratio, 3),
            base_vol_ratio=round(sig.base.volume_ratio, 1),
            adjust_days=sig.adjust_days,
            adjust_vol_ratio=round(sig.adjust_vol_ratio, 3),
        )


def format_report(result: BacktestResult, policy: ExitPolicy) -> str:
    """콘솔 리포트 문자열."""
    m = result.metrics()
    if not m.get("거래수"):
        return "거래 없음 — 기간·유니버스를 넓히거나 조건을 완화하세요."

    lines = ["", "=" * 68,
             f"백테스트 결과  (검사 {result.symbols_tested}종목 / {result.bars_tested:,}봉)",
             "=" * 68,
             f"청산 규칙: {policy.take_profit_r}R 에서 {policy.take_profit_frac:.0%} 익절"
             f"{' → 본전 스톱' if policy.breakeven_after_tp else ''}, "
             f"최종 {policy.final_target_r}R, 시간손절 {policy.max_hold}일, "
             f"슬리피지 {policy.slippage:.1%} + 비용 {policy.fee:.2%}", "-" * 68]
    order = ["거래수", "승률%", "평균손익R", "손익비", "수익팩터", "총손익R",
             "최대낙폭R", "최대연속손실", "평균보유일", "평균손익%"]
    for k in order:
        lines.append(f"  {k:<12} {m[k]}")

    df = result.to_frame()
    lines += ["-" * 68, "청산 사유별:"]
    for reason, grp in df.groupby("reason"):
        lines.append(f"  {reason:<10} {len(grp):>4}건  평균 {grp['r_multiple'].mean():+.2f}R")

    for col, bins, labels, title in [
        ("upper_tail", [0, .45, .6, .75, 1.0], ["~45%", "45~60%", "60~75%", "75%~"], "윗꼬리 비율별"),
        ("base_vol_ratio", [0, 8, 20, 50, 1e9], ["~8배", "8~20배", "20~50배", "50배~"], "기준봉 거래량별"),
        ("adjust_vol_ratio", [0, .15, .3, .45, 1e9], ["~0.15", "0.15~0.3", "0.3~0.45", "0.45~"], "조정 거래량비별"),
        ("adjust_days", [0, 3, 6, 10, 99], ["1~3일", "4~6일", "7~10일", "10일~"], "조정 기간별"),
    ]:
        b = result.by_bucket(col, bins, labels)
        if not b.empty:
            lines += ["-" * 68, f"{title}:"]
            for _, r in b.iterrows():
                lines.append(f"  {str(r['구간']):<10} {int(r['거래수']):>4}건  "
                             f"승률 {r['승률%']:>5}%  평균 {r['평균R']:+.3f}R")
    lines.append("=" * 68)
    return "\n".join(lines)
