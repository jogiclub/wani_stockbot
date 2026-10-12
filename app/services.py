from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models import (
    Advice,
    KRW_100_MILLION,
    RecommendationRecord,
    SourceSnapshot,
    StockEvaluation,
    StockSnapshot,
)
from app.repositories import FileRepository


SOURCE_PRIORITY = {
    "KRX": 0,
    "FSS": 1,
    "NAVER": 2,
    "BROKER": 3,
}


@dataclass
class CanonicalSnapshot:
    source: SourceSnapshot
    warnings: list[str]
    source_summary: str


class SourceVerifier:
    def verify(self, snapshot: StockSnapshot) -> CanonicalSnapshot:
        grouped = sorted(snapshot.source_snapshots, key=lambda item: SOURCE_PRIORITY[item.source])
        canonical = grouped[0]
        warnings: list[str] = []

        unique_pairs = {
            (
                item.foreign_net_buy_3d_krw,
                item.institution_net_buy_3d_krw,
            )
            for item in grouped
        }
        if len(unique_pairs) > 1:
            warnings.append("\uc218\uae09 \uc218\uce58 \ubd88\uc77c\uce58 \ubc1c\uc0dd, KRX \uc6b0\uc120 \uae30\uc900\uc73c\ub85c \uc801\uc6a9")

        summary = " / ".join(item.source for item in grouped)
        return CanonicalSnapshot(source=canonical, warnings=warnings, source_summary=summary)


