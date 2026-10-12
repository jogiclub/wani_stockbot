"""웹 대시보드 API 테스트 (합성 데이터, 외부 API 불필요)."""

import time

import pytest
from fastapi.testclient import TestClient

from screener.config import StrategyConfig
from screener.data_loader import SyntheticDataLoader
from screener.web import ScanService, create_app
from screener.screener import Screener


@pytest.fixture
def client():
    loader = SyntheticDataLoader(n_symbols=4, signal_symbols=("900001",))
    cfg = StrategyConfig(min_avg_turnover=0.0, max_market_cap=0.0, min_price=0.0)
    app = create_app(loader, cfg, interval=3600, top=0, workers=2)
    with TestClient(app) as c:          # startup 이벤트로 첫 스캔이 백그라운드에서 돈다
        _wait_idle(app.state.service)
        yield c


def _wait_idle(service, timeout: float = 15.0) -> None:
    """백그라운드 스캔이 한 바퀴 끝날 때까지 기다린다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service.state.finished_at and not service.state.running:
            return
        time.sleep(0.05)
    raise AssertionError("스캔이 제한 시간 안에 끝나지 않음")


def test_index_serves_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "세력 추정 매매법 스크리너" in r.text


def test_healthz(client):
    assert client.get("/healthz").json()["ok"] is True


def test_state_contains_signal(client):
    body = client.get("/api/state").json()
    assert body["scanned"] == 4
    assert [s["종목코드"] for s in body["signals"]] == ["900001"]
    assert body["signals"][0]["매수가"] > body["signals"][0]["손절가"]


def test_chart_endpoint(client):
    body = client.get("/api/chart/900001?count=10").json()
    assert len(body["candles"]) == 10
    assert set(body["candles"][0]) == {"date", "open", "high", "low", "close", "volume"}


def test_config_endpoint(client):
    cfg = client.get("/api/config").json()
    assert cfg["upper_tail_min"] == 0.35
    assert cfg["markets"] == ["KOSPI", "KOSDAQ"]


def test_manual_rescan(client):
    assert client.post("/api/scan").json()["ok"] is True
    _wait_idle(client.app.state.service)


def test_scan_service_is_reentrant_safe():
    """스캔이 진행 중이면 중복 실행하지 않고 현재 상태를 그대로 돌려준다."""
    loader = SyntheticDataLoader(n_symbols=2, signal_symbols=())
    cfg = StrategyConfig(min_avg_turnover=0.0, max_market_cap=0.0, min_price=0.0)
    svc = ScanService(Screener(loader, cfg, max_workers=1), interval=9999, top=0)
    svc._lock.acquire()
    try:
        assert svc.scan_once() is svc.state          # 락이 잡혀 있으면 즉시 반환
        assert svc.state.finished_at is None
    finally:
        svc._lock.release()


def test_guide_endpoint(client):
    g = client.get("/api/guide").json()
    assert g["컷오프점수"] == 75
    assert len(g["단계"]) == 3
    assert sum(f["배점"] for f in g["요인"]) == 100          # 배점 합 = 100
    assert [p["이름"] for p in g["프로파일"] if p["채택"]] == ["tuned (운영 기본)"]
    assert [s["이름"] for s in g["손절방식"] if s["채택"]] == ["기준봉 저가"]
    assert [r["이름"] for r in g["국면"] if r["채택"]] == ["KOSDAQ MA20"]
    assert g["주의"] and all(isinstance(c, str) for c in g["주의"])


def test_guide_grade_table_matches_scoring_grades(client):
    """가이드에 노출되는 등급 표가 scoring.GRADES 와 어긋나지 않는다."""
    from screener.scoring import GRADES

    g = client.get("/api/guide").json()
    assert [x["등급"] for x in g["등급"]] == [name for _, name, _ in GRADES]
    assert {x["등급"] for x in g["등급성과"]} <= {name for _, name, _ in GRADES}


def test_dashboard_html_has_guide_tab(client):
    html = client.get("/").text
    assert 'data-tab="guide"' in html
    assert "renderGuide" in html and "posCalc" in html


def test_watch_excludes_symbols_that_already_signaled(client):
    """시그널이 뜬 종목은 '돌파 대기' 에 다시 나오지 않는다 (둘이 겹쳐 혼동됐던 회귀)."""
    body = client.get("/api/state").json()
    signaled = {s["종목코드"] for s in body["signals"]}
    assert signaled == {"900001"}
    assert signaled.isdisjoint(w["종목코드"] for w in body["watch"])


def test_watch_trigger_is_prev_high_like_signal():
    """감시 돌파가는 판정일의 전일 고가다 — 시그널의 전일고가와 같은 값이어야 한다.

    예전에는 마지막 봉 자신의 고가를 '전일고가' 라벨로 내보내 값이 어긋났다.
    """
    loader = SyntheticDataLoader(n_symbols=1, signal_symbols=("900001",))
    cfg = StrategyConfig(min_avg_turnover=0.0, max_market_cap=0.0, min_price=0.0)
    scr = Screener(loader, cfg, max_workers=1)
    sig = scr.scan().signals[0]
    watch = scr.watch_candidates()            # exclude 없이 부르면 같은 종목이 잡힌다
    assert [w["종목코드"] for w in watch] == ["900001"]
    assert watch[0]["감시_돌파가(전일고가)"] == round(sig.prev_high, 2)
    assert watch[0]["판정일"] == sig.signal_date.strftime("%Y-%m-%d")
    assert scr.watch_candidates(exclude={"900001"}) == []


def test_dashboard_has_stock_menu(client):
    """종목 메뉴는 공통 모듈(stockmenu.js)이다 — 세력추정·수급포착 두 화면이 함께 쓴다."""
    for page in ("/", "/supply", "/friday"):
        assert "/static/stockmenu.js" in client.get(page).text
    js = client.get("/static/stockmenu.js").text
    assert 'id="stock-menu"' in js
    assert "토스로 보기" in js and "반전 매매에 종목추가" in js
    assert "/api/bollinger/symbols" in js
