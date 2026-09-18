"""볼린저 자동매매 봇 · 웹 API 테스트.

실주문이 절대 새어 나가지 않도록, 실주문이 나가면 안 되는 경로에서는
`create_order` 가 호출되는 순간 테스트가 실패하도록 만들었다.
"""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from screener.bollinger import BollingerConfig, Position
from screener.bollinger_bot import BollingerBot, market_is_open
from screener.config import StrategyConfig
from screener.data_loader import SyntheticDataLoader
from screener.trading import TradingService
from screener.web import create_app


# --------------------------------------------------------------- 가짜 클라이언트
def _flat_then_dump(n=390):
    """횡보하다 마지막 봉에서 급락 — 하한선 이탈 + RSI 과매도를 만든다.

    (선형 하락으로 만들면 이동평균과 표준편차가 함께 따라 내려가 밴드를 못 벗어난다.)
    """
    close = np.full(n, 10_000.0)
    close[-1] = 9_000.0
    return close


class FakeClient:
    def __init__(self, close=None, cash="5000000", holdings_items=()):
        self.close = _flat_then_dump() if close is None else close
        self.cash = cash
        self.holdings_items = list(holdings_items)
        self.orders: list[dict] = []

    # -- 시세 --
    def candles_paged(self, symbol, interval="1d", total=400, adjusted=True):
        # 정규장 안에서 끝나는 봉을 준다 — 봇이 시간외 봉을 걸러내기 때문에
        # 마지막 봉이 15:30 을 넘으면 판정에서 빠진다.
        n = min(total, len(self.close))
        idx = pd.date_range(end="2026-09-08 15:30", periods=n, freq="1min")
        c = self.close[-n:]
        return [{"timestamp": t.isoformat() + "+09:00", "openPrice": str(v),
                 "highPrice": str(v), "lowPrice": str(v), "closePrice": str(v),
                 "volume": "1000"} for t, v in zip(idx, c)]

    def prices(self, symbols):
        return [{"symbol": s, "lastPrice": str(self.close[-1])} for s in symbols]

    def stocks(self, symbols):
        return [{"symbol": s, "name": "테스트종목"} for s in symbols]

    # -- 계좌 --
    def accounts(self):
        return [{"accountNo": "12345678901", "accountSeq": 7, "accountType": "BROKERAGE"}]

    def buying_power(self, account_seq, currency="KRW"):
        return {"currency": "KRW", "cashBuyingPower": self.cash}

    def holdings(self, account_seq, symbol=None):
        # 실제 응답처럼 금액을 통화별 dict 로 준다
        return {"totalPurchaseAmount": {"krw": "1000000", "usd": "0"},
                "marketValue": {"amount": {"krw": "1100000", "usd": "0"}},
                "profitLoss": {"amount": {"krw": "100000", "usd": "0"}, "rate": "0.1"},
                "items": self.holdings_items}

    def sellable_quantity(self, account_seq, symbol):
        for it in self.holdings_items:
            if it["symbol"] == symbol:
                return {"sellableQuantity": it["quantity"]}
        return {"sellableQuantity": "0"}

    def create_order(self, account_seq, body):
        self.orders.append({"account": account_seq, **body})
        return {"orderId": "ORD-1", "status": "PENDING"}

    # -- 주문 조회/취소 (지정가 미체결 회수용). 기본은 계속 미체결. --
    def get_order(self, account_seq, order_id):
        return {"status": "PENDING", "execution": {"filledQuantity": "0"}}

    def cancel_order(self, account_seq, order_id):
        self.canceled = getattr(self, "canceled", [])
        self.canceled.append(order_id)
        return {"ok": True}


class ExplodingClient(FakeClient):
    def create_order(self, *a, **k):
        raise AssertionError("실주문이 나가면 안 되는 경로에서 create_order 가 호출됨")


