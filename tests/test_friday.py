"""금요일 마감 돌파 매매 (screener/friday.py · friday_backtest.py) 회귀 테스트 — 외부 API 불필요."""

from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from screener.data_loader import StockMeta
from screener.friday import (FridayConfig, FridayRecorder, FridayScanner, FridayService,
                             build_report, evaluate)
from screener.friday_backtest import ProxyParams, analyze, proxy_frame
from screener.web import create_app

FRI = date(2026, 10, 9)                  # 금요일
MON = date(2026, 10, 12)
PAST = [date(2026, 10, d) for d in (2, 5, 6, 7, 8)]
CFG = FridayConfig(min_turnover=0, min_price=0)


# ------------------------------------------------------------------ 합성 데이터
def friday_bars(day: date = FRI, *, until: str = "15:15", surge: float = 4.0, breakout: bool = True,
                crash_after: bool = False) -> pd.DataFrame:
    """`09:01`~`until` 1분봉. 14:30 까지 횡보 → 이후 거래량 `surge` 배 + 상승(돌파)."""
    idx = pd.date_range(f"{day} 09:01", f"{day} {until}", freq="1min")
    rng = np.random.default_rng(3)
    off = np.arange(1, len(idx) + 1)
    close = 10_000 + rng.normal(0, 3, len(idx))
    if breakout:
        close = np.where(off > 330, 10_000 + (off - 330) * 4.0, close)
    if crash_after:
        close = np.where(off > 375, 9_000.0, close)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 2
    low = np.minimum(open_, close) - 2
    vol = np.where(off > 330, 1_000.0 * surge, 1_000.0)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def daily(end: date, n: int = 200, *, noisy: bool = False) -> pd.DataFrame:
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    close = np.full(n, 10_000.0)
    if noisy:                                         # 직전 4일이 크게 출렁인다
        close[-4:] = [10_000, 12_500, 9_000, 11_800]
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": np.full(n, 300_000.0)}, index=idx)


def profile(volume: float = 1_000.0) -> pd.Series:
    return pd.Series(volume, index=range(1, 376))


def run_eval(bars=None, *, dly=None, profiles=None, regime=True, cfg=CFG):
    bars = friday_bars() if bars is None else bars
    dly = daily(date(2026, 10, 8)) if dly is None else dly
    profiles = [profile() for _ in PAST] if profiles is None else profiles
    return evaluate("000001", "테스트", "KOSDAQ", bars, dly, profiles, 5e10, cfg, regime)


# ------------------------------------------------------------------ P0 일봉 대리 백테스트
def proxy_daily(n: int = 80, signal_at: int = 64) -> pd.DataFrame:
    """조용하다가 `signal_at` 번째 봉(금요일로 맞춘다)에 거래량 3배 + 고가 마감 돌파."""
    idx = pd.bdate_range("2026-01-05", periods=n)          # 월요일 시작
    close = np.full(n, 10_000.0)
    open_ = close.copy()
    vol = np.full(n, 100_000.0)
    sig = signal_at
    assert idx[sig].weekday() == 4
    close[sig] = 10_300.0
    vol[sig] = 300_000.0
    open_[sig + 1] = 10_450.0                              # 다음 영업일(월) 갭상승
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.005,
                         "low": np.minimum(open_, close) * 0.995, "close": close, "volume": vol}, index=idx)


def test_proxy_signal_gap_uses_next_open_and_flags_weekend():
    df = proxy_daily()
    fr = proxy_frame(df, ProxyParams(min_turnover=0)).set_index("date")
    day = df.index[64]
    assert bool(fr.loc[day, "signal"]) and bool(fr.loc[day, "week_end"])
    assert fr.loc[day, "gap"] == pytest.approx(10_450 / 10_300 - 1)
    # 월~목은 주말을 넘기지 않는다
    assert not fr.loc[df.index[65], "week_end"]
    assert fr["signal"].sum() == 1


