"""백테스터 / 경고종목 필터 테스트."""

import numpy as np
import pandas as pd
import pytest

from screener.backtest import Backtester, ExitPolicy
from screener.config import StrategyConfig
from screener.data_loader import DataLoader, StockMeta
from screener.screener import Screener
from tests.test_pattern_detector import inject_pattern, make_df


class StubLoader(DataLoader):
    """지정한 DataFrame 과 경고 목록을 그대로 돌려주는 테스트용 로더."""

    def __init__(self, frames: dict[str, pd.DataFrame], warnings: dict[str, list] | None = None):
        self.frames = frames
        self._warnings = warnings or {}

    def universe(self):
        return [StockMeta(s, s, "KOSDAQ", 1e7) for s in self.frames]

    def ohlcv(self, symbol, count=260):
        return self.frames[symbol].tail(count)

    def warnings(self, symbol):
        return self._warnings.get(symbol, [])

    def meta(self, symbol):
        return StockMeta(symbol, symbol, "KOSDAQ", 1e7)


@pytest.fixture
def cfg():
    return StrategyConfig(min_avg_turnover=0.0, max_market_cap=0.0, min_price=0.0)


def pattern_then(after: str, n_after: int = 20, **kw):
    """기준봉→조정→돌파 패턴 뒤에 후속 흐름 `n_after` 봉을 이어 붙인다.

    after: "win"  → 손절폭의 4배까지 상승 (목표 도달)
           "loss" → 손절가 아래로 하락 (손절)
           "flat" → 제자리 (시간 청산)
    """
    d = inject_pattern(make_df(200))          # 마지막 봉이 돌파일
    entry = float(d["high"].iloc[-2])         # 전일 고가 = 진입가
    stop = float(d["low"].iloc[-7])           # 기준봉 저가 = 손절가
    risk = entry - stop
    target = {"win": entry + risk * 4, "loss": stop - risk * 0.5, "flat": entry}[after]

    dates = pd.bdate_range(start=d.index[-1] + pd.Timedelta(days=1), periods=n_after)
    # 시간 손절(max_hold)보다 먼저 목표/손절에 닿도록 6봉 안에 도달시킨 뒤 횡보시킨다.
    ramp = np.linspace(float(d["close"].iloc[-1]), target, 7)[1:]
    path = np.concatenate([ramp, np.full(n_after - len(ramp), target)])
    tail = pd.DataFrame({
        "open": path, "high": path * 1.002, "low": path * 0.998, "close": path,
        "volume": np.full(n_after, float(d["volume"].iloc[-1]) * 0.5),
    }, index=dates)
    return pd.concat([d, tail])


def run_bt(df, cfg, **policy_kw):
    policy = ExitPolicy(max_hold=10, slippage=0.0, fee=0.0, **policy_kw)
    return Backtester(StubLoader({"TEST": df}), cfg, policy, max_workers=1).run(["TEST"], bars=260)


def test_winning_trade_hits_target(cfg):
    res = run_bt(pattern_then("win"), cfg)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.reason == "목표 도달"
    assert t.r_multiple == pytest.approx(2.0, abs=0.05)   # 1R 절반 + 3R 절반


def test_losing_trade_hits_stop(cfg):
    res = run_bt(pattern_then("loss"), cfg)
    assert len(res.trades) == 1
    assert res.trades[0].reason in ("손절", "갭하락 손절")
    assert res.trades[0].r_multiple < 0


def test_flat_trade_times_out(cfg):
    res = run_bt(pattern_then("flat"), cfg)
    assert len(res.trades) == 1
    assert res.trades[0].reason == "시간 청산"
    assert res.trades[0].bars_held == 10


def test_unresolved_trade_at_data_end_excluded(cfg):
    """청산 관찰 기간이 안 남은 마지막 봉 시그널은 거래로 세지 않는다."""
    res = run_bt(inject_pattern(make_df(220)), cfg)
    assert res.trades == []


