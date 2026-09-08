"""주문 실행 테스트.

실주문이 절대 나가지 않도록, 가짜 클라이언트는 호출되면 안 되는 지점에서
바로 실패하게 만들어 두었다.
"""

import pytest
from fastapi.testclient import TestClient

from screener.config import StrategyConfig
from screener.data_loader import SyntheticDataLoader
from screener.trading import TradingError, TradingService, round_tick, tick_size
from screener.web import create_app

BODY = {"symbol": "010580", "name": "에스엠벡셀", "quantity": 393,
        "entry": 1762, "stop": 1508, "target": 2016}


class FakeClient:
    """주문 API 를 흉내 내는 클라이언트. 호출 내역을 기록한다."""

    def __init__(self, fail_order: bool = False):
        self.orders: list[dict] = []
        self.conditionals: list[dict] = []
        self.fail_order = fail_order

    def accounts(self):
        return [{"accountNo": "12345678901", "accountSeq": 7, "accountType": "BROKERAGE"}]

    def buying_power(self, account_seq, currency="KRW"):
        return {"currency": currency, "cashBuyingPower": "5000000"}

    def create_order(self, account_seq, body):
        if self.fail_order:
            from screener.toss_client import TossAPIError
            raise TossAPIError(400, "invalid-tick-size", "호가 단위가 올바르지 않습니다")
        self.orders.append({"account": account_seq, **body})
        return {"orderId": "ORD-1"}

    def get_order(self, account_seq, order_id):
        return {"orderId": order_id, "status": "FILLED", "execution": {"filledQuantity": "393"}}

    def create_conditional_order(self, account_seq, body):
        self.conditionals.append({"account": account_seq, **body})
        return {"conditionalOrderId": "COND-1"}


class ExplodingClient(FakeClient):
    """실주문이 시도되면 테스트를 실패시킨다."""

    def create_order(self, *a, **k):
        raise AssertionError("실주문이 나가면 안 되는 경로에서 create_order 가 호출됨")

    def create_conditional_order(self, *a, **k):
        raise AssertionError("실주문이 나가면 안 되는 경로에서 조건주문이 호출됨")


@pytest.fixture
def client_off():
    """실거래 OFF 상태의 앱."""
    svc = TradingService(ExplodingClient(), enabled=False, max_order_amount=1_000_000)
    app = create_app(SyntheticDataLoader(n_symbols=2), StrategyConfig.tuned(),
                     interval=9999, top=0, trading=svc)
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------ 호가 단위
@pytest.mark.parametrize("price,tick", [
    (1_500, 1), (3_950, 5), (10_730, 10), (45_120, 50),
    (123_400, 100), (340_000, 500), (820_000, 1_000),
])
def test_tick_size_table(price, tick):
    assert tick_size(price) == tick


def test_round_tick_aligns_to_grid():
    assert round_tick(45_121, "down") == 45_100
    assert round_tick(45_121, "up") == 45_150
    assert round_tick(2_003, "down") == 2_000
    assert round_tick(0.4, "down") == 1          # 최소 1틱은 보장


# ------------------------------------------------------------------ 검증
def _svc(**kw):
    kw.setdefault("enabled", False)
    kw.setdefault("max_order_amount", 1_000_000)
    return TradingService(kw.pop("client", ExplodingClient()), **kw)


def test_plan_rounds_prices_to_tick():
    # 2,000~5,000원 구간은 5원 호가라 2,016.7 → 2,015 로 내림 정렬된다
    plan = _svc().build_plan("010580", "테스트", 100, 1762.4, 1508.9, 2016.7)
    assert (plan.entry, plan.stop, plan.target) == (1762, 1508, 2015)
    assert plan.amount == 176_200
    assert plan.risk_amount == 100 * (1762 - 1508)


def test_plan_rejects_bad_price_order():
    with pytest.raises(TradingError, match="가격 순서"):
        _svc().build_plan("010580", "테스트", 10, 1000, 1200, 900)


def test_plan_rejects_zero_quantity():
    with pytest.raises(TradingError, match="수량"):
        _svc().build_plan("010580", "테스트", 0, 1000, 900, 1200)


def test_plan_rejects_non_krx_symbol():
    with pytest.raises(TradingError, match="6자리"):
        _svc().build_plan("AAPL", "Apple", 1, 100, 90, 120)


def test_plan_rejects_over_limit():
    with pytest.raises(TradingError, match="한도"):
        _svc(max_order_amount=100_000).build_plan("010580", "테스트", 393, 1762, 1508, 2016)


