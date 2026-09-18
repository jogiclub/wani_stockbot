"""세력추정매매 자동 매수 회귀 테스트 (실제 토스 API 호출 없음)."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from screener.autobuy import AutoBuyer
from screener.pattern_detector import BaseCandle, Signal
from screener.sessions import SessionSettings
from screener.trading import TradingService

NOW = datetime(2026, 9, 17, 10, 0)          # 목요일 장중


class FakeClient:
    def __init__(self, cash="10000000", held=()):
        self.cash, self.held = cash, held
        self.orders: list[dict] = []
        self.canceled: list[str] = []
        self.order_state = {"status": "PENDING", "execution": {"filledQuantity": "0"}}

    def accounts(self):
        return [{"accountSeq": 1, "accountNo": "12345678"}]

    def buying_power(self, account, currency="KRW"):
        return {"currency": "KRW", "cashBuyingPower": self.cash}

    def holdings(self, account, symbol=None):
        return {"items": [{"symbol": s, "quantity": "3"} for s in self.held]}

    def create_order(self, account, body):
        self.orders.append(body)
        return {"orderId": f"o{len(self.orders)}"}

    def get_order(self, account, order_id):
        return self.order_state

    def cancel_order(self, account, order_id):
        self.canceled.append(order_id)
        return {}


def signal(symbol="123456", score=80, entry=10_000, last=10_200, day=NOW) -> Signal:
    ts = pd.Timestamp(day.date())
    base = BaseCandle(ts, 9000, 11000, 9000, 9500, 1e6, 0.5, 30, True, -0.4)
    return Signal(symbol=symbol, name="테스트", signal_date=ts, base=base,
                  prev_high=entry, trigger_price=entry, last_price=last, volume=1e6,
                  volume_ratio_prev=1.5, entry=entry, stop=9_000,
                  targets=(11_000, 12_000, 13_000), adjust_days=2, adjust_vol_ratio=0.15,
                  score=SimpleNamespace(total=score))


def buyer(tmp_path, client, *, enabled=True, amount=1_000_000, max_amount=10_000_000,
          now=NOW, score=75):
    trading = TradingService(client, enabled=enabled, max_order_amount=max_amount,
                             log_path=tmp_path / "orders.jsonl")
    trading._attach_oco_when_filled = lambda *a, **k: None     # 체결 감시 스레드 생략
    return AutoBuyer(trading, min_score=score, amount=amount, clock=lambda: now)


def test_buys_max_quantity_within_amount(tmp_path):
    c = FakeClient()
    out = buyer(tmp_path, c, amount=1_050_000).handle([signal()])
    assert out[0]["ok"] and out[0]["quantity"] == 105
    assert c.orders[0]["price"] == "10000" and c.orders[0]["quantity"] == "105"


def test_skips_below_score_and_buys_once_per_day(tmp_path):
    c = FakeClient()
    b = buyer(tmp_path, c)
    assert b.handle([signal(score=74)]) == []
    b.handle([signal(score=75)])
    b.handle([signal(score=90)])
    assert len(c.orders) == 1
    # 재시작해도 오늘 기록을 읽어 다시 사지 않는다
    assert buyer(tmp_path, c).handle([signal()]) == []
    assert len(c.orders) == 1


def test_amount_capped_by_order_limit_and_cash(tmp_path):
    c = FakeClient(cash="300000")
    buyer(tmp_path, c, amount=5_000_000, max_amount=1_000_000).handle([signal()])
    assert c.orders[0]["quantity"] == "30"


def test_outside_market_hours_and_old_signal(tmp_path):
    c = FakeClient()
    assert buyer(tmp_path, c, now=datetime(2026, 9, 17, 15, 25)).handle([signal()]) == []
    assert buyer(tmp_path, c, now=datetime(2026, 9, 19, 10, 0)).handle([signal()]) == []  # 토요일
    old = signal(day=datetime(2026, 9, 16))
    assert buyer(tmp_path, c).handle([old]) == []
    assert c.orders == []


def test_skips_held_symbol_and_chased_price(tmp_path):
    c = FakeClient(held=("123456",))
    b = buyer(tmp_path, c)
    assert b.handle([signal()]) == []
    out = b.handle([signal(symbol="654321", last=11_000)])      # 이미 1R 도달
    assert out[0]["ok"] is False and c.orders == []


def test_disabled_trading_is_dry_run(tmp_path):
    c = FakeClient()
    out = buyer(tmp_path, c, enabled=False).handle([signal()])
    assert out[0]["dry_run"] and out[0]["ok"] and c.orders == []
    rec = [json.loads(x) for x in (tmp_path / "orders.jsonl").read_text().splitlines()]
    assert any(r["event"] == "auto_buy" for r in rec)
    # 모의 기록은 재시작 후 실주문을 막지 않는다
    assert buyer(tmp_path, c, enabled=True).handle([signal()])[0]["dry_run"] is False


def test_unfilled_order_is_canceled_on_timeout(tmp_path):
    c = FakeClient()
    t = TradingService(c, enabled=True, log_path=tmp_path / "o.jsonl", poll_interval=0)
    plan = t.build_plan(symbol="123456", name="x", quantity=1, entry=10000,
                        stop=9000, target=11000)
    t._attach_oco_when_filled(1, "o1", plan, timeout=0.01, cancel_on_timeout=True)
    assert c.canceled == ["o1"]
    t._attach_oco_when_filled(1, "o2", plan, timeout=0.01)        # 수동 주문은 취소 안 함
    assert c.canceled == ["o1"]


def test_settings_fields():
    s = SessionSettings.from_body({"client_id": "tsck_live_abcd1234",
                                   "client_secret": "tssk_live_secret",
                                   "auto_buy": True, "auto_buy_score": 150,
                                   "auto_buy_amount": "500000"})
    assert (s.auto_buy, s.auto_buy_score, s.auto_buy_amount) == (True, 100, 500000)
    d = SessionSettings.from_body({"client_id": "tsck_live_abcd1234",
                                   "client_secret": "tssk_live_secret"})
    assert (d.auto_buy, d.auto_buy_score, d.auto_buy_amount) == (False, 75, 0)