def test_proxy_signal_ignores_today_volume_in_average():
    """거래량 평균은 shift(1) — 당일 폭증이 자기 평균을 키워 신호를 없애면 안 된다."""
    fr = proxy_frame(proxy_daily(), ProxyParams(min_turnover=0))
    assert fr["signal"].sum() == 1


def test_proxy_last_bar_has_no_next_day():
    df = proxy_daily()
    fr = proxy_frame(df, ProxyParams(min_turnover=0))
    assert fr["date"].max() < df.index[-1]


def _frames(weekend_gap: float, ctl_gap: float, base_gap: float = 0.0, weeks: int = 40, seed: int = 1):
    rng = np.random.default_rng(seed)
    rows = []
    for w in range(weeks):
        fri = pd.Timestamp("2023-01-06") + pd.Timedelta(weeks=w * 3)
        for _ in range(3):
            rows.append((fri, True, True, weekend_gap + rng.normal(0, 0.004)))
            rows.append((fri - pd.Timedelta(days=2), False, True, ctl_gap + rng.normal(0, 0.004)))
        for _ in range(30):
            rows.append((fri, True, False, base_gap + rng.normal(0, 0.004)))
            rows.append((fri - pd.Timedelta(days=2), False, False, base_gap + rng.normal(0, 0.004)))
    return [pd.DataFrame(rows, columns=["date", "week_end", "signal", "gap"])]


def test_analyze_passes_when_weekend_edge_beats_cost_and_control():
    res = analyze(_frames(0.02, 0.0), ProxyParams(), symbols=1)
    assert res.verdict() == "통과", res.summary
    assert res.summary["손익분기비용%"] == pytest.approx(2.0, abs=0.3)


def test_analyze_conditional_when_gross_edge_below_cost():
    res = analyze(_frames(0.005, 0.0), ProxyParams(), symbols=1)       # 총 0.5% < 비용 0.75%
    assert res.verdict() == "조건부", res.summary


def test_analyze_redefine_when_weekend_equals_control():
    res = analyze(_frames(0.02, 0.02), ProxyParams(), symbols=1)
    assert res.verdict() == "재정의", res.summary


def test_analyze_rejects_negative_gap_and_small_sample():
    assert analyze(_frames(-0.01, 0.0), ProxyParams(), symbols=1).verdict() == "기각"
    assert analyze(_frames(0.02, 0.0, weeks=3), ProxyParams(), symbols=1).verdict() == "표본부족"


# ------------------------------------------------------------------ 판정 (분봉)
def test_entry_when_quiet_week_surge_and_breakout():
    sig = run_eval()
    assert sig is not None
    assert sig.surge_ratio == pytest.approx(4.0, rel=0.05)
    assert sig.price > sig.pre_high
    assert sig.checks["조용한한주"] and sig.entry and sig.score >= CFG.entry_score_min
    assert sig.as_of.strftime("%H:%M") == "15:15"


def test_no_candidate_without_surge_or_breakout():
    assert run_eval(friday_bars(surge=1.0)) is None
    assert run_eval(friday_bars(breakout=False)) is None


def test_requires_known_normal_volume():
    """과거 분봉이 없으면 급증을 말할 수 없다 — 일평균으로 근사하지 않는다."""
    assert run_eval(profiles=[]) is None
    assert run_eval(profiles=[profile()]) is None          # min_profile_days=2 미만


def test_window_not_open_returns_none():
    assert run_eval(friday_bars(until="14:20")) is None
    assert run_eval(friday_bars(until="14:30")) is None


def test_bars_after_window_end_are_ignored():
    """15:15 이후(동시호가 직전·후) 급락 봉은 판정에 쓰지 않는다 — 판정 시각 = 15:15."""
    sig = run_eval(friday_bars(until="15:31", crash_after=True))
    assert sig is not None and sig.as_of.strftime("%H:%M") == "15:15" and sig.price > 10_000


def test_not_quiet_week_is_candidate_but_not_entry():
    sig = run_eval(dly=daily(date(2026, 10, 8), noisy=True))
    assert sig is not None and not sig.checks["조용한한주"] and not sig.entry
    assert any("조용" in n for n in sig.notes)