@pytest.fixture(autouse=True)
def always_market_open(monkeypatch):
    """장중 판정을 벽시계에 의존시키지 않는다 (주말·야간에도 테스트가 돌아야 한다).

    `test_market_hours_window` 는 원본 함수를 직접 부르므로 이 패치의 영향을 받지 않는다.
    """
    monkeypatch.setattr("screener.bollinger_bot.market_is_open", lambda now=None: True)


def make_bot(client=None, *, enabled=False, live=False, symbols=("005930",),
             max_order_amount=1_000_000, **cfg_kw):
    client = client or FakeClient()
    trading = TradingService(client, enabled=enabled, max_order_amount=max_order_amount,
                             account_seq=7)
    cfg = BollingerConfig(interval="1m", symbols=symbols, **cfg_kw)
    bot = BollingerBot(client, trading, cfg, live=live)
    bot.state.market_open = True                 # _act 를 직접 부르는 테스트용
    return bot, client


# --------------------------------------------------------------- 실주문 게이트
def test_dry_run_never_calls_order_api():
    bot, client = make_bot(ExplodingClient(), enabled=True, live=False)
    bot.run_once()
    assert bot.live is False


def test_live_switch_requires_trading_enabled():
    """봇 스위치를 켜도 주문 서비스가 꺼져 있으면 실주문이 되지 않는다."""
    bot, _ = make_bot(ExplodingClient(), enabled=False, live=True)
    assert bot.live is False
    assert bot.set_live(True) is False
    bot.run_once()                               # ExplodingClient 라 실주문이면 실패한다


def test_live_bot_places_limit_buy():
    """매수는 지정가로 낸다 — 현재가보다 buy_slippage 만큼 위를 상한으로."""
    bot, client = make_bot(enabled=True, live=True, cash_ratio=0.5, min_order_amount=10_000,
                           buy_slippage=0.005)
    close = client.close[-1]
    bot.run_once()
    assert len(client.orders) == 1
    order = client.orders[0]
    assert order["side"] == "BUY" and order["orderType"] == "LIMIT"
    assert int(order["price"]) >= close          # 현재가 이상(슬리피지 상한) — 지정가
    assert int(order["price"]) <= close * 1.006  # 대략 +0.5% 안쪽(호가 올림 여유)
    assert int(order["quantity"]) > 0
    assert order["clientOrderId"].startswith("blL-005930-B-")   # 지정가 태그(L)


def test_buy_amount_respects_order_cap():
    """예수금 비율이 커도 1회 주문 한도를 넘지 않는다."""
    bot, client = make_bot(enabled=True, live=True, cash_ratio=1.0,
                           max_order_amount=100_000, min_order_amount=10_000)
    bot.run_once()
    order = client.orders[0]
    assert int(order["quantity"]) * 9_000 <= 100_000


def test_no_order_outside_market_hours(monkeypatch):
    monkeypatch.setattr("screener.bollinger_bot.market_is_open", lambda now=None: False)
    bot, client = make_bot(ExplodingClient(), enabled=True, live=True)
    bot.state.market_open = False
    bot._sync_account()
    d = bot._evaluate("005930")
    assert d is not None and d.action == "BUY"
    result = bot._act(d)
    assert result["skipped"] is True and "정규장" in result["message"]


def test_max_positions_blocks_new_entry():
    client = FakeClient(holdings_items=[
        {"symbol": "000660", "name": "보유중", "quantity": "10", "averagePurchasePrice": "10000"}])
    bot, client = make_bot(client, enabled=True, live=True, max_positions=1,
                           symbols=("005930", "000660"),
                           cash_ratio=0.5, min_order_amount=10_000)
    bot.run_once()
    # 이미 1종목(000660) 보유 → 005930 신규 진입은 차단된다 (000660 손절 매도는 별개)
    assert not any(o["side"] == "BUY" for o in client.orders)
    assert any("동시 보유 상한" in (e.get("내용") or "") for e in bot.state.events)