class StockSelector:
    def __init__(self, repository: FileRepository, verifier: SourceVerifier | None = None) -> None:
        self.repository = repository
        self.verifier = verifier or SourceVerifier()

    def run(self, snapshots: list[StockSnapshot], run_date: str | None = None) -> RecommendationRecord:
        run_date = run_date or datetime.now().strftime("%Y-%m-%d")
        recent_codes = self.repository.load_recent_recommendation_codes(days=3)
        evaluations = [self.evaluate(snapshot, recent_codes) for snapshot in snapshots]

        selected = [item for item in evaluations if item.selected]
        selected.sort(key=lambda item: item.score, reverse=True)
        final_selected_codes = {item.code for item in selected[:5]}

        normalized_candidates: list[StockEvaluation] = []
        for evaluation in sorted(evaluations, key=lambda item: item.score, reverse=True):
            normalized_candidates.append(
                evaluation.model_copy(
                    update={"selected": evaluation.code in final_selected_codes and evaluation.selected}
                )
            )

        record = RecommendationRecord(
            generated_at=datetime.now(),
            run_date=run_date,
            candidates=normalized_candidates,
        )
        self.repository.save_recommendation(record)
        return record

    def evaluate(self, snapshot: StockSnapshot, recent_codes: set[str]) -> StockEvaluation:
        verified = self.verifier.verify(snapshot)
        reasons: list[str] = []
        warnings = list(verified.warnings)
        selected = True
        score = 0

        if snapshot.instrument_type in {snapshot.instrument_type.ETF, snapshot.instrument_type.ETN}:
            selected = False
            reasons.append("ETF/ETN \uc81c\uc678")
        if snapshot.is_managed:
            selected = False
            reasons.append("\uad00\ub9ac\uc885\ubaa9 \uc81c\uc678")
        if snapshot.is_halted:
            selected = False
            reasons.append("\uac70\ub798\uc815\uc9c0 \uc81c\uc678")

        if snapshot.foreign_positive_days_last_3 < 2 or snapshot.foreign_net_buy_3d_krw < 100 * KRW_100_MILLION:
            selected = False
            reasons.append("\uc678\uad6d\uc778 \uc218\uae09 \uae30\uc900 \ubbf8\ub2ec")
        else:
            reasons.append("\uc678\uad6d\uc778 \uc218\uae09 \uae30\uc900 \ucda9\uc871")

        institution_support = (
            snapshot.institution_net_buy_3d_krw >= 30 * KRW_100_MILLION
            or snapshot.institution_rank_percentile <= 0.2
        )
        if not institution_support:
            selected = False
            reasons.append("\uae30\uad00 \uc218\uae09 \uae30\uc900 \ubbf8\ub2ec")
        else:
            reasons.append("\uae30\uad00 \uc218\uae09 \uae30\uc900 \ucda9\uc871")

        if snapshot.avg_trading_value_last_3d_krw < 300 * KRW_100_MILLION:
            selected = False
            reasons.append("\uc720\ub3d9\uc131 \uae30\uc900 \ubbf8\ub2ec")
        else:
            reasons.append("\uc720\ub3d9\uc131 \uae30\uc900 \ucda9\uc871")

        market_cap_cut = 5000 * KRW_100_MILLION if snapshot.market.value == "KOSPI" else 7000 * KRW_100_MILLION
        if snapshot.market_cap_krw < market_cap_cut:
            selected = False
            reasons.append("\uc2dc\uac00\ucd1d\uc561 \uae30\uc900 \ubbf8\ub2ec")
        else:
            reasons.append("\uc2dc\uac00\ucd1d\uc561 \uae30\uc900 \ucda9\uc871")

        within_high_band = snapshot.current_price <= snapshot.highest_price_20d * 1.03
        above_ma = snapshot.current_price >= snapshot.moving_average_20
        if not above_ma or not within_high_band:
            selected = False
            reasons.append("\uac00\uaca9 \uc704\uce58 \uae30\uc900 \ubbf8\ub2ec")
        else:
            reasons.append("\uac00\uaca9 \uc704\uce58 \uae30\uc900 \ucda9\uc871")

        strong_dual_buy = snapshot.institution_rank_percentile <= 0.1 and snapshot.foreign_net_buy_3d_krw >= 200 * KRW_100_MILLION
        if snapshot.price_change_5d_pct >= 30 and not strong_dual_buy:
            selected = False
            reasons.append("\ub2e8\uae30 \uacfc\uc5f4 \uc81c\uc678")
        elif snapshot.price_change_5d_pct >= 30:
            reasons.append("\uacfc\uc5f4 \uc608\uc678 \ud5c8\uc6a9")

        if snapshot.code in recent_codes:
            selected = False
            reasons.append("\ucd5c\uadfc 3\uc77c \ucd94\ucc9c \uc774\ub825 \uc81c\uc678")

        if snapshot.foreign_positive_streak_last_3 == 3:
            score += 2
        if snapshot.foreign_net_buy_3d_krw >= 200 * KRW_100_MILLION:
            score += 2
        if snapshot.institution_net_buy_3d_krw > 0:
            score += 2

        growth_ratio = self._trading_value_growth(snapshot)
        if growth_ratio >= 0.2:
            score += 1
            reasons.append("\uac70\ub798\ub300\uae08 \uc99d\uac00\uc728 20% \uc774\uc0c1")

        if above_ma:
            score += 1
            reasons.append("20\uc77c\uc120 \uc704 \uc720\uc9c0")

        pullback_pct = self._pullback_pct(snapshot)
        if -6 <= pullback_pct <= -2:
            score += 1
            reasons.append("\uace0\uc810 \ub300\ube44 \ub20c\ub9bc \uad6c\uac04")

        advice = self._build_advice(snapshot, pullback_pct)
        if snapshot.foreign_sell_streak_days >= 2:
            warnings.append(Advice.SELL_CONSIDER.value)
        if snapshot.institution_turned_to_sell:
            warnings.append(Advice.WARNING.value)
        if snapshot.volume_spike and snapshot.price_change_5d_pct >= 20:
            warnings.append(Advice.DO_NOT_BUY.value)

        return StockEvaluation(
            code=snapshot.code,
            name=snapshot.name,
            score=score,
            selected=selected,
            reasons=reasons,
            warnings=warnings,
            advice=advice,
            source_summary=verified.source_summary,
            foreign_trend=f"1\uc77c {self._format_krw(snapshot.foreign_net_buy_1d_krw)}, 3\uc77c {self._format_krw(snapshot.foreign_net_buy_3d_krw)}",
            institution_trend=f"1\uc77c {self._format_krw(snapshot.institution_net_buy_1d_krw)}, 3\uc77c {self._format_krw(snapshot.institution_net_buy_3d_krw)}",
            trading_value_summary=f"3\uc77c \ud3c9\uade0 {self._format_krw(snapshot.avg_trading_value_last_3d_krw)}",
            price_position_summary=f"20\uc77c\uc120 {snapshot.moving_average_20:.0f} / 20\uc77c \uace0\uc810 {snapshot.highest_price_20d:.0f}",
        )

    def _build_advice(self, snapshot: StockSnapshot, pullback_pct: float) -> Advice:
        if snapshot.volume_spike and snapshot.price_change_5d_pct >= 20:
            return Advice.DO_NOT_BUY
        if -6 <= pullback_pct <= -2:
            return Advice.BUY_ON_PULLBACK
        if snapshot.current_price >= snapshot.highest_price_20d * 0.98:
            return Advice.WAIT_FOR_BREAKOUT
        return Advice.WATCH

    @staticmethod
    def _trading_value_growth(snapshot: StockSnapshot) -> float:
        if snapshot.avg_trading_value_prev_3d_krw <= 0:
            return 0.0
        return (snapshot.avg_trading_value_last_3d_krw - snapshot.avg_trading_value_prev_3d_krw) / snapshot.avg_trading_value_prev_3d_krw

    @staticmethod
    def _pullback_pct(snapshot: StockSnapshot) -> float:
        return ((snapshot.current_price / snapshot.highest_price_20d) - 1) * 100

    @staticmethod
    def _format_krw(value: int) -> str:
        return f"{value / KRW_100_MILLION:.1f}\uc5b5"