def test_regime_off_blocks_entry_and_unknown_is_neutral():
    off, unknown = run_eval(regime=False), run_eval(regime=None)
    assert off is not None and not off.entry and not off.checks["국면우호"]
    assert unknown is not None and unknown.entry and any("미확인" in n for n in unknown.notes)


def test_hot_change_blocks_entry():
    dly = daily(date(2026, 10, 8))
    dly.iloc[-1, dly.columns.get_loc("close")] = 8_000.0          # 전일 종가가 낮아 당일 +20%↑
    sig = run_eval(dly=dly)
    assert sig is not None and not sig.checks["과열아님"] and not sig.entry


def test_today_in_daily_is_excluded_from_quiet_week():
    """일봉에 오늘 봉이 섞여 와도 '직전 4일' 에 포함하지 않는다."""
    dly = pd.concat([daily(date(2026, 10, 8)), pd.DataFrame(
        {"open": [10_000.0], "high": [13_000.0], "low": [9_000.0], "close": [12_000.0], "volume": [9e6]},
        index=[pd.Timestamp(FRI)])])
    sig = run_eval(dly=dly)
    assert sig is not None and sig.checks["조용한한주"]


# ------------------------------------------------------------------ 설정 · 창
def test_record_day_and_window():
    cfg = FridayConfig(extra_days=("2026-10-01",))
    assert cfg.is_record_day(FRI) and not cfg.is_record_day(MON)
    assert cfg.is_record_day(date(2026, 10, 1))                      # 금요일이 휴장인 주의 목요일
    assert cfg.in_window(datetime(2026, 10, 9, 14, 40)) and cfg.in_window(datetime(2026, 10, 9, 15, 18))
    assert not cfg.in_window(datetime(2026, 10, 9, 13, 0))
    assert not cfg.in_window(datetime(2026, 10, 9, 15, 25))
    assert not cfg.in_window(datetime(2026, 10, 12, 14, 40))         # 월요일


# ------------------------------------------------------------------ 기록 · 결과 · 리포트
class FakeLoader:
    def __init__(self, session: date = FRI, with_next: bool = True):
        self.session = session
        self.with_next = with_next
        self.meta_map = {
            "000001": StockMeta("000001", "돌파주", "KOSDAQ", security_type="STOCK"),
            "000002": StockMeta("000002", "평범주", "KOSDAQ", security_type="STOCK"),
            "000003": StockMeta("000003", "경고주", "KOSPI", security_type="STOCK"),
            "000004": StockMeta("000004", "레버리지ETF", "KOSPI", security_type="ETF"),
        }
        self.bars = {"000001": friday_bars(session, surge=4.0), "000002": friday_bars(session, surge=1.0),
                     "000003": friday_bars(session, surge=5.0), "000004": friday_bars(session, surge=6.0)}

    def turnover_leaders(self, count):
        return [(s, 5e10) for s in self.meta_map][:count]

    def metas(self, symbols):
        return {s: self.meta_map[s] for s in symbols}

    def session_minutes(self, symbol):
        return self.bars[symbol]

    def ohlcv(self, symbol, count):
        d = daily(date(2026, 10, 8))
        if self.with_next:                                            # 다음 영업일(월) 봉
            d.loc[pd.Timestamp(self.session)] = [10_000.0, 10_100.0, 9_900.0, 10_050.0, 3e5]
            d.loc[pd.Timestamp(MON)] = [10_500.0, 10_800.0, 10_400.0, 10_600.0, 4e5]
        return d

    def minute_profile(self, symbol, day, minutes):
        assert day < self.session
        return profile()

    def price_at(self, symbol, day, minutes=30):
        assert day == MON and minutes == 30
        return 10_700.0

    def warnings(self, symbol):
        return [{"warningType": "INVESTMENT_WARNING"}] if symbol == "000003" else []

    def index_ohlcv(self, symbol="KOSDAQ", count=800):
        idx = pd.bdate_range(end=pd.Timestamp(FRI), periods=60)
        up = symbol == "KOSDAQ"
        close = np.linspace(100, 130, 60) if up else np.linspace(130, 100, 60)
        return pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                             "volume": 1.0}, index=idx)