def test_metrics_shape(cfg):
    res = run_bt(pattern_then("win"), cfg)
    m = res.metrics()
    assert m["거래수"] == 1 and m["승률%"] == 100.0
    assert "최대낙폭R" in m and "손익비" in m


def test_metrics_empty():
    from screener.backtest import BacktestResult
    assert BacktestResult().metrics() == {"거래수": 0}


# ------------------------------------------------------------- 경고종목 필터
def _signal_frames():
    return {"AAA": inject_pattern(make_df()), "BBB": inject_pattern(make_df(seed=7))}


def test_warned_symbol_is_blocked(cfg):
    loader = StubLoader(_signal_frames(),
                        warnings={"AAA": [{"warningType": "INVESTMENT_WARNING"}]})
    report = Screener(loader, cfg, max_workers=1).scan()
    assert [s.symbol for s in report.signals] == ["BBB"]
    assert report.blocked == [{"종목코드": "AAA", "종목명": "AAA", "사유": "투자경고"}]


def test_vi_is_noted_not_blocked(cfg):
    loader = StubLoader(_signal_frames(), warnings={"AAA": [{"warningType": "VI_STATIC"}]})
    report = Screener(loader, cfg, max_workers=1).scan()
    assert {s.symbol for s in report.signals} == {"AAA", "BBB"}
    aaa = next(s for s in report.signals if s.symbol == "AAA")
    assert aaa.notes == ["VI(정적)"]
    assert aaa.to_dict()["유의사항"] == ["VI(정적)"]


def test_warning_filter_can_be_disabled():
    cfg = StrategyConfig(min_avg_turnover=0.0, max_market_cap=0.0, min_price=0.0,
                         exclude_warned=False)
    loader = StubLoader(_signal_frames(),
                        warnings={"AAA": [{"warningType": "LIQUIDATION_TRADING"}]})
    report = Screener(loader, cfg, max_workers=1).scan()
    assert len(report.signals) == 2 and report.blocked == []


def test_suspended_symbol_filtered_on_symbols_path(cfg):
    """--top / --symbols 경로에서도 거래정지 종목이 걸러진다."""
    loader = StubLoader(_signal_frames())
    scr = Screener(loader, cfg, max_workers=1)
    loader.meta = lambda sym: StockMeta(sym, sym, "KOSDAQ", 1e7, suspended=(sym == "AAA"))
    assert [m.symbol for m in scr.build_universe(symbols=["AAA", "BBB"])] == ["BBB"]


# ------------------------------------------------------------- 프로파일
def test_profiles_differ():
    """운영 프로파일은 원본 스펙보다 모든 축에서 엄격하다."""
    from screener.config import StrategyConfig

    spec, tuned = StrategyConfig.spec(), StrategyConfig.tuned()
    assert (spec.upper_tail_min, spec.vol_mult_min, spec.vol_condition_mode,
            spec.adjust_max_days) == (0.35, 5.0, "or", 10)
    assert (tuned.upper_tail_min, tuned.vol_mult_min, tuned.vol_condition_mode,
            tuned.adjust_max_days) == (0.45, 20.0, "and", 3)


def test_tuned_profile_is_stricter_on_same_data(cfg):
    """같은 데이터에서 tuned 는 spec 의 부분집합만 잡아낸다."""
    from screener.config import StrategyConfig
    from screener.pattern_detector import PatternDetector, enrich

    df = inject_pattern(make_df(), vol_mult=1.5)     # 20일 최고 거래량이지만 평균 대비 배수는 낮음
    spec = StrategyConfig.spec(min_avg_turnover=0.0, max_market_cap=0.0, min_price=0.0)
    tuned = StrategyConfig.tuned(min_avg_turnover=0.0, max_market_cap=0.0, min_price=0.0)
    assert PatternDetector(spec).find_base_candles(enrich(df, spec))       # or 조건 통과
    assert not PatternDetector(tuned).find_base_candles(enrich(df, tuned))  # and 조건 탈락
