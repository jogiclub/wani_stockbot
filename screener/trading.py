"""토스증권 실주문 실행 (매수 + OCO 손절/익절 자동 등록).

⚠ 이 모듈은 **실제 돈이 나가는 주문**을 보낸다. 다음 안전장치를 반드시 거친다.

1. `TOSS_TRADING_ENABLED=true` 가 아니면 실주문은 거부되고 모의(dry-run)만 된다.
2. `TOSS_MAX_ORDER_AMOUNT` (기본 100만원) 를 넘는 주문은 서버에서 거부한다.
3. 모든 주문에 멱등키(`clientOrderId`)를 붙여 중복 체결을 막는다.
4. 주문 계열 POST 는 재시도하지 않는다 (`TossClient.post`).
5. 요청·응답을 전부 `out/orders.jsonl` 에 남긴다.

주문 흐름
    ① 지정가 매수 (전일 고가 = 돌파선)
    ② 체결 감시 (백그라운드 폴링)
    ③ 체결 수량만큼 OCO 조건주문 등록 — 익절(높은 감시가) / 손절(낮은 감시가)
       하나가 발동하면 나머지는 자동 취소된다.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .toss_client import TossAPIError, TossClient

log = logging.getLogger(__name__)

# KRX 호가 단위 (2023 개편 기준, 코스피·코스닥 공통)
TICK_TABLE = ((2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50),
              (200_000, 100), (500_000, 500), (float("inf"), 1_000))


def tick_size(price: float) -> int:
    for upper, tick in TICK_TABLE:
        if price < upper:
            return tick
    return 1_000


def round_tick(price: float, mode: str = "down") -> int:
    """호가 단위에 맞춰 가격을 정렬한다. 어긋나면 `invalid-tick-size` 로 거부된다."""
    tick = tick_size(price)
    q = price / tick
    if mode == "up":
        n = int(q) + (1 if q % 1 else 0)
    elif mode == "near":
        n = round(q)
    else:
        n = int(q)
    return max(tick, int(n) * tick)


class TradingError(RuntimeError):
    """주문 전 검증 실패 (사용자에게 그대로 보여줄 메시지)."""


@dataclass
class OrderPlan:
    """주문 1건의 계획. 화면에서 확인한 값을 그대로 담는다."""

    symbol: str
    name: str
    quantity: int
    entry: int          # 지정가 매수가 (호가 정렬 후)
    stop: int           # 손절 감시가
    target: int         # 익절 감시가
    expire_date: str    # 조건주문 만료일 (YYYY-MM-DD)

    @property
    def amount(self) -> int:
        return self.quantity * self.entry

    @property
    def risk_amount(self) -> int:
        return self.quantity * (self.entry - self.stop)

    @property
    def reward_amount(self) -> int:
        return self.quantity * (self.target - self.entry)

    def as_dict(self) -> dict:
        return {**asdict(self), "amount": self.amount,
                "risk_amount": self.risk_amount, "reward_amount": self.reward_amount}


@dataclass
class OrderOutcome:
    ok: bool
    dry_run: bool
    plan: dict
    steps: list[dict] = field(default_factory=list)
    order_id: str | None = None
    conditional_order_id: str | None = None
    message: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class TradingService:
    """주문 검증 → 매수 → 체결 감시 → OCO 등록."""

    def __init__(self, client: TossClient, *, enabled: bool = False,
                 max_order_amount: float = 1_000_000, account_seq: int | None = None,
                 log_path: Path | None = None, fill_timeout: float = 600.0,
                 poll_interval: float = 5.0):
        self.client = client
        self.enabled = enabled
        self.max_order_amount = max_order_amount
        self._account_seq = account_seq
        self.log_path = Path(log_path) if log_path else None
        self.fill_timeout = fill_timeout
        self.poll_interval = poll_interval
        self._accounts: list[dict] | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 계좌
    def accounts(self) -> list[dict]:
        if self._accounts is None:
            self._accounts = self.client.accounts()
        return self._accounts

    def account_seq(self) -> int:
        if self._account_seq is not None:
            return self._account_seq
        accounts = self.accounts()
        if not accounts:
            raise TradingError("주문 가능한 계좌가 없습니다. 토스증권에서 종합매매 계좌를 먼저 개설하세요.")
        self._account_seq = int(accounts[0]["accountSeq"])
        return self._account_seq

    def status(self) -> dict:
        info = {"enabled": self.enabled, "max_order_amount": self.max_order_amount,
                "account": None, "account_error": None}
        try:
            accounts = self.accounts()
            if accounts:
                a = accounts[0]
                info["account"] = {"accountNo": _mask(a.get("accountNo", "")),
                                   "accountSeq": a.get("accountSeq"),
                                   "accountType": a.get("accountType")}
        except Exception as exc:
            info["account_error"] = str(exc)
        return info

    # ------------------------------------------------------------ 검증
    def build_plan(self, symbol: str, name: str, quantity: int, entry: float,
                   stop: float, target: float, expire_days: int = 30) -> OrderPlan:
        """가격을 호가 단위에 맞추고 주문 계획을 만든다 (검증 포함)."""
        if not re.fullmatch(r"\d{6}", symbol or ""):
            raise TradingError("국내 주식(6자리 종목코드)만 지원합니다.")
        quantity = int(quantity)
        if quantity <= 0:
            raise TradingError("수량이 0입니다. 계좌 잔고 또는 허용 손실을 늘리세요.")

        plan = OrderPlan(
            symbol=symbol, name=name, quantity=quantity,
            entry=round_tick(entry, "down"),
            stop=round_tick(stop, "down"),
            target=round_tick(target, "down"),
            expire_date=(date.today() + timedelta(days=expire_days)).isoformat(),
        )
        if not (plan.stop < plan.entry < plan.target):
            raise TradingError(
                f"가격 순서가 잘못됐습니다 "
                f"(손절 {plan.stop:,} < 매수 {plan.entry:,} < 익절 {plan.target:,}).")
        if plan.amount > self.max_order_amount:
            raise TradingError(
                f"주문 금액 {plan.amount:,}원이 한도 {int(self.max_order_amount):,}원을 넘습니다. "
                "수량을 줄이거나 TOSS_MAX_ORDER_AMOUNT 를 조정하세요.")
        return plan

    def preview(self, **kwargs) -> dict:
        """실주문 없이 계획만 계산한다 (화면 확인용)."""
        plan = self.build_plan(**kwargs)
        out = {"plan": plan.as_dict(), "enabled": self.enabled}
        try:
            power = self.client.buying_power(self.account_seq())
            out["buying_power"] = power
            # 응답 키는 `cashBuyingPower` 다 (구버전 이름도 대비해 뒤에 남겨둔다)
            cash = _to_float(power.get("cashBuyingPower")
                             or power.get("orderableAmount") or power.get("cash"))
            if cash == cash and cash < plan.amount:
                out["warning"] = (f"매수 가능 금액({cash:,.0f}원)이 "
                                  f"주문 금액({plan.amount:,}원)보다 적습니다.")
        except Exception as exc:
            out["buying_power_error"] = str(exc)
        return out

    # ------------------------------------------------------------ 실행
    def submit(self, *, dry_run: bool = True, fill_timeout: float | None = None,
               cancel_on_timeout: bool = False, **kwargs) -> OrderOutcome:
        """매수 주문을 넣고, 체결되면 OCO(익절·손절)를 자동 등록한다.

        `cancel_on_timeout` 이면 `fill_timeout` 까지 다 체결되지 않은 잔량을 취소한다
        (자동매수용 — 손절 없는 늦은 체결을 남기지 않는다).
        """
        plan = self.build_plan(**kwargs)
        outcome = OrderOutcome(ok=False, dry_run=dry_run, plan=plan.as_dict())

        if dry_run:
            outcome.ok = True
            outcome.message = "모의 실행 — 실제 주문은 나가지 않았습니다."
            outcome.steps = [
                {"단계": "매수 주문", "상태": "모의",
                 "내용": f"지정가 {plan.entry:,}원 × {plan.quantity:,}주 = {plan.amount:,}원"},
                {"단계": "OCO 등록", "상태": "모의",
                 "내용": f"익절 {plan.target:,}원 / 손절 {plan.stop:,}원 "
                         f"(만료 {plan.expire_date})"},
            ]
            self._log(outcome)
            return outcome

        if not self.enabled:
            raise TradingError(
                "실거래가 꺼져 있습니다. .env 에 TOSS_TRADING_ENABLED=true 를 설정하고 "
                "컨테이너를 다시 띄우세요.")

        with self._lock:                      # 동시 중복 주문 방지
            account = self.account_seq()
            key = _client_order_id(plan)
            try:
                res = self.client.create_order(account, {
                    "clientOrderId": key,
                    "symbol": plan.symbol,
                    "side": "BUY",
                    "orderType": "LIMIT",
                    "timeInForce": "DAY",
                    "quantity": str(plan.quantity),
                    "price": str(plan.entry),
                })
            except TossAPIError as exc:
                outcome.message = f"매수 주문 실패: {exc}"
                outcome.steps.append({"단계": "매수 주문", "상태": "실패", "내용": str(exc)})
                self._log(outcome)
                return outcome

            outcome.order_id = res.get("orderId")
            outcome.steps.append({"단계": "매수 주문", "상태": "접수",
                                  "내용": f"주문번호 {outcome.order_id} · "
                                          f"지정가 {plan.entry:,}원 × {plan.quantity:,}주"})
            outcome.ok = True
            outcome.message = "매수 주문이 접수됐습니다. 체결되면 익절·손절(OCO)이 자동 등록됩니다."
            self._log(outcome)

        threading.Thread(target=self._attach_oco_when_filled,
                         args=(account, outcome.order_id, plan,
                               fill_timeout or self.fill_timeout, cancel_on_timeout),
                         name=f"oco-{plan.symbol}", daemon=True).start()
        return outcome

    # -------------------------------------------------- 시장가 주문 (볼린저 봇)
    def market_order(self, *, symbol: str, side: str, quantity: int,
                     reference_price: float = 0.0, name: str = "",
                     reason: str = "", dry_run: bool = True) -> dict:
        """시장가 단건 주문. 볼린저 반전 봇의 진입·청산에 쓴다.

        OCO 를 붙이지 않는 대신 봇이 매 주기마다 손절·익절 조건을 직접 감시한다.
        안전장치는 지정가 경로와 동일하다 — `enabled` 가 아니면 실주문은 나가지 않고,
        매수는 `max_order_amount` 한도를 넘을 수 없다.

        **매도에는 금액 한도를 적용하지 않는다.** 손절 청산이 한도에 걸려 막히면
        위험이 오히려 커지기 때문이다.
        """
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise TradingError(f"주문 방향이 올바르지 않습니다: {side}")
        if not re.fullmatch(r"\d{6}", symbol or ""):
            raise TradingError("국내 주식(6자리 종목코드)만 지원합니다.")
        quantity = int(quantity)
        if quantity <= 0:
            raise TradingError("주문 수량이 0입니다.")

        amount = int(quantity * max(reference_price, 0))
        if side == "BUY" and amount > self.max_order_amount:
            raise TradingError(
                f"주문 금액 {amount:,}원이 한도 {int(self.max_order_amount):,}원을 넘습니다.")

        record = {"symbol": symbol, "name": name, "side": side, "quantity": quantity,
                  "reference_price": reference_price, "amount": amount,
                  "reason": reason, "dry_run": dry_run}

        if dry_run:
            out = {**record, "ok": True, "order_id": None,
                   "message": "모의 실행 — 실제 주문은 나가지 않았습니다."}
            self._log_event({"event": "bollinger_order", **out})
            return out

        if not self.enabled:
            raise TradingError("실거래가 꺼져 있습니다 (TOSS_TRADING_ENABLED / 실주문 허용).")

        with self._lock:                       # 동시 중복 주문 방지
            account = self.account_seq()
            body = {
                "clientOrderId": _market_order_id(symbol, side, quantity),
                "symbol": symbol,
                "side": side,
                "orderType": "MARKET",         # 시장가에는 price 를 넣으면 400 이다
                "timeInForce": "DAY",
                "quantity": str(quantity),
            }
            try:
                res = self.client.create_order(account, body)
            except TossAPIError as exc:
                out = {**record, "ok": False, "order_id": None, "message": f"주문 실패: {exc}"}
                self._log_event({"event": "bollinger_order", **out})
                raise TradingError(f"{side} 주문 실패: {exc}") from exc

        out = {**record, "ok": True, "order_id": res.get("orderId"),
               "status": res.get("status"),
               "message": f"{'매수' if side == 'BUY' else '매도'} 시장가 주문 접수"}
        self._log_event({"event": "bollinger_order", **out})
        return out

    # -------------------------------------------------- 지정가 매수 (볼린저 봇)
    def limit_buy(self, *, symbol: str, quantity: int, limit_price: int,
                  reference_price: float = 0.0, name: str = "", reason: str = "",
                  cancel_after: float = 180.0, dry_run: bool = True) -> dict:
        """지정가 매수 + 미체결 회수. 볼린저 봇의 진입에 쓴다.

        시장가는 얇은 종목에서 슬리피지로 현재가와 크게 벌어질 수 있어, 매수는
        **현재가 근처 상한 지정가**로 낸다(이 값 이하로만 체결). 반전 전략상 하한선
        근처에서 사므로 대개 곧 체결되지만, `cancel_after` 안에 다 안 채워지면
        미체결 잔량을 **취소(회수)** 한다 — 확률이 낮은 주문을 오래 두지 않는다.

        매도(손절·익절)는 확실한 청산을 위해 여전히 `market_order` (시장가)를 쓴다.
        """
        if not re.fullmatch(r"\d{6}", symbol or ""):
            raise TradingError("국내 주식(6자리 종목코드)만 지원합니다.")
        quantity = int(quantity)
        if quantity <= 0:
            raise TradingError("주문 수량이 0입니다.")
        limit_price = int(round_tick(limit_price, "up"))
        amount = quantity * limit_price
        if amount > self.max_order_amount:
            raise TradingError(
                f"주문 금액 {amount:,}원이 한도 {int(self.max_order_amount):,}원을 넘습니다.")

        record = {"symbol": symbol, "name": name, "side": "BUY", "quantity": quantity,
                  "limit_price": limit_price, "reference_price": reference_price,
                  "amount": amount, "reason": reason, "dry_run": dry_run}

        if dry_run:
            out = {**record, "ok": True, "order_id": None,
                   "message": f"모의 실행 — 지정가 {limit_price:,}원 매수 (실제 주문 없음)"}
            self._log_event({"event": "bollinger_order", **out})
            return out

        if not self.enabled:
            raise TradingError("실거래가 꺼져 있습니다 (TOSS_TRADING_ENABLED / 실주문 허용).")

        with self._lock:                       # 동시 중복 주문 방지
            account = self.account_seq()
            body = {
                "clientOrderId": _market_order_id(symbol, "BUY", quantity, tag="L"),
                "symbol": symbol,
                "side": "BUY",
                "orderType": "LIMIT",
                "timeInForce": "DAY",
                "quantity": str(quantity),
                "price": str(limit_price),
            }
            try:
                res = self.client.create_order(account, body)
            except TossAPIError as exc:
                out = {**record, "ok": False, "order_id": None, "message": f"주문 실패: {exc}"}
                self._log_event({"event": "bollinger_order", **out})
                raise TradingError(f"매수 주문 실패: {exc}") from exc

        order_id = res.get("orderId")
        out = {**record, "ok": True, "order_id": order_id, "status": res.get("status"),
               "message": f"매수 지정가 {limit_price:,}원 주문 접수 "
                          f"({int(cancel_after)}초 안에 미체결이면 회수)"}
        self._log_event({"event": "bollinger_order", **out})
        # 미체결 회수는 백그라운드에서 감시한다 (주문 응답을 막지 않는다).
        threading.Thread(target=self._cancel_unfilled, args=(account, order_id, cancel_after),
                         name=f"bl-cancel-{symbol}", daemon=True).start()
        return out

    def _cancel_unfilled(self, account: int, order_id: str, cancel_after: float) -> None:
        """`cancel_after` 초 뒤에도 다 체결되지 않은 지정가 매수를 취소한다."""
        if not order_id:
            return
        time.sleep(max(1.0, cancel_after))
        try:
            order = self.client.get_order(account, order_id)
        except Exception as exc:
            log.warning("주문 %s 조회 실패 — 회수 판단 보류: %s", order_id, exc)
            return
        status = str(order.get("status", ""))
        if status in ("FILLED", "COMPLETED", "EXECUTED", "CANCELED", "REJECTED", "EXPIRED"):
            return                              # 이미 끝난 주문은 손대지 않는다
        try:
            self.client.cancel_order(account, order_id)
            filled = _filled_qty(order)
            log.info("주문 %s 미체결 회수 (체결 %d주)", order_id, filled)
            self._log_event({"event": "bollinger_cancel", "orderId": order_id,
                             "filled": filled, "reason": "미체결 회수(시한 초과)"})
        except Exception as exc:
            log.error("주문 %s 회수 실패 — 수동 확인 필요: %s", order_id, exc)
            self._log_event({"event": "bollinger_cancel_failed",
                             "orderId": order_id, "error": str(exc)})

    # ------------------------------------------------------------ 체결 후 OCO
    def _attach_oco_when_filled(self, account: int, order_id: str, plan: OrderPlan,
                                timeout: float | None = None,
                                cancel_on_timeout: bool = False) -> None:
        deadline = time.monotonic() + (timeout or self.fill_timeout)
        filled = 0
        done = False
        while time.monotonic() < deadline:
            try:
                order = self.client.get_order(account, order_id)
            except Exception as exc:
                log.warning("주문 %s 조회 실패: %s", order_id, exc)
                time.sleep(self.poll_interval)
                continue
            filled = _filled_qty(order)
            status = str(order.get("status", ""))
            if filled > 0 and status in ("FILLED", "COMPLETED", "EXECUTED"):
                done = True
                break
            if status in ("CANCELED", "REJECTED", "EXPIRED"):
                log.info("주문 %s 종료(%s) — OCO 등록 안 함", order_id, status)
                self._log_event({"event": "oco_skipped", "orderId": order_id, "status": status})
                return
            time.sleep(self.poll_interval)

        if not done and cancel_on_timeout:
            try:
                self.client.cancel_order(account, order_id)
                self._log_event({"event": "order_canceled", "orderId": order_id,
                                 "reason": "체결 시한 초과", "filled": filled})
            except Exception as exc:
                log.error("주문 %s 잔량 취소 실패 — 수동 확인 필요: %s", order_id, exc)
                self._log_event({"event": "cancel_failed", "orderId": order_id,
                                 "error": str(exc)})
            try:                                # 취소 직전 체결분까지 다시 센다
                order = self.client.get_order(account, order_id)
                filled = _filled_qty(order)
            except Exception as exc:
                log.warning("주문 %s 재조회 실패: %s", order_id, exc)

        if filled <= 0:
            log.info("주문 %s 미체결 — OCO 등록 안 함", order_id)
            self._log_event({"event": "oco_skipped", "orderId": order_id, "reason": "미체결"})
            return

        try:
            res = self.client.create_conditional_order(account, {
                "clientOrderId": _client_order_id(plan, suffix="oco"),
                "symbol": plan.symbol,
                "type": "OCO",
                "quantity": str(filled),
                "orderType": "LIMIT",
                "expireDate": plan.expire_date,
                # OCO 규칙: first 감시가 > 현재가 > second 감시가, 두 leg 모두 SELL.
                # `orderSide` 는 leg 필수 필드다 (빠지면 400 invalid-request).
                "first": {"orderSide": "SELL", "triggerPrice": str(plan.target),
                          "orderPrice": str(plan.target)},
                "second": {"orderSide": "SELL", "triggerPrice": str(plan.stop),
                           "orderPrice": str(plan.stop)},
            })
            log.info("%s OCO 등록 완료: %s", plan.symbol, res.get("conditionalOrderId"))
            self._log_event({"event": "oco_created", "orderId": order_id,
                             "conditionalOrderId": res.get("conditionalOrderId"),
                             "filled": filled, "plan": plan.as_dict()})
        except Exception as exc:
            log.error("%s OCO 등록 실패 — 수동으로 손절/익절을 걸어야 합니다: %s", plan.symbol, exc)
            self._log_event({"event": "oco_failed", "orderId": order_id, "error": str(exc),
                             "plan": plan.as_dict()})

    # ------------------------------------------------------------ 기록
    def _log(self, outcome: OrderOutcome) -> None:
        self._log_event({"event": "submit", **outcome.as_dict()})

    def _log_event(self, payload: dict) -> None:
        if not self.log_path:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                     **payload}, ensure_ascii=False) + "\n")
        except Exception as exc:                       # 기록 실패가 주문을 막지 않도록
            log.warning("주문 로그 기록 실패: %s", exc)


def _client_order_id(plan: OrderPlan, suffix: str = "") -> str:
    """같은 종목·같은 가격·같은 날의 주문은 하나로 취급되는 멱등키."""
    base = f"sf-{plan.symbol}-{date.today():%m%d}-{plan.entry}-{plan.quantity}"
    return (base + ("-" + suffix if suffix else ""))[:36]


def _market_order_id(symbol: str, side: str, quantity: int, tag: str = "") -> str:
    """봇 주문 멱등키. 분 단위 버킷이라 같은 분의 중복 발사만 하나로 접힌다
    (멱등키는 10분간 유효하므로 시각을 넣지 않으면 정상 재진입까지 막힌다).
    `tag` 로 시장가(빈값)와 지정가("L") 매수 키가 겹치지 않게 한다."""
    return f"bl{tag}-{symbol}-{side[0]}-{datetime.now():%m%d%H%M}-{quantity}"[:36]


def _mask(account_no: str) -> str:
    if not account_no:
        return ""
    return account_no[:4] + "*" * max(0, len(account_no) - 6) + account_no[-2:]


def _filled_qty(order: dict) -> int:
    q = _to_float((order.get("execution") or {}).get("filledQuantity"))
    return int(q) if q == q else 0


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")