def scanner(tmp_path, loader=None, cfg=CFG):
    rec = FridayRecorder(tmp_path / "friday_signals.jsonl")
    return FridayScanner(loader or FakeLoader(), cfg, max_workers=2, recorder=rec), rec


def test_scan_filters_and_records_once(tmp_path):
    sc, rec = scanner(tmp_path)
    rep = sc.scan()
    assert [s.symbol for s in rep.signals] == ["000001"]             # ETF·경고·급증 없음 제외
    assert [b["종목코드"] for b in rep.blocked] == ["000003"]
    assert rep.regimes == {"KOSPI": False, "KOSDAQ": True}
    assert rep.session == FRI and rep.recorded == 2                  # candidate + entry
    assert sc.scan().recorded == 0                                   # 같은 종목은 다시 쓰지 않는다
    kinds = sorted(r["kind"] for r in rec.records())
    assert kinds == ["candidate", "entry"]
    assert FridayRecorder(rec.path).record(sc.scan().signals, CFG) == 0     # 재시작 후에도 중복 없음


def test_record_skips_non_record_day(tmp_path):
    tue = date(2026, 10, 6)
    sc, rec = scanner(tmp_path, FakeLoader(session=tue))
    rep = sc.scan()
    assert rep.signals and rep.recorded == 0 and rec.records() == []


def test_regime_failure_is_unknown_not_crash(tmp_path):
    loader = FakeLoader()
    loader.index_ohlcv = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    rep, _ = scanner(tmp_path, loader)[0].scan(), None
    assert rep.regimes == {"KOSPI": None, "KOSDAQ": None} and rep.signals[0].regime_on is None


def test_fill_outcomes_and_report(tmp_path):
    sc, rec = scanner(tmp_path)
    sc.scan()
    loader = sc.loader
    # 같은 날 저녁이나 월요일 장중에는 채우지 않는다
    assert rec.fill_outcomes(loader, FRI, datetime(2026, 10, 9, 16, 0)) == 0
    assert rec.fill_outcomes(loader, MON, datetime(2026, 10, 12, 10, 0)) == 0
    assert rec.fill_outcomes(loader, MON, datetime(2026, 10, 12, 16, 0)) == 1
    assert rec.fill_outcomes(loader, MON, datetime(2026, 10, 12, 16, 0)) == 0        # 한 번만
    out = rec.outcomes()["20261009-000001"]
    assert out["next_open"] == 10_500 and out["p0930"] == 10_700 and out["next_close"] == 10_600

    rep = rec.report(cost=0.0075)
    price = rec.records()[0]["price"]
    g = rep["groups"]["후보 전체"]
    assert g["건수"] == 1 and rep["pending"] == 0 and rep["weeks"] == 1
    assert g["평균갭%"] == pytest.approx((10_500 / price - 1) * 100, abs=0.01)
    assert g["평균순갭%"] == pytest.approx((10_500 / price - 1 - 0.0075) * 100, abs=0.01)
    assert rep["groups"]["진입 판정"]["건수"] == 1
    assert rep["groups"]["후보 · 15:00 이후 포착"]["건수"] == 1 and rep["groups"]["후보 · 15:00 이전 포착"]["건수"] == 0
    assert rep["trades"][0]["entry"] is True and rep["ready_for_review"] is False
    assert "100건" in rep["note"]


def test_fill_waits_when_next_day_missing(tmp_path):
    sc, rec = scanner(tmp_path, FakeLoader(with_next=False))
    sc.scan()
    assert rec.fill_outcomes(sc.loader, MON, datetime(2026, 10, 12, 16, 0)) == 0
    assert rec.report()["pending"] == 1


def test_report_with_no_records_is_empty():
    rep = build_report([], {})
    assert rep["recorded"] == 0 and rep["groups"]["후보 전체"] == {"건수": 0}


