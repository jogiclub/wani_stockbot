"""볼린저 밴드 반전 매매 — 지표·판정·봇·API 테스트 (외부 API 불필요)."""

import numpy as np
import pandas as pd
import pytest

from screener.bollinger import (BollingerConfig, Position, Snapshot, bollinger_bands,
                                decide, enrich, evaluate, position_size, prepare,
                                regular_session, resample, rsi)


# ------------------------------------------------------------------ 지표
def test_rsi_matches_wilder_definition():
    """단조 상승은 100, 완전 횡보는 50, 단조 하락은 0."""
    up = pd.Series(np.arange(1, 60, dtype=float))
    flat = pd.Series(np.full(60, 100.0))
    down = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert rsi(up).iloc[-1] == pytest.approx(100.0)
    assert rsi(flat).iloc[-1] == pytest.approx(50.0)
    assert rsi(down).iloc[-1] == pytest.approx(0.0)


def test_rsi_warmup_is_nan():
    """기간이 차기 전 값은 NaN 이어야 한다 (0 이나 50 으로 채우면 오판한다)."""
    s = pd.Series(np.random.default_rng(0).normal(100, 1, 40))
    assert np.isnan(rsi(s, 14).iloc[12])
    assert np.isfinite(rsi(s, 14).iloc[-1])


def test_bollinger_bands_on_flat_series_collapse():
    flat = pd.Series(np.full(40, 1000.0))
    upper, mid, lower = bollinger_bands(flat, 20, 2.0)
    assert upper.iloc[-1] == pytest.approx(1000.0)
    assert lower.iloc[-1] == pytest.approx(1000.0)
    assert mid.iloc[-1] == pytest.approx(1000.0)


def test_bollinger_bands_width_follows_stddev():
    s = pd.Series(np.random.default_rng(1).normal(1000, 20, 60))
    upper, mid, lower = bollinger_bands(s, 20, 2.0)
    sd = s.rolling(20).std(ddof=0).iloc[-1]
    assert upper.iloc[-1] - mid.iloc[-1] == pytest.approx(2 * sd)
    assert mid.iloc[-1] - lower.iloc[-1] == pytest.approx(2 * sd)


def test_resample_aggregates_ohlcv():
    idx = pd.date_range("2026-09-08 09:00", periods=30, freq="1min")
    df = pd.DataFrame({"open": np.arange(30.0), "high": np.arange(30.0) + 2,
                       "low": np.arange(30.0) - 2, "close": np.arange(30.0) + 1,
                       "volume": np.ones(30)}, index=idx)
    out = resample(df, "15min")
    assert len(out) == 2
    assert out.iloc[0]["open"] == 0.0 and out.iloc[0]["close"] == 15.0
    assert out.iloc[0]["high"] == 16.0 and out.iloc[0]["low"] == -2.0
    assert out.iloc[0]["volume"] == 15.0


def test_regular_session_drops_after_hours_bars():
    """시간외 단일가 봉이 섞이면 이동평균·표준편차가 왜곡된다 — 빼야 한다."""
    idx = pd.to_datetime(["2026-09-07 18:45", "2026-09-08 08:50", "2026-09-08 09:00",
                          "2026-09-08 12:00", "2026-09-08 15:30", "2026-09-08 16:10"])
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                      index=idx)
    kept = list(regular_session(df).index.strftime("%m-%d %H:%M"))
    assert kept == ["09-08 09:00", "09-08 12:00", "09-08 15:30"]


def test_prepare_skips_session_filter_for_daily_bars():
    idx = pd.date_range("2026-06-01", periods=30, freq="B")
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                      index=idx)   # 일봉 index 는 자정이라 정규장 필터를 태우면 전멸한다
    assert len(prepare(df, BollingerConfig(interval="1d"))) == 30


# ------------------------------------------------------------------ 판정
def snap(close, lower=900.0, mid=1000.0, upper=1100.0, rsi_v=50.0):
    width = upper - lower
    return Snapshot(time="t", close=close, upper=upper, mid=mid, lower=lower,
                    rsi=rsi_v, percent_b=(close - lower) / width, bandwidth=width / mid)


CFG = BollingerConfig()


def test_buy_needs_both_band_and_rsi():
    assert decide("005930", snap(890, rsi_v=25), CFG).action == "BUY"
    # 밴드만 만족 (RSI 과매도 아님) → 매수하지 않는다
    assert decide("005930", snap(890, rsi_v=45), CFG).action == "HOLD"
    # RSI 만 만족 (하한선 위) → 매수하지 않는다
    assert decide("005930", snap(950, rsi_v=25), CFG).action == "HOLD"