def test_stop_loss_sells_entire_position():
    """평단 대비 −5% 이탈이면 손절 전량 매도. 밴드·RSI 조건과 무관하다."""
    client = FakeClient(holdings_items=[
        {"symbol": "005930", "name": "테스트종목", "quantity": "10",
         "averagePurchasePrice": "10000"}])
    bot, client = make_bot(client, enabled=True, live=True, stop_loss_pct=0.05,
                           manage_existing=True)
    bot.run_once()
    assert len(client.orders) == 1
    order = client.orders[0]
    assert order["side"] == "SELL" and order["quantity"] == "10"
    assert any(e["액션"] == "STOP" for e in bot.state.events)


def test_sell_is_capped_by_sellable_quantity():
    client = FakeClient(holdings_items=[
        {"symbol": "005930", "name": "테스트종목", "quantity": "10",
         "averagePurchasePrice": "10000"}])
    client.sellable_quantity = lambda a, s: {"sellableQuantity": "4"}
    bot, client = make_bot(client, enabled=True, live=True, stop_loss_pct=0.05,
                           manage_existing=True)
    bot.run_once()
    assert client.orders[0]["quantity"] == "4"


def test_positions_are_restored_from_account():
    """감시 목록 안의 보유분은 계좌 잔고에서 복구한다 (재시작·수동 매수 대비)."""
    client = FakeClient(holdings_items=[
        {"symbol": "000660", "name": "하이닉스", "quantity": "7", "averagePurchasePrice": "18000"}])
    bot, _ = make_bot(client, enabled=False, symbols=("000660",))
    bot._sync_account()
    pos = bot.positions["000660"]
    assert pos.quantity == 7 and pos.entry_price == 18_000 and pos.adopted is True
    assert bot.state.adopted == ["000660"]      # 화면에 경고를 띄우기 위한 표시


def test_existing_holdings_are_not_liquidated_by_default():
    """감시 목록에 넣은 **장기 보유 종목**을 첫 주기에 털어 버리면 안 된다.

    기본값 `manage_existing=False` 에서는 판정만 하고 매도 주문을 내지 않는다.
    """
    client = ExplodingClient(holdings_items=[
        {"symbol": "005930", "name": "테스트종목", "quantity": "10",
         "averagePurchasePrice": "10000"}])
    bot, _ = make_bot(client, enabled=True, live=True, stop_loss_pct=0.05)
    assert bot.cfg.manage_existing is False
    bot.run_once()                               # ExplodingClient — 주문이 나가면 실패한다
    assert any("봇이 매수한 물량이 아닙니다" in (e.get("내용") or "") for e in bot.state.events)


def test_bot_bought_position_is_always_managed():
    """봇이 직접 산 물량은 manage_existing 과 무관하게 손절·청산 대상이다."""
    client = FakeClient(holdings_items=[])
    bot, client = make_bot(client, enabled=True, live=True, cash_ratio=0.5,
                           min_order_amount=10_000, stop_loss_pct=0.05)
    bot.run_once()                               # 매수 (adopted=False 로 기록)
    assert bot.positions["005930"].adopted is False

    # 다음 주기: 계좌에 손실 상태로 잡히면 손절이 나간다
    client.holdings_items = [{"symbol": "005930", "name": "테스트종목",
                              "quantity": "10", "averagePurchasePrice": "10000"}]
    client.orders.clear()
    bot.run_once()
    assert client.orders and client.orders[0]["side"] == "SELL"


def test_unwatched_holdings_are_never_managed():
    """감시 목록 밖 보유분은 관리 대상이 아니다 — 봇이 사지 않은 물량을 팔면 안 된다."""
    client = FakeClient(holdings_items=[
        {"symbol": "035720", "name": "카카오", "quantity": "100", "averagePurchasePrice": "50000"},
        {"symbol": "AAPL", "name": "애플", "quantity": "3", "averagePurchasePrice": "200"}])
    bot, _ = make_bot(client, enabled=False, symbols=("005930",))
    bot._sync_account()
    assert "035720" not in bot.positions and "AAPL" not in bot.positions
    assert bot.state.adopted == []