def test_recorder_without_path_is_noop():
    rec = FridayRecorder(None)
    assert rec.record([], CFG) == 0 and rec.records() == [] and rec.outcomes() == {}


def test_recorder_skips_corrupt_lines(tmp_path):
    p = tmp_path / "friday_signals.jsonl"
    p.write_text('깨진줄\n{"event":"other"}\n', encoding="utf-8")
    assert FridayRecorder(p).records() == []


# ------------------------------------------------------------------ 서비스
def test_service_scan_state_and_due(tmp_path):
    now = [datetime(2026, 10, 9, 14, 45)]
    sc, _ = scanner(tmp_path)
    svc = FridayService(sc, interval=60, idle_interval=1800, clock=lambda: now[0])
    assert svc.due()
    state = svc.scan_once()
    assert state.error is None and state.in_window and state.record_day
    assert state.signals and state.signals[0]["진입"] is True and state.recorded == 2
    assert not svc.due()                                            # 방금 돌았다
    now[0] = datetime(2026, 10, 10, 11, 0)                          # 토요일 — 창 밖이라 30분 간격
    assert not svc.in_window()


def test_service_survives_scan_failure(tmp_path):
    sc, _ = scanner(tmp_path)
    sc.loader.turnover_leaders = lambda c: (_ for _ in ()).throw(RuntimeError("api down"))
    svc = FridayService(sc, clock=lambda: datetime(2026, 10, 9, 14, 45))
    state = svc.scan_once()
    assert "api down" in state.error and state.finished_at and not state.running


def test_maybe_fill_runs_at_most_every_30_min(tmp_path):
    sc, rec = scanner(tmp_path)
    sc.scan()
    svc = FridayService(sc, clock=lambda: datetime(2026, 10, 12, 16, 0))
    assert svc.maybe_fill() == 1
    assert svc.maybe_fill() == 0


# ------------------------------------------------------------------ 웹 API
@pytest.fixture
def client(tmp_path):
    sc, _ = scanner(tmp_path)
    svc = FridayService(sc, clock=lambda: datetime(2026, 10, 9, 14, 45))
    app = create_app(None, friday=svc)
    with TestClient(app) as c:
        svc.scan_once()
        yield c


def test_friday_page_and_state(client):
    assert client.get("/friday").status_code == 200
    body = client.get("/api/friday/state").json()
    assert body["configured"] is True and body["signals"][0]["종목코드"] == "000001"
    assert body["config"]["window_start"] == "14:30" and body["regimes"]["KOSDAQ"] is True


def test_friday_chart_has_breakout_line(client):
    body = client.get("/api/friday/chart/000001").json()
    assert body["pre_high"] and body["window_start"] == 330 and body["bars"][-1]["t"] == "15:15"
    assert client.get("/api/friday/chart/12").status_code == 400


def test_friday_report_endpoint(client):
    body = client.get("/api/friday/report").json()
    assert body["enabled"] is True and body["recorded"] == 1


def test_friday_scan_endpoint(client):
    assert client.post("/api/friday/scan").json()["ok"] is True


def test_friday_unconfigured_returns_503_and_empty_state():
    with TestClient(create_app(None)) as c:
        body = c.get("/api/friday/state").json()
        assert body["configured"] is False
        assert c.post("/api/friday/scan").status_code == 503
        assert c.get("/api/friday/chart/000001").status_code == 503
        assert c.get("/api/friday/report").status_code == 503


def test_state_is_json_serializable(tmp_path):
    sc, _ = scanner(tmp_path)
    rep = sc.scan()
    json.dumps([s.to_dict() for s in rep.signals], default=str)


def test_friday_evidence_endpoint_and_gnb(client):
    ev = client.get("/api/friday/evidence").json()["proxy"]
    assert ev["그룹"][0]["그룹"].startswith("신호 · 주말") and ev["민감도"] and ev["연도별"]
    assert '"/friday"' in client.get("/static/gnb.js").text
    for tag in ("/static/offcanvas.js", "/static/stockmenu.js", 'data-gnb="friday"'):
        assert tag in client.get("/friday").text
