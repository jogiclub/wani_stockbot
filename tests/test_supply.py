"""수급 포착 매매 (screener/supply.py) 회귀 테스트 — 외부 API 불필요."""

from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from screener.data_loader import StockMeta
from screener.supply import (STATUS_EXIT, STATUS_HOLD, STATUS_WATCH, STATUS_WEAK,
                             SupplyConfig, SupplyScanner, SupplyService, continuous_bars,
                             evaluate, expected_cum_volume, minute_ma, session_bars,
                             tick_flow, vwap)

DAY = date(2026, 9, 16)
PAST = [date(2026, 9, d) for d in (9, 10, 11, 14, 15)]


# ------------------------------------------------------------------ 합성 데이터
def minute_bars(day: date, minutes: int, *, base_vol: float = 1_000.0, surge: float = 1.0,
                price0: float = 10_000.0, drift: float = 0.0005, late_drift: float | None = None,
                late_from: int = 10_000, fade_from: int = 10_000, fade: float = 1.0,
                seed: int = 1) -> pd.DataFrame:
    """`09:01` 부터 `minutes` 개의 1분봉 (토스처럼 **봉 종료 시각** 라벨)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(datetime.combine(day, datetime.min.time()) + pd.Timedelta(hours=9, minutes=1),
                        periods=minutes, freq="1min")
    steps = np.array([late_drift if (late_drift is not None and i >= late_from) else drift
                      for i in range(minutes)])
    close = price0 * np.exp(np.cumsum(steps + rng.normal(0, 0.0003, minutes)))
    open_ = np.r_[price0, close[:-1]]
    high = np.maximum(open_, close) * 1.001
    low = np.minimum(open_, close) * 0.999
    # 장 초반에 거래가 몰리는 U자형 일중 패턴
    shape = np.array([3.0 if i < 30 else 1.5 if i < 60 else 1.0 for i in range(minutes)])
    vol = base_vol * shape * surge
    vol = np.where(np.arange(minutes) >= fade_from, vol * fade, vol)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol},
                        index=idx)


def daily_bars(end: date, n: int = 200, price: float = 10_000.0, *,
               peak: float = 16_000.0) -> pd.DataFrame:
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    close = np.linspace(peak, price, n)
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": np.full(n, 300_000.0)}, index=idx)


def profiles(base_vol: float = 1_000.0) -> list[pd.Series]:
    return [minute_bars(d, 60, base_vol=base_vol)["volume"].set_axis(range(1, 61))
            for d in PAST]


CFG = SupplyConfig(min_turnover=0, min_price=0, use_investor=False, use_orderbook=False)


# ------------------------------------------------------------------ 지표
def test_session_bars_follow_end_labelled_minutes():
    """`09:00` 봉은 장전, `15:31` 봉은 종가 동시호가 — 앞은 빼고 뒤는 넣는다."""
    idx = pd.to_datetime(["2026-09-16 08:59", "2026-09-16 09:00", "2026-09-16 09:01",
                          "2026-09-16 15:20", "2026-09-16 15:31", "2026-09-16 15:41"])
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    kept = session_bars(df)
    assert [t.strftime("%H:%M") for t in kept.index] == ["09:01", "15:20", "15:31"]
    assert [t.strftime("%H:%M") for t in continuous_bars(kept).index] == ["09:01", "15:20"]


def test_tick_flow_splits_by_price_change():
    idx = pd.date_range("2026-09-16 09:01", periods=4, freq="1min")
    df = pd.DataFrame({"open": [10, 10, 11, 11], "high": 12, "low": 9,
                       "close": [10.5, 11, 10, 10], "volume": [100, 200, 300, 400]},
                      index=idx, dtype=float)
    f = tick_flow(df)
    assert f["buy"].tolist() == [100, 200, 0, 0]        # 첫 봉은 양봉 → 매수
    assert f["sell"].tolist() == [0, 0, 300, 400]       # 보합 + 음봉 → 매도
    assert f["cvd"].iloc[-1] == 300 - 700


def test_vwap_is_volume_weighted():
    idx = pd.date_range("2026-09-16 09:01", periods=2, freq="1min")
    df = pd.DataFrame({"open": [10, 20], "high": [10, 20], "low": [10, 20],
                       "close": [10, 20], "volume": [1, 3]}, index=idx, dtype=float)
    assert vwap(df).iloc[-1] == pytest.approx(17.5)


def test_minute_ma_uses_only_completed_bars():
    """5분봉 이평은 완성된 5분봉만 쓴다 — 진행 중인 봉 값이 새면 미래 정보다."""
    bars = minute_bars(DAY, 120)
    ma = minute_ma(bars, "5min", 3)
    assert ma.iloc[:14].isna().all()                     # 5분봉 3개가 모이기 전
    five = bars["close"].resample("5min", label="right", closed="left").last()
    first = five.rolling(3).mean().dropna().index[0]
    assert ma.loc[first] == pytest.approx(five.rolling(3).mean().loc[first])
    later = bars.copy()
    later.iloc[-1, later.columns.get_loc("close")] *= 2  # 마지막 봉만 바꿔도 이미 나온 값은 불변
    assert minute_ma(later, "5min", 3).iloc[:-1].equals(ma.iloc[:-1])


def test_expected_volume_ignores_premarket_offset():
    p = pd.Series({0: 999_999.0, 1: 10.0, 2: 20.0})
    assert expected_cum_volume([p], 2) == 30.0


# ------------------------------------------------------------------ 판정
def test_no_signal_without_surge():
    bars = minute_bars(DAY, 120, surge=1.2)
    assert evaluate("A", "a", bars, daily_bars(DAY), profiles(), 1e11, CFG) is None


def test_early_surge_before_confirm_is_watch():
    bars = minute_bars(DAY, 20, surge=4.0)
    sig = evaluate("A", "a", bars, daily_bars(DAY), profiles(), 1e11, CFG)
    assert sig is not None and sig.status == STATUS_WATCH
    assert sig.surge_ratio == pytest.approx(4.0, rel=0.05)
    assert sig.surge_basis == "과거 5일 같은 시각"
    assert sig.surge_at == "09:20"


def test_sustained_flow_is_hold():
    bars = minute_bars(DAY, 180, surge=4.0, drift=0.0006)
    sig = evaluate("A", "a", bars, daily_bars(DAY), profiles(), 1e11, CFG)
    assert sig.status == STATUS_HOLD, sig.checks
    assert sig.surge_at == "10:00"                       # 1차 구간이 끝났으면 10:00 기준으로 잰다
    assert sig.price >= sig.stop >= 0 and sig.checks["바닥권"]
    assert 0 < sig.score <= 100


def test_breakdown_below_stop_is_exit():
    bars = minute_bars(DAY, 180, surge=4.0, drift=0.0006, late_drift=-0.004, late_from=150)
    sig = evaluate("A", "a", bars, daily_bars(DAY), profiles(), 1e11, CFG)
    assert sig.status == STATUS_EXIT
    assert not sig.checks["손절선위"]


def test_fading_volume_with_selling_is_exit_or_weak():
    bars = minute_bars(DAY, 180, surge=4.0, drift=0.0006, late_drift=-0.0003, late_from=150,
                       fade_from=150, fade=0.2)
    sig = evaluate("A", "a", bars, daily_bars(DAY), profiles(), 1e11, CFG)
    assert sig.status in (STATUS_EXIT, STATUS_WEAK)
    assert not sig.checks["거래량유지"]


def test_gap_and_high_zone_warnings_cut_score():
    bars = minute_bars(DAY, 180, surge=4.0, drift=0.0006, price0=16_500.0)
    daily = daily_bars(DAY, price=15_000.0, peak=15_000.0)        # 전일 종가 15,000 → 갭 +10%
    sig = evaluate("A", "a", bars, daily, profiles(), 1e11, CFG)
    assert not sig.checks["갭과열아님"] and not sig.checks["고점권아님"]
    assert any("설거지" in n for n in sig.notes) and any("갭" in n for n in sig.notes)
    clean = evaluate("A", "a", minute_bars(DAY, 180, surge=4.0, drift=0.0006),
                     daily_bars(DAY), profiles(), 1e11, CFG)
    assert sig.score < clean.score


def test_falls_back_to_daily_average_without_profiles():
    bars = minute_bars(DAY, 60, surge=4.0)
    sig = evaluate("A", "a", bars, daily_bars(DAY), [], 1e11, CFG)
    assert sig is not None and sig.surge_basis == "일평균 근사"
    assert any("근사" in n for n in sig.notes)


def test_turnover_and_price_floor():
    bars = minute_bars(DAY, 60, surge=4.0)
    cfg = SupplyConfig(min_turnover=10e9, use_investor=False, use_orderbook=False)
    assert evaluate("A", "a", bars, daily_bars(DAY), profiles(), 5e9, cfg) is None


def test_today_is_excluded_from_history():
    """일봉에 오늘 봉이 섞여 있어도 전일 종가·평소 거래량은 어제까지로 잰다 (look-ahead 방지)."""
    bars = minute_bars(DAY, 60, surge=4.0)
    daily = daily_bars(DAY)
    today = daily.index[-1]
    daily.loc[today, ["close", "volume"]] = [99_999.0, 1e12]
    sig = evaluate("A", "a", bars, daily, [], 1e11, CFG)
    assert sig.prev_close != 99_999.0
    assert sig.day_volume_ratio < 1e3


def test_to_dict_is_strict_json():
    bars = minute_bars(DAY, 20, surge=4.0)                 # 5분봉 이평이 아직 NaN
    sig = evaluate("A", "a", bars, daily_bars(DAY), profiles(), 1e11, CFG)
    sig.investor = {"외국인": float("nan")}
    json.dumps(sig.to_dict(), allow_nan=False)


# ------------------------------------------------------------------ 스캐너 · 서비스
class FakeSupplyLoader:
    def __init__(self):
        self.calls: list[str] = []
        self.bars = {
            "000001": minute_bars(DAY, 180, surge=4.0, drift=0.0006),
            "000002": minute_bars(DAY, 180, surge=1.0),
            "000003": minute_bars(DAY, 180, surge=5.0),
            "000004": minute_bars(DAY, 180, surge=6.0),
        }
        self.meta_map = {
            "000001": StockMeta("000001", "지속주", "KOSDAQ", security_type="STOCK"),
            "000002": StockMeta("000002", "평범주", "KOSDAQ", security_type="STOCK"),
            "000003": StockMeta("000003", "경고주", "KOSDAQ", security_type="STOCK"),
            "000004": StockMeta("000004", "레버리지ETF", "KOSPI", security_type="ETF"),
            "000005": StockMeta("000005", "정지주", "KOSPI", suspended=True, security_type="STOCK"),
        }

    def turnover_leaders(self, count):
        return [(s, 5e10) for s in self.meta_map][:count]

    def metas(self, symbols):
        return {s: self.meta_map[s] for s in symbols}

    def session_minutes(self, symbol):
        self.calls.append(symbol)
        return self.bars[symbol]

    def ohlcv(self, symbol, count):
        return daily_bars(DAY)

    def minute_profile(self, symbol, day, minutes):
        assert day < DAY                                   # 오늘은 '평소' 에 넣지 않는다
        return minute_bars(day, minutes)["volume"].set_axis(range(1, minutes + 1))

    def warnings(self, symbol):
        return [{"warningType": "INVESTMENT_WARNING"}] if symbol == "000003" else []

    def investor_flow(self, symbol, session):
        return {"외국인": 1000.0, "기관": 500.0, "외국인+기관": 1500.0}

    def orderbook_summary(self, symbol):
        return {"매수잔량": 1.0, "매도잔량": 2.0, "매수/매도": 0.5}


def test_scanner_filters_etf_suspended_and_warned():
    loader = FakeSupplyLoader()
    rep = SupplyScanner(loader, SupplyConfig(min_price=0)).scan()
    assert "000004" not in loader.calls and "000005" not in loader.calls   # 조회조차 안 한다
    assert [s.symbol for s in rep.signals] == ["000001"]
    assert rep.blocked == [{"종목코드": "000003", "종목명": "경고주", "사유": "투자경고"}]
    sig = rep.signals[0]
    assert sig.investor["외국인+기관"] == 1500.0
    assert any(d["요인"] == "외국인·기관 순매수" for d in sig.score_detail)
    assert rep.session == DAY


def test_chart_payload_matches_evaluation():
    sc = SupplyScanner(FakeSupplyLoader(), SupplyConfig(min_price=0))
    ch = sc.chart("000001")
    json.dumps(ch, allow_nan=False)
    assert ch["session"] == DAY.isoformat() and len(ch["bars"]) == 180
    assert ch["bars"][0]["t"] == "09:01" and ch["profile_days"] == 5
    assert len(ch["profile"]) == 60 and ch["profile"][-1]["expected"] > 0


def test_service_state_is_json_safe_during_market():
    svc = SupplyService(SupplyScanner(FakeSupplyLoader(), SupplyConfig(min_price=0)),
                        clock=lambda: datetime(2026, 9, 16, 10, 30))
    svc.scan_once()
    state = svc.state.as_dict()
    json.dumps(state, allow_nan=False)
    assert state["session"] == DAY.isoformat() and state["market_open"] is True


def test_service_scans_rarely_outside_market_hours():
    svc = SupplyService(SupplyScanner(FakeSupplyLoader(), SupplyConfig(min_price=0)),
                        interval=60, idle_interval=1800,
                        clock=lambda: datetime(2026, 9, 16, 21, 0))
    assert svc.due() is True
    svc.scan_once()
    assert svc.due() is False                               # 장 밖에서는 30분에 한 번만


# ------------------------------------------------------------------ 웹 API
from fastapi.testclient import TestClient  # noqa: E402

from screener.config import StrategyConfig  # noqa: E402
from screener.data_loader import SyntheticDataLoader  # noqa: E402
from screener.web import create_app  # noqa: E402


@pytest.fixture
def supply_service():
    return SupplyService(SupplyScanner(FakeSupplyLoader(), SupplyConfig(min_price=0)),
                         clock=lambda: datetime(2026, 9, 16, 10, 30))


def test_supply_page_and_menu_order():
    app = create_app(SyntheticDataLoader(n_symbols=1), StrategyConfig(), interval=9999, top=0)
    with TestClient(app) as c:
        r = c.get("/supply")
        assert r.status_code == 200 and 'data-gnb="supply"' in r.text
        gnb = c.get("/static/gnb.js").text
        # 수급포착매매가 세력추정매매 왼쪽(앞)에 온다
        assert gnb.index("수급포착매매") < gnb.index("세력추정매매") < gnb.index("볼린저 밴드 반전 매매")


def test_supply_state_without_service_is_unconfigured():
    app = create_app(SyntheticDataLoader(n_symbols=1), StrategyConfig(), interval=9999, top=0)
    with TestClient(app) as c:
        body = c.get("/api/supply/state").json()
        assert body["configured"] is False and body["signals"] == []
        assert body["config"]["surge_min"] == 2.0
        assert c.post("/api/supply/scan").status_code == 503
        assert c.get("/api/supply/chart/000001").status_code == 503


def test_supply_state_scan_and_chart(supply_service):
    app = create_app(SyntheticDataLoader(n_symbols=1), StrategyConfig(), interval=9999, top=0,
                     supply=supply_service)
    with TestClient(app) as c:
        # 앱 시작과 함께 백그라운드 스캔이 돈다 — 한 바퀴 끝날 때까지 기다린다
        import time
        deadline = time.monotonic() + 15
        while supply_service.state.finished_at is None and time.monotonic() < deadline:
            time.sleep(0.05)
        while supply_service.state.running and time.monotonic() < deadline:
            time.sleep(0.05)
        body = c.get("/api/supply/state").json()
        assert body["configured"] is True and body["source"] == "server"
        assert [s["종목코드"] for s in body["signals"]] == ["000001"]
        assert body["signals"][0]["상태"] == STATUS_HOLD
        assert body["blocked"][0]["종목코드"] == "000003"

        chart = c.get("/api/supply/chart/000001")
        assert chart.status_code == 200 and chart.json()["symbol"] == "000001"
        assert c.get("/api/supply/chart/..%2Fetc").status_code in (400, 404)
        assert c.get("/api/supply/chart/12345").status_code == 400
    assert supply_service._stop.is_set()                   # 앱 종료 시 스레드도 멈춘다


# ------------------------------------------------------------------ 진입 판정
def test_sustained_signal_is_entry():
    """수급 지속 + 과열·고점 아님 + 점수 컷오프 → '진입'(들어갈 만한 것)."""
    bars = minute_bars(DAY, 180, surge=4.0, drift=0.0006)
    sig = evaluate("A", "a", bars, daily_bars(DAY), profiles(), 1e11, CFG)
    assert sig.status == STATUS_HOLD and sig.entry is True
    assert all(e["충족"] for e in sig.entry_reasons)


def test_gap_up_blocks_entry_even_if_sustained():
    """수급은 지속이어도 시가 갭이 과열이면 진입에서 제외한다 (단발 재료 회피)."""
    bars = minute_bars(DAY, 180, surge=4.0, drift=0.0006)
    daily = daily_bars(DAY)
    # prev_close 는 session 이전 마지막 종가 = 뒤에서 두 번째 행 (마지막 행은 당일이라 제외됨)
    daily.iloc[-2, daily.columns.get_loc("close")] = float(bars["open"].iloc[0]) / 1.1  # 시가 +10% 갭
    sig = evaluate("A", "a", bars, daily, profiles(), 1e11, CFG)
    assert sig.status == STATUS_HOLD and sig.entry is False
    reasons = {e["항목"]: e["충족"] for e in sig.entry_reasons}
    assert reasons["갭 과열 아님(단발 재료 회피)"] is False


def test_score_cutoff_blocks_entry():
    """점수 컷오프를 높이면 지속이어도 진입이 아니다."""
    bars = minute_bars(DAY, 180, surge=4.0, drift=0.0006)
    sig = evaluate("A", "a", bars, daily_bars(DAY), profiles(), 1e11,
                   SupplyConfig(min_turnover=0, min_price=0, use_investor=False,
                                use_orderbook=False, entry_score_min=101))
    assert sig.status == STATUS_HOLD and sig.entry is False


def test_watch_status_is_not_entry():
    """연속성 판단 전(관찰) 종목은 진입이 아니다."""
    bars = minute_bars(DAY, 20, surge=4.0)
    sig = evaluate("A", "a", bars, daily_bars(DAY), profiles(), 1e11, CFG)
    assert sig.status == STATUS_WATCH and sig.entry is False


def test_non_entry_hold_is_flagged_false():
    """지속이지만 진입 조건 미달(점수 컷오프 초과)이면 진입 플래그가 꺼진다."""
    svc = SupplyService(SupplyScanner(FakeSupplyLoader(),
                                      SupplyConfig(min_price=0, entry_score_min=101)),
                        clock=lambda: datetime(2026, 9, 16, 10, 30))
    svc.scan_once()
    assert svc.state.signals[0]["상태"] == STATUS_HOLD and svc.state.signals[0]["진입"] is False