def test_unwatched_holdings_do_not_consume_position_slots():
    client = FakeClient(holdings_items=[
        {"symbol": "035720", "name": "카카오", "quantity": "100", "averagePurchasePrice": "50000"}])
    bot, client = make_bot(client, enabled=True, live=True, max_positions=1,
                           cash_ratio=0.5, min_order_amount=10_000)
    bot.run_once()
    assert len(client.orders) == 1 and client.orders[0]["side"] == "BUY"


def test_equity_reads_krw_from_currency_map():
    """금액 노드는 통화별 dict 다 — `amount` 키를 찾으면 항상 0 이 된다."""
    bot, _ = make_bot(enabled=False)
    bot._sync_account()
    assert bot.state.equity["투자원금"] == 1_000_000
    assert bot.state.equity["평가금액"] == 1_100_000
    assert bot.state.equity["평가손익"] == 100_000


def test_cleared_position_is_zeroed():
    bot, _ = make_bot(FakeClient(holdings_items=[]), enabled=False)
    bot.positions["005930"] = Position("005930", quantity=5, entry_price=1000, mid_taken=True)
    bot._sync_account()
    assert bot.positions["005930"].quantity == 0
    assert bot.positions["005930"].mid_taken is False


def test_market_hours_window():
    """정규장 창은 실제 함수로 검증한다.

    모듈 상단에서 import 한 이름은 autouse 패치(모듈 속성 교체)의 영향을 받지 않으므로
    여기서 다시 import 하면 안 된다.
    """
    import datetime as dt
    assert market_is_open(dt.datetime(2026, 9, 8, 10, 0)) is True      # 화요일 장중
    assert market_is_open(dt.datetime(2026, 9, 8, 8, 59)) is False     # 개장 전
    assert market_is_open(dt.datetime(2026, 9, 8, 15, 31)) is False    # 마감 후
    assert market_is_open(dt.datetime(2026, 9, 12, 10, 0)) is False    # 토요일


# ------------------------------------------------------------------ 웹 API
@pytest.fixture
def client():
    bot, _ = make_bot(ExplodingClient(), enabled=False, live=False)
    app = create_app(SyntheticDataLoader(n_symbols=2), StrategyConfig.tuned(),
                     interval=9999, top=0, bollinger=bot)
    with TestClient(app) as c:
        c.bot = bot
        yield c


def test_bollinger_page_is_served(client):
    r = client.get("/bollinger")
    assert r.status_code == 200
    assert "볼린저 밴드 반전 매매" in r.text
    assert 'data-gnb="bollinger"' in r.text


def test_gnb_appears_on_both_pages(client):
    for path in ("/", "/bollinger"):
        assert 'data-gnb' in client.get(path).text
    gnb = client.get("/static/gnb.js")
    assert gnb.status_code == 200
    assert "왔니 주식" in gnb.text
    assert "세력추정매매" in gnb.text and "볼린저 밴드 반전 매매" in gnb.text


def test_state_reports_config_and_mode(client):
    body = client.get("/api/bollinger/state").json()
    assert body["configured"] is True
    assert body["live"] is False and body["running"] is False
    assert body["config"]["bb_period"] == 20 and body["config"]["rsi_period"] == 14


def test_live_switch_needs_confirm_and_server_switch(client):
    assert client.post("/api/bollinger/live", json={"live": True}).status_code == 400
    r = client.post("/api/bollinger/live", json={"live": True, "confirm": "실행"})
    assert r.status_code == 403                   # 서버 실거래 스위치가 꺼져 있다
    assert client.get("/api/bollinger/state").json()["live"] is False


