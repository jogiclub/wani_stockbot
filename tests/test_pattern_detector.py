"""패턴 탐지 알고리즘 단위 테스트 (외부 API 불필요)."""

import numpy as np
import pandas as pd
import pytest

from screener.config import StrategyConfig
from screener.data_loader import SyntheticDataLoader, candles_to_df
from screener.pattern_detector import PatternDetector, enrich
from screener.screener import Screener


@pytest.fixture
def cfg():
    return StrategyConfig()


@pytest.fixture
def det(cfg):
    return PatternDetector(cfg)


def make_df(n=200, start=10000.0, drift=-0.003, seed=1):
    """바닥권으로 흘러내리는 밋밋한 베이스 데이터."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp("2026-09-01"), periods=n)
    close = start * np.exp(np.arange(n) * drift)
    return pd.DataFrame({
        "open": close * 1.001, "high": close * 1.008,
        "low": close * 0.992, "close": close,
        "volume": np.full(n, 100_000.0) + rng.normal(0, 500, n),
    }, index=idx)


def inject_pattern(df, base_pos=-7, tail=0.6, vol_mult=8.0, adjust_decay=0.3):
    """기준봉 → 매물 소화 → 전일고가 돌파 패턴을 심는다."""
    d = df.copy()
    n = len(d)
    b = n + base_pos if base_pos < 0 else base_pos
    base_close = d["close"].iloc[b - 1]
    rng_size = base_close * 0.30
    body_top = base_close * 1.08
    d.iloc[b, d.columns.get_loc("open")] = base_close
    d.iloc[b, d.columns.get_loc("close")] = body_top
    d.iloc[b, d.columns.get_loc("low")] = body_top - rng_size * (1 - tail) - 1e-9
    d.iloc[b, d.columns.get_loc("high")] = body_top + rng_size * tail
    d.iloc[b, d.columns.get_loc("volume")] = d["volume"].iloc[b - 20:b].max() * vol_mult

    for k in range(b + 1, n - 1):
        d.iloc[k, d.columns.get_loc("volume")] = d["volume"].iloc[b] * adjust_decay * (0.9 ** (k - b))
        c = d["close"].iloc[b] * (0.96 - 0.005 * (k - b))
        d.iloc[k, d.columns.get_loc("close")] = c
        d.iloc[k, d.columns.get_loc("open")] = c * 1.01
        d.iloc[k, d.columns.get_loc("high")] = c * 1.02
        d.iloc[k, d.columns.get_loc("low")] = c * 0.985

    t = n - 1
    prev_high = d["high"].iloc[t - 1]
    d.iloc[t, d.columns.get_loc("open")] = d["close"].iloc[t - 1]
    d.iloc[t, d.columns.get_loc("low")] = d["close"].iloc[t - 1] * 0.995
    d.iloc[t, d.columns.get_loc("close")] = prev_high * 1.04
    d.iloc[t, d.columns.get_loc("high")] = prev_high * 1.05
    d.iloc[t, d.columns.get_loc("volume")] = d["volume"].iloc[t - 1] * 3
    return d


# --------------------------------------------------------------- 지표
def test_upper_tail_ratio(cfg):
    df = pd.DataFrame({"open": [100.0], "high": [140.0], "low": [90.0],
                       "close": [110.0], "volume": [1.0]},
                      index=pd.to_datetime(["2026-09-01"]))
    assert enrich(df, cfg)["upper_tail_ratio"].iloc[0] == pytest.approx((140 - 110) / (140 - 90))


def test_vol_columns_use_only_past(cfg):
    """거래량 비교는 shift(1) 기반 — 당일 거래량이 평균에 섞이면 안 된다."""
    df = make_df(60)
    df.iloc[-1, df.columns.get_loc("volume")] = 10_000_000.0
    e = enrich(df, cfg)
    assert e["vol_ma"].iloc[-1] < 200_000
    assert bool(e["is_vol_max"].iloc[-1])


# --------------------------------------------------------------- STEP 1
def test_base_candle_detected(det):
    d = enrich(inject_pattern(make_df()), det.cfg)
    bases = det.find_base_candles(d)
    assert bases and bases[0].bars_since == 6
    assert bases[0].upper_tail_ratio >= det.cfg.upper_tail_min


def test_short_tail_rejected(det):
    d = enrich(inject_pattern(make_df(), tail=0.10), det.cfg)
    assert det.find_base_candles(d) == []


def test_low_volume_rejected(det):
    cfg = StrategyConfig(require_vol_max=False, vol_mult_min=5.0)
    d = enrich(inject_pattern(make_df(), vol_mult=1.2), cfg)
    assert PatternDetector(cfg).find_base_candles(d) == []


def test_not_bottom_rejected(det):
    """장기 상승 추세(고점권) 종목은 바닥권 조건에서 탈락."""
    d = enrich(inject_pattern(make_df(drift=+0.006)), det.cfg)
    assert det.find_base_candles(d) == []


# --------------------------------------------------------------- STEP 2
def test_adjustment_volume_too_high(det):
    d = enrich(inject_pattern(make_df(), adjust_decay=0.95), det.cfg)
    base = det.find_base_candles(d)[0]
    ok, ratio, reasons = det.check_adjustment(d, base, len(d) - 1)
    assert not ok and ratio > det.cfg.adjust_vol_max_ratio and reasons


def test_adjustment_breaks_base_low(det):
    d = inject_pattern(make_df())
    d.iloc[-3, d.columns.get_loc("close")] = d["low"].iloc[-7] * 0.9
    e = enrich(d, det.cfg)
    base = det.find_base_candles(e)[0]
    ok, _, reasons = det.check_adjustment(e, base, len(e) - 1)
    assert not ok and any("저가 이탈" in r for r in reasons)


def test_adjustment_window_limit(det):
    """조정 기간이 10영업일을 넘으면 기준봉 후보에서 제외된다."""
    d = enrich(inject_pattern(make_df(), base_pos=-15), det.cfg)
    assert det.find_base_candles(d) == []


# --------------------------------------------------------------- STEP 3
def test_signal_end_to_end(det):
    sig = det.detect(inject_pattern(make_df()), "005930", "삼성전자")
    assert sig is not None
    assert sig.entry == pytest.approx(sig.prev_high)
    assert sig.stop == pytest.approx(sig.base.low)
    assert sig.volume_ratio_prev > 1
    assert sig.targets[0] > sig.entry


def test_no_breakout_no_signal(det):
    d = inject_pattern(make_df())
    prev_high = d["high"].iloc[-2]
    d.iloc[-1, d.columns.get_loc("high")] = prev_high * 0.99   # 전일 고가 미달
    d.iloc[-1, d.columns.get_loc("close")] = prev_high * 0.98
    assert det.detect(d, "TEST") is None


def test_no_volume_no_signal(det):
    d = inject_pattern(make_df())
    d.iloc[-1, d.columns.get_loc("volume")] = d["volume"].iloc[-2] * 0.5
    assert det.detect(d, "TEST") is None


def test_live_price_triggers_intraday(det):
    """장중: 일봉 고가는 미달이어도 현재가가 전일고가를 넘으면 시그널."""
    d = inject_pattern(make_df())
    prev_high = d["high"].iloc[-2]
    d.iloc[-1, d.columns.get_loc("high")] = prev_high * 0.99
    d.iloc[-1, d.columns.get_loc("close")] = prev_high * 0.98
    assert det.detect(d, "TEST") is None
    assert det.detect(d, "TEST", live_price=prev_high * 1.02) is not None


def test_insufficient_history(det):
    assert det.detect(make_df(50), "TEST") is None


# --------------------------------------------------------------- 통합
def test_screener_scan_synthetic():
    loader = SyntheticDataLoader(n_symbols=6, signal_symbols=("900002",))
    cfg = StrategyConfig(min_avg_turnover=0.0, max_market_cap=0.0, min_price=0.0)
    report = Screener(loader, cfg, max_workers=2).scan()
    assert [s.symbol for s in report.signals] == ["900002"]


def test_candles_to_df_parsing():
    df = candles_to_df([{
        "timestamp": "2026-03-25T09:00:00+09:00", "openPrice": "71600",
        "highPrice": "72300", "lowPrice": "71500", "closePrice": "72000",
        "volume": "3521000", "currency": "KRW",
    }])
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].iloc[0] == 72000.0
    assert str(df.index[0]) == "2026-03-25 09:00:00"


def test_backtest_finds_historical_signal(det):
    sigs = det.backtest_signals(inject_pattern(make_df(220)), "TEST")
    assert len(sigs) >= 1