def test_sell_needs_both_band_and_rsi():
    pos = Position("005930", quantity=10, entry_price=1000)
    assert decide("005930", snap(1110, rsi_v=75), CFG, pos).action == "SELL"
    assert decide("005930", snap(1110, rsi_v=60), CFG, pos).action != "SELL"


def test_stop_loss_wins_over_buy_signal():
    """밴드 라이딩 방지: 하한선 이탈 + 과매도라도 손절선을 깼으면 손절이 먼저다."""
    cfg = BollingerConfig(stop_loss_pct=0.05)
    pos = Position("005930", quantity=10, entry_price=1000)
    d = decide("005930", snap(940, lower=950, rsi_v=20), cfg, pos)
    assert d.action == "STOP" and d.quantity_ratio == 1.0


def test_mid_take_profit_only_above_entry_and_once():
    cfg = BollingerConfig(mid_take_profit=True, mid_take_ratio=0.5)
    pos = Position("005930", quantity=10, entry_price=950)
    d = decide("005930", snap(1000, rsi_v=55), cfg, pos)
    assert d.action == "TAKE_PROFIT" and d.quantity_ratio == 0.5

    pos.mid_taken = True                        # 이미 했으면 다시 하지 않는다
    assert decide("005930", snap(1000, rsi_v=55), cfg, pos).action == "HOLD"

    loss_pos = Position("005930", quantity=10, entry_price=1050)   # 매수가 아래면 익절 아님
    assert decide("005930", snap(1000, rsi_v=55), cfg, loss_pos).action == "HOLD"


def test_mid_take_profit_can_be_disabled():
    cfg = BollingerConfig(mid_take_profit=False)
    pos = Position("005930", quantity=10, entry_price=950)
    assert decide("005930", snap(1000, rsi_v=55), cfg, pos).action == "HOLD"


def test_no_position_means_no_exit():
    """보유가 없으면 손절·청산 판정이 나오면 안 된다."""
    assert decide("005930", snap(1110, rsi_v=80), CFG, None).action == "HOLD"
    assert decide("005930", snap(500, rsi_v=80), CFG, Position("005930", quantity=0)).action == "HOLD"


def test_evaluate_returns_none_without_enough_bars():
    idx = pd.date_range("2026-09-08 09:00", periods=10, freq="1min")
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                      index=idx)
    assert evaluate("005930", df, BollingerConfig(interval="1m")) is None


def test_evaluate_uses_live_price_over_bar_close():
    """장중에는 마지막 봉 종가가 아니라 현재가로 판정한다."""
    idx = pd.date_range("2026-09-08 09:00", periods=60, freq="1min")
    close = np.full(60, 1000.0)
    close[-1] = 1000.0
    df = pd.DataFrame({"open": close, "high": close, "low": close,
                       "close": close, "volume": 1.0}, index=idx)
    cfg = BollingerConfig(interval="1m")
    # 완전 횡보라 밴드 폭이 0 → 현재가가 하한선 이하이고 RSI 50 이라 매수 조건 미달
    d = evaluate("005930", df, cfg, None, live_price=1.0)
    assert d is not None and d.snapshot.close == 1.0


# ------------------------------------------------------------- 포지션 사이징
def test_position_size_respects_ratio_and_order_cap():
    cfg = BollingerConfig(cash_ratio=0.5, min_order_amount=50_000)
    # 예수금 1,000,000 × 50% = 500,000 → 1만원짜리 50주
    assert position_size(1_000_000, 10_000, cfg, 10_000_000) == 50
    # 1회 주문 한도가 더 낮으면 한도가 이긴다
    assert position_size(1_000_000, 10_000, cfg, 200_000) == 20


def test_position_size_zero_below_minimum():
    cfg = BollingerConfig(cash_ratio=0.1, min_order_amount=50_000)
    assert position_size(100_000, 1_000, cfg, 10_000_000) == 0   # 예산 1만원 < 최소 5만원
    assert position_size(1_000_000, 0, cfg, 10_000_000) == 0


# ------------------------------------------------------------------ 설정
def test_from_body_keeps_only_valid_symbols():
    cfg = BollingerConfig.from_body({"symbols": "005930, 000660 abc 12345 005930"})
    assert cfg.symbols == ("005930", "000660")      # 중복·형식 오류 제거


def test_from_body_clamps_and_rejects_bad_interval():
    cfg = BollingerConfig.from_body({"cash_ratio": 99, "stop_loss_pct": 0, "max_positions": 999})
    assert cfg.cash_ratio == 1.0 and cfg.stop_loss_pct == 0.005 and cfg.max_positions == 20
    with pytest.raises(ValueError):
        BollingerConfig.from_body({"interval": "3m"})


def test_from_body_ignores_unknown_keys():
    cfg = BollingerConfig.from_body({"enabled": True, "account_seq": 9, "bogus": 1})
    assert not hasattr(cfg, "bogus")