def test_config_update_over_http(client):
    r = client.post("/api/bollinger/config",
                    json={"symbols": "005930 000660", "stop_loss_pct": 0.03,
                          "interval": "30m", "poll_interval": 120})
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert cfg["symbols"] == ["005930", "000660"]
    assert cfg["stop_loss_pct"] == 0.03 and cfg["interval"] == "30m"
    assert r.json()["poll_interval"] == 120


def test_config_rejects_bad_interval(client):
    assert client.post("/api/bollinger/config", json={"interval": "7m"}).status_code == 400


def test_start_requires_symbols(client):
    client.post("/api/bollinger/config", json={"symbols": ""})
    assert client.post("/api/bollinger/start").status_code == 400


def test_start_and_stop(client):
    client.post("/api/bollinger/config", json={"symbols": "005930"})
    assert client.post("/api/bollinger/start").json()["running"] is True
    assert client.post("/api/bollinger/stop").json()["running"] is False


def test_endpoints_503_without_bot():
    app = create_app(SyntheticDataLoader(n_symbols=2), StrategyConfig.tuned(),
                     interval=9999, top=0)
    with TestClient(app) as c:
        assert c.get("/api/bollinger/state").json()["configured"] is False
        assert c.post("/api/bollinger/start").status_code == 503
        assert c.post("/api/bollinger/config", json={}).status_code == 503


def test_add_symbol_appends_without_touching_other_settings(client):
    client.post("/api/bollinger/config",
                json={"symbols": "005930", "stop_loss_pct": 0.03, "interval": "30m"})
    r = client.post("/api/bollinger/symbols", json={"symbol": "100130"})
    assert r.status_code == 200
    assert r.json()["added"] is True
    assert r.json()["symbols"] == ["005930", "100130"]
    cfg = client.get("/api/bollinger/state").json()["config"]
    assert cfg["symbols"] == ["005930", "100130"]
    assert cfg["stop_loss_pct"] == 0.03 and cfg["interval"] == "30m"   # 다른 설정 보존


def test_add_symbol_triggers_immediate_run(client):
    """종목을 추가하면 즉시 1회 판정(run_once)을 백그라운드로 돌린다 (반전매매 화면 지연 방지)."""
    import time
    calls: list[int] = []
    client.bot.run_once = lambda: calls.append(1)
    client.post("/api/bollinger/symbols", json={"symbol": "100130"})
    for _ in range(100):                       # 백그라운드 스레드가 부를 때까지 잠깐 기다린다
        if calls:
            break
        time.sleep(0.02)
    assert calls == [1]


def test_add_symbol_idempotent_skips_run(client):
    """이미 있는 종목이면 판정을 다시 돌리지 않는다."""
    import time
    client.post("/api/bollinger/config", json={"symbols": "005930"})
    calls: list[int] = []
    client.bot.run_once = lambda: calls.append(1)
    client.post("/api/bollinger/symbols", json={"symbol": "005930"})
    time.sleep(0.2)
    assert calls == []


def test_add_symbol_is_idempotent(client):
    client.post("/api/bollinger/config", json={"symbols": "005930"})
    r = client.post("/api/bollinger/symbols", json={"symbol": "005930"})
    assert r.json()["added"] is False
    assert r.json()["symbols"] == ["005930"]


@pytest.mark.parametrize("bad", ["", "5930", "00593A", "0059300", None])
def test_add_symbol_rejects_bad_code(client, bad):
    assert client.post("/api/bollinger/symbols", json={"symbol": bad}).status_code == 400


def test_add_symbol_without_bot_is_503():
    app = create_app(SyntheticDataLoader(n_symbols=1), StrategyConfig.tuned(),
                     interval=9999, top=0, bollinger=None)
    with TestClient(app) as c:
        assert c.post("/api/bollinger/symbols", json={"symbol": "005930"}).status_code == 503