# ------------------------------------------------------------------ 실행
def test_dry_run_never_calls_api(tmp_path):
    svc = _svc(log_path=tmp_path / "orders.jsonl")     # ExplodingClient
    out = svc.submit(dry_run=True, symbol="010580", name="테스트", quantity=10,
                     entry=1762, stop=1508, target=2016)
    assert out.ok and out.dry_run and out.order_id is None
    assert [s["상태"] for s in out.steps] == ["모의", "모의"]
    assert (tmp_path / "orders.jsonl").read_text()     # 기록은 남는다


def test_live_submit_refused_when_disabled():
    with pytest.raises(TradingError, match="실거래가 꺼져"):
        _svc().submit(dry_run=False, symbol="010580", name="테스트", quantity=10,
                      entry=1762, stop=1508, target=2016)


def test_live_submit_places_order_and_oco(tmp_path):
    fake = FakeClient()
    svc = TradingService(fake, enabled=True, max_order_amount=1_000_000,
                         log_path=tmp_path / "orders.jsonl", poll_interval=0.01)
    out = svc.submit(dry_run=False, symbol="010580", name="테스트", quantity=393,
                     entry=1762, stop=1508, target=2016)
    assert out.ok and out.order_id == "ORD-1"

    order = fake.orders[0]
    assert order["side"] == "BUY" and order["orderType"] == "LIMIT"
    assert order["price"] == "1762" and order["quantity"] == "393"
    assert order["clientOrderId"].startswith("sf-010580-")     # 멱등키

    # 체결 감시 스레드가 OCO 를 등록할 때까지 기다린다
    for _ in range(200):
        if fake.conditionals:
            break
        import time
        time.sleep(0.02)
    oco = fake.conditionals[0]
    assert oco["type"] == "OCO" and oco["orderType"] == "LIMIT"
    assert oco["quantity"] == "393"
    # 두 leg 모두 매도여야 하고, orderSide 가 빠지면 실서버가 400 을 낸다
    assert oco["first"]["orderSide"] == oco["second"]["orderSide"] == "SELL"
    # OCO 규칙: first(익절) 감시가 > second(손절) 감시가
    assert int(oco["first"]["triggerPrice"]) == 2015      # 호가 정렬 결과
    assert int(oco["second"]["triggerPrice"]) == 1508
    assert int(oco["first"]["triggerPrice"]) > int(oco["second"]["triggerPrice"])


def test_order_api_failure_is_reported(tmp_path):
    svc = TradingService(FakeClient(fail_order=True), enabled=True,
                         max_order_amount=1_000_000, log_path=tmp_path / "o.jsonl")
    out = svc.submit(dry_run=False, symbol="010580", name="테스트", quantity=10,
                     entry=1762, stop=1508, target=2016)
    assert not out.ok and "invalid-tick-size" in out.message


# ------------------------------------------------------------------ HTTP
def test_status_endpoint_masks_account(client_off):
    st = client_off.get("/api/trading/status").json()
    assert st["configured"] is True and st["enabled"] is False
    assert st["account"]["accountNo"] == "1234*****01"        # 계좌번호 마스킹


def test_http_dry_run(client_off):
    r = client_off.post("/api/trading/order", json={**BODY, "dry_run": True}).json()
    assert r["ok"] and r["dry_run"]
    assert r["plan"]["amount"] == 393 * 1762


def test_http_live_requires_confirm(client_off):
    r = client_off.post("/api/trading/order", json={**BODY, "dry_run": False})
    assert r.status_code == 400 and "confirm" in r.json()["detail"]


def test_http_live_blocked_when_disabled(client_off):
    r = client_off.post("/api/trading/order",
                        json={**BODY, "dry_run": False, "confirm": "실행"})
    assert r.status_code == 400 and "실거래가 꺼져" in r.json()["detail"]


def test_http_defaults_to_dry_run(client_off):
    """dry_run 을 빼먹어도 실주문이 나가지 않는다 (ExplodingClient 가 보증)."""
    r = client_off.post("/api/trading/order", json=BODY).json()
    assert r["dry_run"] is True


def test_http_rejects_unknown_fields(client_off):
    r = client_off.post("/api/trading/order", json={**BODY, "side": "SELL", "dry_run": True})
    assert r.status_code == 200                               # side 는 무시된다
    assert r.json()["plan"]["symbol"] == "010580"


def test_trading_absent_returns_503():
    app = create_app(SyntheticDataLoader(n_symbols=2), StrategyConfig.tuned(),
                     interval=9999, top=0)
    with TestClient(app) as c:
        assert c.get("/api/trading/status").json() == {"configured": False, "enabled": False}
        assert c.post("/api/trading/preview", json=BODY).status_code == 503
