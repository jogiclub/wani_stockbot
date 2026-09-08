"""시그널 점수화 (100점 만점).

배점은 임의로 정한 게 아니라 **3년 백테스트(2,563종목·208만 봉)에서 측정한
구간별 평균 손익(R)** 에 맞춰 정했다. 각 요인의 배점 = 그 요인이 성과를 가르는 크기,
구간별 점수 = 그 구간의 실측 평균 R 순위.

측정 표본: tuned 프로파일 + 코스닥 국면 통과 734건 (전체 평균 +0.173R)

  손절폭        ~8% +0.214R · 8~12% +0.158R · 12~18% +0.052R · 18%~ -0.152R   → 25점
  기준봉 거래량  30~50배 +0.214R · 50~100배 +0.228R · 20~30배 +0.113R · 100배~ +0.152R → 20점
  조정 거래량비  0.10~0.20 +0.315R · ~0.10 +0.111R · 0.20~0.35 +0.148R · 0.35~ +0.086R → 20점
  조정 기간      2일 +0.285R · 3일 +0.081R                                    → 15점
  윗꼬리        55~65% +0.226R · 80%~ +0.186R · 65~80% +0.153R · 45~55% +0.135R → 10점
  시장 국면      통과 +0.173R · 이탈 -0.117R                                   → 10점
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .pattern_detector import Signal

# (임계값 상한, 점수, 설명) — 값이 임계값 이하이면 해당 구간. 마지막은 상한 None.
Band = tuple[float | None, int, str]


@dataclass(frozen=True)
class Factor:
    key: str
    label: str
    max_points: int
    bands: tuple[Band, ...]
    higher_is_better: bool = False   # 표시용

    def evaluate(self, value: float | None) -> tuple[int, str]:
        if value is None or value != value:            # None / NaN
            return 0, "데이터 없음"
        for upper, pts, note in self.bands:
            if upper is None or value <= upper:
                return pts, note
        return 0, "구간 밖"


FACTORS: tuple[Factor, ...] = (
    Factor("risk_pct", "손절폭", 25, (
        (8.0,  25, "8% 이하 — 손익비 최상"),
        (12.0, 18, "8~12% — 양호"),
        (18.0,  8, "12~18% — 부담"),
        (None,  0, "18% 초과 — 실측 손실 구간"),
    )),
    Factor("base_vol_ratio", "기준봉 거래량", 20, (
        (30.0,  11, "20~30배 — 매집 강도 보통"),
        (50.0,  18, "30~50배 — 강함"),
        (100.0, 20, "50~100배 — 최상"),
        (None,  14, "100배 초과 — 과열 가능"),
    ), higher_is_better=True),
    Factor("adjust_vol_ratio", "조정 거래량비", 20, (
        (0.10, 8,  "0.10 이하 — 거래 실종(관심 부족)"),
        (0.20, 20, "0.10~0.20 — 최적 매물 소화"),
        (0.35, 11, "0.20~0.35 — 매물 다소 출회"),
        (None, 6,  "0.35 초과 — 매물 미소화"),
    )),
    Factor("adjust_days", "조정 기간", 15, (
        (2.0, 15, "2일 — 짧고 강한 눌림"),
        (3.0, 6,  "3일"),
        (6.0, 3,  "4~6일 — 실측 열위"),
        (None, 0, "7일 이상 — 실측 손실 구간"),
    )),
    Factor("upper_tail", "윗꼬리 비율", 10, (
        (0.55, 6,  "45~55%"),
        (0.65, 10, "55~65% — 최상"),
        (0.80, 7,  "65~80%"),
        (None, 8,  "80% 초과"),
    ), higher_is_better=True),
)

REGIME_FACTOR = Factor("regime", "시장 국면", 10, (
    (0.5, 0,  "지수가 이동평균 아래 — 실측 -0.117R"),
    (None, 10, "지수가 이동평균 위 — 매매 우호"),
), higher_is_better=True)

# 등급별 실측 성과 (tuned + KOSDAQ MA20, 1,230건 대상 사후 채점):
#   A+(85~) 152건 승률 69.1% 평균 +0.412R | A(75~84) 370건 60.0% +0.165R
#   B(65~74) 447건 50.1% -0.048R | C(50~64) 218건 51.4% +0.017R | D(~49) 42건 38.1% -0.260R
# → **실전 컷오프는 75점**. 65~74 구간은 승률 50%로 비용을 못 넘긴다.
ENTRY_CUTOFF = 75

GRADES = ((85, "A+", "적극 검토"), (75, "A", "검토 가능"), (65, "B", "관망 (실측 열위)"),
          (50, "C", "관망"), (0, "D", "회피"))


@dataclass
class ScoreItem:
    label: str
    points: int
    max_points: int
    value: str
    note: str

    def as_dict(self) -> dict:
        return {"항목": self.label, "점수": self.points, "만점": self.max_points,
                "값": self.value, "설명": self.note}


@dataclass
class Score:
    total: int
    grade: str
    advice: str
    items: list[ScoreItem] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"총점": self.total, "등급": self.grade, "판단": self.advice,
                "세부": [i.as_dict() for i in self.items]}


def _fmt(key: str, value: float) -> str:
    if key == "risk_pct":
        return f"{value:.1f}%"
    if key == "base_vol_ratio":
        return f"×{value:.0f}"
    if key == "adjust_days":
        return f"{value:.0f}일"
    if key == "upper_tail":
        return f"{value * 100:.0f}%"
    if key == "regime":
        return "통과" if value >= 0.5 else "이탈"
    return f"{value:.2f}"


def score_signal(sig: Signal, regime_on: bool | None = None) -> Score:
    """시그널 하나를 100점 만점으로 평가한다."""
    values = {
        "risk_pct": sig.risk_pct,
        "base_vol_ratio": sig.base.volume_ratio,
        "adjust_vol_ratio": sig.adjust_vol_ratio,
        "adjust_days": float(sig.adjust_days),
        "upper_tail": sig.base.upper_tail_ratio,
    }
    items: list[ScoreItem] = []
    total = 0
    for f in FACTORS:
        v = values.get(f.key)
        pts, note = f.evaluate(v)
        total += pts
        items.append(ScoreItem(f.label, pts, f.max_points, _fmt(f.key, v) if v is not None else "-", note))

    if regime_on is None:
        # 국면 정보가 없으면 만점 기준에서 제외하지 않고 중립(절반) 처리
        pts, note = REGIME_FACTOR.max_points // 2, "지수 데이터 없음 — 중립 처리"
        value = "-"
    else:
        pts, note = REGIME_FACTOR.evaluate(1.0 if regime_on else 0.0)
        value = _fmt("regime", 1.0 if regime_on else 0.0)
    total += pts
    items.append(ScoreItem(REGIME_FACTOR.label, pts, REGIME_FACTOR.max_points, value, note))

    grade, advice = next((g, a) for cut, g, a in GRADES if total >= cut)
    return Score(total=total, grade=grade, advice=advice, items=items)


def max_points() -> int:
    return sum(f.max_points for f in FACTORS) + REGIME_FACTOR.max_points