def test_remove_symbol_drops_from_watch(client):
    client.post("/api/bollinger/config", json={"symbols": "005930,100130", "stop_loss_pct": 0.03})
    r = client.request("DELETE", "/api/bollinger/symbols", json={"symbol": "100130"})
    assert r.status_code == 200 and r.json()["removed"] is True
    assert r.json()["symbols"] == ["005930"]
    cfg = client.get("/api/bollinger/state").json()["config"]
    assert cfg["symbols"] == ["005930"] and cfg["stop_loss_pct"] == 0.03   # 다른 설정 보존


def test_remove_symbol_clears_decision_immediately(client):
    """삭제하면 감시 현황(decisions)에서도 즉시 빠진다 (다음 판정 주기를 기다리지 않는다)."""
    client.post("/api/bollinger/config", json={"symbols": "005930,100130"})
    client.bot.state.decisions = [{"종목코드": "005930", "액션": "HOLD"},
                                  {"종목코드": "100130", "액션": "HOLD"}]
    client.request("DELETE", "/api/bollinger/symbols", json={"symbol": "100130"})
    codes = [d["종목코드"] for d in client.get("/api/bollinger/state").json()["decisions"]]
    assert codes == ["005930"]


def test_remove_symbol_refuses_held_position(client):
    """보유 중(진행 중)인 종목은 감시 목록에서 삭제할 수 없다."""
    client.post("/api/bollinger/config", json={"symbols": "005930,100130"})
    client.bot.positions["100130"] = Position("100130", quantity=5, entry_price=1000)
    r = client.request("DELETE", "/api/bollinger/symbols", json={"symbol": "100130"})
    assert r.status_code == 409
    assert client.get("/api/bollinger/state").json()["config"]["symbols"] == ["005930", "100130"]


def test_remove_symbol_allows_zero_quantity_position(client):
    """전량 청산된(수량 0) 보유 흔적은 삭제를 막지 않는다."""
    client.post("/api/bollinger/config", json={"symbols": "005930"})
    client.bot.positions["005930"] = Position("005930", quantity=0)
    r = client.request("DELETE", "/api/bollinger/symbols", json={"symbol": "005930"})
    assert r.status_code == 200 and r.json()["symbols"] == []


@pytest.mark.parametrize("bad", ["", "5930", "00593A", None])
def test_remove_symbol_rejects_bad_code(client, bad):
    assert client.request("DELETE", "/api/bollinger/symbols",
                          json={"symbol": bad}).status_code == 400


# ------------------------------------------------------------------ 지정가 회수
def test_limit_buy_cancels_unfilled_after_timeout():
    """지정가 매수가 시한 안에 안 채워지면 미체결분을 회수(취소)한다."""
    import time
    from screener.trading import TradingService
    client = FakeClient()                       # get_order 가 계속 미체결을 준다
    trading = TradingService(client, enabled=True, max_order_amount=10_000_000, account_seq=7)
    out = trading.limit_buy(symbol="005930", quantity=10, limit_price=1000,
                            reference_price=990, cancel_after=0.05, dry_run=False)
    assert out["ok"] and client.orders[0]["orderType"] == "LIMIT"
    for _ in range(100):                        # 회수 스레드가 취소할 때까지 잠깐 기다린다
        if getattr(client, "canceled", None):
            break
        time.sleep(0.02)
    assert client.canceled == ["ORD-1"]


def test_limit_buy_keeps_filled_order():
    """이미 체결된 주문은 회수하지 않는다."""
    import time
    from screener.trading import TradingService
    client = FakeClient()
    client.get_order = lambda a, o: {"status": "FILLED", "execution": {"filledQuantity": "10"}}
    trading = TradingService(client, enabled=True, max_order_amount=10_000_000, account_seq=7)
    trading.limit_buy(symbol="005930", quantity=10, limit_price=1000,
                      cancel_after=0.05, dry_run=False)
    time.sleep(0.2)
    assert getattr(client, "canceled", []) == []
