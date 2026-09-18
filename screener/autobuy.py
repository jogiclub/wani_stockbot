"""세력추정매매 자동 매수 — 점수 컷오프 이상 시그널을 토스로 자동 주문한다.

⚠ **실제 돈이 나가는 경로다.** 수동 주문(`/api/trading/order`)과 같은
`TradingService.submit` 을 쓰므로 안전장치도 같다.

- 실주문은 `trading.enabled`(서버 `TOSS_TRADING_ENABLED` + 사용자 `trading_opt_in`)일 때만.
  꺼져 있으면 **모의 주문**으로 기록·알림만 한다.
- 1회 금액 = min(사용자 자동매수 금액, 1회 주문 한도, 예수금). 그 안에서 최대 수량.
- 주문은 수동 주문과 같다 — 지정가 매수(매수가=전일 고가) → 체결 시 OCO(1R 익절 / 기준봉 저가 손절).
  돌파 뒤 추격 매수하지 않는다(백테스트 진입가와 같게). 대신 **장 마감까지 미체결이면 취소**한다
  — 손절 없이 늦게 체결된 물량이 남지 않게.
- 같은 종목은 **하루 1회**. 재시작해도 주문 로그(`auto_buy` 이벤트)와 보유 잔고로 다시 사지 않는다.
- 정규장 접속매매 시간(09:00~15:20)에만, 오늘 날짜 시그널만 주문한다.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, time as dtime
from typing import Callable

from .pattern_detector import Signal
from .trading import TradingError, TradingService, round_tick

log = logging.getLogger(__name__)

AUTO_OPEN = dtime(9, 0)
AUTO_CLOSE = dtime(15, 20)          # 종가 동시호가 전까지만
ORDER_DEADLINE = dtime(15, 30)      # 이때까지 미체결이면 취소


def auto_window(now: datetime) -> bool:
    return now.weekday() < 5 and AUTO_OPEN <= now.time() < AUTO_CLOSE


class AutoBuyer:
    """스캔 결과를 받아 점수 컷오프 이상 시그널을 주문한다. 스레드 안전."""

    def __init__(self, trading: TradingService, *, min_score: int, amount: float,
                 notify: Callable[[str], None] | None = None,
                 clock: Callable[[], datetime] = datetime.now):
        self.trading = trading
        self.min_score = int(min_score)
        self.amount = float(amount)
        self.notify = notify
        self.clock = clock
        self._done: set[tuple[str, str]] = set()     # (YYYY-MM-DD, 종목)
        self._lock = threading.Lock()
        self._load_today(clock().date())

    # ------------------------------------------------------------ 진입점
    def handle(self, signals: list[Signal]) -> list[dict]:
        now = self.clock()
        if not auto_window(now):
            return []
        today = now.date()
        picks = [s for s in signals
                 if s.score is not None and s.score.total >= self.min_score
                 and s.signal_date.date() == today
                 and (today.isoformat(), s.symbol) not in self._done]
        if not picks:
            return []
        picks.sort(key=lambda s: s.score.total, reverse=True)
        results = []
        with self._lock:
            held = self._held_symbols()
            for sig in picks:
                key = (today.isoformat(), sig.symbol)
                if key in self._done:
                    continue
                if sig.symbol in held:
                    self._done.add(key)
                    continue
                results.append(self._buy(sig, now))
                self._done.add(key)     # 실패해도 같은 날 재시도하지 않는다 (반복 주문 방지)
        return results

    # ------------------------------------------------------------ 내부
    def _buy(self, sig: Signal, now: datetime) -> dict:
        live = self.trading.enabled
        target = sig.targets[0] if sig.targets else 0.0
        entry = round_tick(sig.entry, "down")
        record = {"event": "auto_buy", "date": now.date().isoformat(), "symbol": sig.symbol,
                  "name": sig.name, "score": sig.score.total, "entry": entry,
                  "dry_run": not live}
        try:
            if not target or sig.last_price >= target:
                raise TradingError(f"현재가 {sig.last_price:,.0f}원이 이미 1R 목표가"
                                   f" {target:,.0f}원 이상이라 추격하지 않습니다.")
            budget = min(self.amount, self.trading.max_order_amount)
            cash = self._cash() if live else None
            if cash is not None:
                budget = min(budget, cash)
            qty = int(budget // entry)
            if qty <= 0:
                raise TradingError(f"금액 {budget:,.0f}원으로 {entry:,}원짜리를 1주도 살 수 없습니다.")
            deadline = datetime.combine(now.date(), ORDER_DEADLINE)
            outcome = self.trading.submit(
                dry_run=not live, symbol=sig.symbol, name=sig.name, quantity=qty,
                entry=sig.entry, stop=sig.stop, target=target,
                fill_timeout=max(60.0, (deadline - now).total_seconds()),
                cancel_on_timeout=True)
            record.update(ok=outcome.ok, quantity=qty, amount=qty * entry,
                          order_id=outcome.order_id, message=outcome.message)
        except TradingError as exc:
            record.update(ok=False, message=str(exc))
        except Exception as exc:                       # 주문 실패가 스캔 루프를 죽이지 않게
            log.exception("자동매수 실패 %s", sig.symbol)
            record.update(ok=False, message=f"{type(exc).__name__}: {exc}")

        self.trading._log_event(record)
        log.info("자동매수 %s %s: %s", sig.symbol, "성공" if record["ok"] else "실패",
                 record["message"])
        self._notify(record)
        return record

    def _cash(self) -> float | None:
        try:
            power = self.trading.client.buying_power(self.trading.account_seq())
            return float(power.get("cashBuyingPower"))
        except Exception as exc:
            log.warning("예수금 조회 실패 — 설정 금액 기준으로 주문: %s", exc)
            return None

    def _held_symbols(self) -> set[str]:
        if not self.trading.enabled:
            return set()
        try:
            held = self.trading.client.holdings(self.trading.account_seq())
        except Exception as exc:
            log.warning("잔고 조회 실패: %s", exc)
            return set()
        out = set()
        for item in held.get("items") or []:
            try:
                if float(item.get("quantity") or 0) > 0:
                    out.add(item.get("symbol"))
            except (TypeError, ValueError):
                continue
        return out

    def _load_today(self, day: date) -> None:
        """재시작 후 같은 날 같은 종목을 다시 사지 않도록 오늘 기록을 읽는다."""
        path = self.trading.log_path
        if not path or not path.exists():
            return
        today = day.isoformat()
        try:
            with path.open(encoding="utf-8") as fp:
                for line in fp:
                    if '"auto_buy"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("event") == "auto_buy" and rec.get("date") == today \
                            and not rec.get("dry_run"):
                        self._done.add((today, rec.get("symbol")))
        except OSError as exc:
            log.warning("주문 로그 읽기 실패: %s", exc)

    def _notify(self, rec: dict) -> None:
        if not self.notify:
            return
        mode = "모의" if rec["dry_run"] else "실주문"
        head = "✅ 자동매수" if rec["ok"] else "⚠ 자동매수 실패"
        body = (f"{head} ({mode})\n{rec['name']}({rec['symbol']}) {rec['score']}점\n")
        if rec.get("quantity"):
            body += f"지정가 {rec['entry']:,}원 × {rec['quantity']:,}주 = {rec['amount']:,}원\n"
        body += rec.get("message", "")
        try:
            self.notify(body)
        except Exception as exc:
            log.warning("자동매수 알림 실패: %s", exc)
