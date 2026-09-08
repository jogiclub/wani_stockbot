"""점수 산정 / 시장 국면 필터 테스트."""

import pandas as pd
import pytest

from screener.config import StrategyConfig
from screener.data_loader import DataLoader
from screener.regime import MarketRegime
from screener.scoring import FACTORS, REGIME_FACTOR, max_points, score_signal
from screener.pattern_detector import PatternDetector
from tests.test_pattern_detector import inject_pattern, make_df


def make_signal(**over):
    det = PatternDetector(StrategyConfig())
    sig = det.detect(inject_pattern(make_df()), "TEST", "테스트")
    assert sig is not None
    for k, v in over.items():
        if k in ("upper_tail_ratio", "volume_ratio"):
            setattr(sig.base, k, v)
        else:
            setattr(sig, k, v)
    return sig


def test_max_is_100():
    assert max_points() == 100
    assert sum(f.max_points for f in FACTORS) + REGIME_FACTOR.max_points == 100


def test_perfect_signal_scores_100():
    sig = make_signal(upper_tail_ratio=0.60, volume_ratio=70.0,
                      adjust_days=2, adjust_vol_ratio=0.15,
                      entry=100.0, stop=95.0)          # 손절폭 5%
    score = score_signal(sig, regime_on=True)
    assert score.total == 100
    assert score.grade == "A+"


def test_poor_signal_scores_low():
    sig = make_signal(upper_tail_ratio=0.46, volume_ratio=25.0,
                      adjust_days=9, adjust_vol_ratio=0.5,
                      entry=100.0, stop=75.0)          # 손절폭 25%
    score = score_signal(sig, regime_on=False)
    assert score.total <= 25
    assert score.grade in ("C", "D")


def test_regime_costs_exactly_ten_points():
    sig = make_signal(entry=100.0, stop=95.0)
    on = score_signal(sig, regime_on=True).total
    off = score_signal(sig, regime_on=False).total
    assert on - off == 10


def test_unknown_regime_is_neutral():
    sig = make_signal(entry=100.0, stop=95.0)
    unknown = score_signal(sig, regime_on=None)
    assert unknown.total == score_signal(sig, regime_on=False).total + 5
    assert "중립" in unknown.items[-1].note


def test_breakdown_sums_to_total():
    score = score_signal(make_signal(), regime_on=True)
    assert sum(i.points for i in score.items) == score.total
    assert [i.label for i in score.items][-1] == "시장 국면"


def test_wider_stop_never_scores_higher():
    """손절폭이 넓어지면 점수는 절대 올라가지 않는다 (단조성)."""
    prev = 101
    for stop in (95.0, 90.0, 85.0, 78.0):
        pts = score_signal(make_signal(entry=100.0, stop=stop), regime_on=True).total
        assert pts <= prev
        prev = pts


# ------------------------------------------------------------------ 국면
class IndexLoader(DataLoader):
    def __init__(self, closes):
        idx = pd.bdate_range(end="2026-09-01", periods=len(closes))
        self.df = pd.DataFrame({"open": closes, "high": closes, "low": closes,
                                "close": closes, "volume": [1.0] * len(closes)}, index=idx)

    def universe(self):
        return []

    def ohlcv(self, symbol, count=260):
        return self.df

    def index_ohlcv(self, symbol="KOSDAQ", count=800):
        return self.df


def test_regime_on_when_index_above_ma():
    reg = MarketRegime(IndexLoader(list(range(100, 160))), "KOSDAQ", 20)
    assert reg.available and reg.is_on() is True
    assert reg.status()["on"] is True


def test_regime_off_when_index_below_ma():
    reg = MarketRegime(IndexLoader(list(range(160, 100, -1))), "KOSDAQ", 20)
    assert reg.is_on() is False
    assert reg.status()["gap_pct"] < 0


def test_regime_missing_data_passes_through():
    class Empty(IndexLoader):
        def index_ohlcv(self, symbol="KOSDAQ", count=800):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    reg = MarketRegime(Empty([1.0]), "KOSDAQ", 20)
    assert not reg.available and reg.is_on() is True
    assert reg.status() == {"available": False}


def test_regime_filter_dates_aligns_past_values():
    reg = MarketRegime(IndexLoader(list(range(100, 160))), "KOSDAQ", 20)
    dates = pd.Series(reg.index.index[-3:])
    assert reg.filter_dates(dates).all()
