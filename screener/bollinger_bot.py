"""볼린저 밴드 반전 매매 자동매매 봇.

설정한 주기마다 감시 종목의 캔들을 받아 지표를 계산하고(`screener.bollinger`),
판정 결과대로 **토스증권 OpenAPI 로 실제 시장가 매수·매도**를 낸다.

⚠ 실제 돈이 나간다. 안전장치는 세력추정매매 주문 경로와 동일하며 더 좁다.
  1. 서버 `TOSS_TRADING_ENABLED` 와 사용자 동의가 **둘 다** 참일 때만 실주문.
  2. 그 위에 봇 자체 스위치(`live`)가 하나 더 있다. 기본은 모의(dry-run)다.
  3. 1회 주문 금액은 `TradingService.max_order_amount` 를 넘을 수 없다.
  4. 동시 보유 종목 수(`max_positions`)와 예수금 비율(`cash_ratio`)로 노출을 제한한다.
  5. 모든 판정·주문·체결을 `out/bollinger.log`(텍스트)와 주문 JSONL 에 남긴다.

보유 수량의 진실은 **항상 토스 계좌 잔고**다. 봇의 로컬 상태(매수가·부분익절 여부)는
잔고에 덧씌우는 메모일 뿐이라, 봇을 재시작해도 계좌를 다시 읽어 복구한다.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path

import pandas as pd

from .bollinger import (BollingerConfig, Decision, Position, enrich, evaluate,
                        position_size, prepare)
from .toss_client import TossClient
from .trading import TradingError, TradingService, round_tick

log = logging.getLogger(__name__)

# 국내 정규장 (KST). 장 밖에서는 판정만 하고 주문은 내지 않는다.
MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 30)


def market_is_open(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:                      # 토·일
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


@dataclass
class BotState:
    """화면·API 로 내려줄 봇 상태 스냅샷."""

    running: bool = False
    live: bool = False                          # True = 실주문, False = 모의
    started_at: str | None = None
    last_run: str | None = None
    next_run_in: int = 0
    error: str | None = None
    market_open: bool = False
    cash: float = 0.0
    decisions: list = field(default_factory=list)   # 종목별 최신 판정
    positions: list = field(default_factory=list)   # 보유 종목
    events: list = field(default_factory=list)      # 최근 체결/알림 기록
    equity: dict = field(default_factory=dict)      # 평가 요약
    adopted: list = field(default_factory=list)    # 봇이 사지 않았는데 관리하게 된 보유분

    def as_dict(self) -> dict:
        return {**self.__dict__}


class BollingerBot:
    """감시 → 판정 → 주문 을 주기적으로 도는 자동매매 루프."""

    MAX_EVENTS = 200

    def __init__(self, client: TossClient, trading: TradingService,
                 cfg: BollingerConfig | None = None, *,
                 poll_interval: int = 60, live: bool = False,
                 notify=None, log_path: Path | str | None = None,
                 cache_dir: Path | str | None = None):
        self.client = client
        self.trading = trading
        self.cfg = cfg or BollingerConfig()
        self.poll_interval = max(20, int(poll_interval))
        self._live = bool(live)
        self.notify = notify                    # callable(str) — 텔레그램 등
        self.state = BotState(live=self.live)
        self.positions: dict[str, Position] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._names: dict[str, str] = {}
        self.file_log = _file_logger(log_path)

    # ------------------------------------------------------------- 스위치
    @property
    def live(self) -> bool:
        """실주문 여부. **주문 서비스가 허용하지 않으면 무조건 False.**"""
        return self._live and self.trading.enabled

    def set_live(self, value: bool) -> bool:
        self._live = bool(value)
        self.state.live = self.live
        self._say(f"실주문 스위치 {'ON ⚠' if self.live else 'OFF (모의)'}")
        return self.live

    def update_config(self, cfg: BollingerConfig, poll_interval: int | None = None) -> None:
        with self._lock:
            self.cfg = cfg
            if poll_interval:
                self.poll_interval = max(20, int(poll_interval))
        self._say(f"설정 변경: {cfg.interval}봉 · 감시 {len(cfg.symbols)}종목 · "
                  f"예수금 {cfg.cash_ratio * 100:.0f}% · 손절 {cfg.stop_loss_pct * 100:.1f}%")

    # ------------------------------------------------------------- 루프
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.state.running = True
        self.state.started_at = _now()
        self._thread = threading.Thread(target=self._loop, name="bollinger-bot", daemon=True)
        self._thread.start()
        self._say(f"봇 시작 ({'실주문 ⚠' if self.live else '모의'} · {self.cfg.interval}봉 · "
                  f"{self.poll_interval}초 주기)")

    def stop(self) -> None:
        self._stop.set()
        self.state.running = False
        self._say("봇 정지")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                self.state.error = f"{type(exc).__name__}: {exc}"
                log.exception("볼린저 봇 주기 실행 실패")
                self.file_log.error("주기 실행 실패: %s", exc)
            self._stop.wait(self.poll_interval)

    # ------------------------------------------------------------- 1주기
    def run_once(self) -> BotState:
        """한 주기: 잔고 동기화 → 종목별 판정 → 주문."""
        if not self._lock.acquire(blocking=False):
            return self.state                    # 이미 도는 중이면 건너뛴다
        try:
            self.state.error = None
            self.state.market_open = market_is_open()
            self._sync_account()

            decisions: list[dict] = []
            for symbol in self.cfg.symbols:
                try:
                    decision = self._evaluate(symbol)
                except Exception as exc:
                    log.warning("%s 판정 실패: %s", symbol, exc)
                    decisions.append({"종목코드": symbol, "액션": "ERROR", "사유": str(exc)})
                    continue
                if decision is None:
                    decisions.append({"종목코드": symbol, "액션": "NODATA",
                                      "사유": f"{self.cfg.interval}봉 데이터 부족"})
                    continue
                row = {**decision.as_dict(), "종목명": self._name(symbol)}
                if decision.action != "HOLD":
                    row["결과"] = self._act(decision)
                decisions.append(row)

            self.state.decisions = decisions
            self.state.positions = [p.as_dict() for p in self.positions.values() if p.quantity > 0]
            self.state.last_run = _now()
        finally:
            self._lock.release()
        return self.state

    # ------------------------------------------------------------- 판정
    def _evaluate(self, symbol: str) -> Decision | None:
        df = self._candles(symbol)
        pos = self.positions.get(symbol)
        live_price = None
        if self.state.market_open:
            live_price = self._last_price(symbol)
        return evaluate(symbol, df, self.cfg, pos, live_price)

    def _candles(self, symbol: str) -> pd.DataFrame:
        """판정에 필요한 만큼 캔들을 받는다 (실시간이라 캐시하지 않는다).

        1분봉 응답에는 **시간외 단일가 봉도 섞여** 오고 그건 지표에서 걸러낸다
        (`bollinger.regular_session`). 그래서 필요한 정규장 봉 수보다 넉넉히 받아야 한다 —
        딱 맞춰 받으면 장 초반에 "데이터 부족" 이 난다.
        """
        from .data_loader import candles_to_df

        need = self.cfg.min_bars() + 10
        if self.cfg.candle_interval() == "1d":
            return candles_to_df(self.client.candles_paged(symbol, "1d", need))
        return candles_to_df(self.client.candles_paged(symbol, "1m", _minutes_to_fetch(
            need * _minutes_per_bar(self.cfg.interval))))

    def _last_price(self, symbol: str) -> float | None:
        try:
            rows = self.client.prices([symbol])
            return float(rows[0]["lastPrice"]) if rows else None
        except Exception as exc:
            log.debug("%s 현재가 조회 실패: %s", symbol, exc)
            return None

    def indicator_frame(self, symbol: str) -> pd.DataFrame:
        """차트용 — 판정과 **같은 전처리**(정규장 필터 + 리샘플)를 거친 뒤 지표를 붙인다."""
        return enrich(prepare(self._candles(symbol), self.cfg), self.cfg)

    # ------------------------------------------------------------- 계좌
    def _sync_account(self) -> None:
        """보유 수량·매수단가·예수금을 계좌에서 다시 읽는다 (로컬 상태보다 우선).

        **감시 목록에 있는 국내 종목만** 봇의 관리 대상으로 삼는다. 계좌의 다른 보유분은
        건드리지 않으며 동시 보유 한도에도 세지 않는다 — 봇이 사지 않은 물량을
        마음대로 청산하지 않기 위해서다.

        다만 감시 목록에 **이미 들고 있던 종목**을 넣으면 그 물량은 봇의 관리 대상이 되어
        손절·청산 규칙이 그대로 적용된다. 이 경우 화면에 경고를 띄운다(`adopted`).
        """
        try:
            account = self.trading.account_seq()
        except TradingError as exc:
            self.state.error = str(exc)
            return
        try:
            power = self.client.buying_power(account)
            self.state.cash = _to_float(power.get("cashBuyingPower"))
        except Exception as exc:
            log.debug("예수금 조회 실패: %s", exc)

        try:
            held = self.client.holdings(account)
        except Exception as exc:
            log.warning("잔고 조회 실패: %s", exc)
            return

        self.state.equity = {
            "투자원금": _krw(held.get("totalPurchaseAmount")),
            "평가금액": _krw((held.get("marketValue") or {}).get("amount")),
            "평가손익": _krw((held.get("profitLoss") or {}).get("amount")),
        }

        watched = set(self.cfg.symbols)
        adopted: list[str] = []
        live_symbols = set()
        for item in held.get("items") or []:
            sym = item.get("symbol")
            qty = _to_float(item.get("quantity"))
            if sym not in watched or not (qty > 0):
                continue                          # 감시 목록 밖 보유분은 관리하지 않는다
            live_symbols.add(sym)
            self._names.setdefault(sym, item.get("name", sym))
            pos = self.positions.get(sym)
            if pos is None:
                # 봇이 모르던 보유분(재시작·수동 매수). 계좌 평단으로 복구한다.
                pos = Position(symbol=sym, entered_at=_now(), name=item.get("name", sym),
                               adopted=True, note="계좌에 이미 있던 보유분")
                self.positions[sym] = pos
                adopted.append(sym)
            pos.quantity = qty
            pos.entry_price = _to_float(item.get("averagePurchasePrice")) or pos.entry_price
            pos.name = item.get("name", pos.name)

        for sym, pos in list(self.positions.items()):
            if sym not in live_symbols:           # 전량 청산됐거나 감시 목록에서 빠졌다
                pos.quantity = 0.0
                pos.mid_taken = False

        self.state.adopted = adopted
        for sym in adopted:
            tail = ("평단 {:,.0f}원 기준으로 손절·청산 규칙이 적용됩니다."
                    if self.cfg.manage_existing else
                    "'기존 보유분도 관리' 가 꺼져 있어 **청산하지 않습니다** (평단 {:,.0f}원).")
            self._say(f"⚠ {self._name(sym)}({sym}) 은 봇이 산 물량이 아닙니다 — "
                      + tail.format(self.positions[sym].entry_price))

    def _name(self, symbol: str) -> str:
        if symbol not in self._names:
            try:
                found = self.client.stocks([symbol])
                self._names[symbol] = found[0].get("name", symbol) if found else symbol
            except Exception:
                self._names[symbol] = symbol
        return self._names[symbol]

    # ------------------------------------------------------------- 주문
    def _act(self, d: Decision) -> dict:
        """판정에 따라 주문을 낸다. 장 밖이면 기록만 남기고 보낸다고 하지 않는다."""
        if not self.state.market_open:
            return self._skip(d, "정규장 시간이 아니라 주문하지 않았습니다")

        if d.action == "BUY":
            return self._buy(d)

        # 봇이 사지 않은 물량은 기본적으로 건드리지 않는다. 사용자가 감시 목록에
        # 장기 보유 종목을 넣었다가 첫 주기에 전량 털리는 사고를 막는 장치다.
        pos = self.positions.get(d.symbol)
        if pos is not None and pos.adopted and not self.cfg.manage_existing:
            return self._skip(d, "봇이 매수한 물량이 아닙니다 "
                                 "('기존 보유분도 관리' 를 켜야 청산합니다)")
        return self._sell(d)

    def _buy(self, d: Decision) -> dict:
        held = sum(1 for p in self.positions.values() if p.quantity > 0)
        if held >= self.cfg.max_positions:
            return self._skip(d, f"동시 보유 상한 {self.cfg.max_positions}종목 도달")

        # 매수는 지정가로 낸다 — 현재가 대비 buy_slippage 만큼 위를 상한으로.
        # 수량도 이 지정가 기준으로 산정해 1회 한도를 넘지 않게 한다.
        limit = round_tick(d.snapshot.close * (1 + self.cfg.buy_slippage), "up")
        qty = position_size(self.state.cash, limit, self.cfg,
                            self.trading.max_order_amount)
        if qty <= 0:
            return self._skip(
                d, f"예수금 {self.state.cash:,.0f}원 × {self.cfg.cash_ratio * 100:.0f}% 로는 "
                   f"{self.cfg.min_order_amount:,.0f}원 최소 주문에 미달")

        result = self._order(d, "BUY", qty, limit_price=limit)
        if result.get("ok"):
            pos = self.positions.setdefault(d.symbol, Position(symbol=d.symbol))
            pos.name = self._name(d.symbol)
            pos.entered_at = _now()
            pos.mid_taken = False
            if not pos.entry_price:              # 체결 전 잠정값 (다음 동기화 때 평단으로 교정)
                pos.entry_price = d.snapshot.close
            pos.history.append({"ts": _now(), "액션": "BUY", "수량": qty,
                                "가격": d.snapshot.close, "사유": d.reason})
        return result

    def _sell(self, d: Decision) -> dict:
        pos = self.positions.get(d.symbol)
        if pos is None or pos.quantity <= 0:
            return self._skip(d, "보유 수량이 없습니다")

        qty = int(pos.quantity * d.quantity_ratio)
        if d.action != "TAKE_PROFIT":
            qty = int(pos.quantity)              # 청산·손절은 항상 전량
        qty = min(qty, self._sellable(d.symbol, int(pos.quantity)))
        if qty <= 0:
            return self._skip(d, "매도 가능 수량이 0입니다 (결제 미도래 등)")

        result = self._order(d, "SELL", qty)
        if result.get("ok"):
            if d.action == "TAKE_PROFIT":
                pos.mid_taken = True             # 부분 익절은 한 번만
            pos.history.append({"ts": _now(), "액션": d.action, "수량": qty,
                                "가격": d.snapshot.close, "사유": d.reason})
        return result

    def _sellable(self, symbol: str, fallback: int) -> int:
        try:
            res = self.client.sellable_quantity(self.trading.account_seq(), symbol)
            return int(_to_float(res.get("sellableQuantity")) or 0)
        except Exception as exc:
            log.debug("%s 매도가능수량 조회 실패, 보유수량으로 진행: %s", symbol, exc)
            return fallback

    def _order(self, d: Decision, side: str, quantity: int,
               limit_price: int | None = None) -> dict:
        """매수는 지정가(+미체결 회수), 매도는 시장가로 주문한다."""
        try:
            if side == "BUY":
                out = self.trading.limit_buy(
                    symbol=d.symbol, quantity=quantity, limit_price=int(limit_price),
                    reference_price=d.snapshot.close, name=self._name(d.symbol),
                    reason=f"{d.action}: {d.reason}",
                    cancel_after=self.cfg.buy_cancel_after, dry_run=not self.live)
                price, kind = int(limit_price), f"지정가 {int(limit_price):,}원"
            else:
                out = self.trading.market_order(
                    symbol=d.symbol, side=side, quantity=quantity,
                    reference_price=d.snapshot.close, name=self._name(d.symbol),
                    reason=f"{d.action}: {d.reason}", dry_run=not self.live)
                price, kind = d.snapshot.close, f"시장가 (현재 {d.snapshot.close:,.0f}원)"
        except TradingError as exc:
            self._event(d, "실패", str(exc))
            return {"ok": False, "message": str(exc)}

        amount = quantity * price
        text = (f"{'[실주문]' if self.live else '[모의]'} {self._name(d.symbol)}({d.symbol}) "
                f"{_LABEL[d.action]} {quantity:,}주 {kind} "
                f"≈ {amount:,.0f}원 — {d.reason}")
        self._event(d, "접수", text, order_id=out.get("order_id"))
        return out

    def _skip(self, d: Decision, why: str) -> dict:
        self._event(d, "보류", f"{self._name(d.symbol)}({d.symbol}) {_LABEL[d.action]} 보류 — {why}")
        return {"ok": False, "skipped": True, "message": why}

    # ------------------------------------------------------------- 기록·알림
    def _event(self, d: Decision, status: str, text: str, order_id: str | None = None) -> None:
        event = {"ts": _now(), "종목코드": d.symbol, "종목명": self._name(d.symbol),
                 "액션": d.action, "상태": status, "내용": text, "주문번호": order_id,
                 "RSI": round(d.snapshot.rsi, 1), "가격": d.snapshot.close}
        self.state.events.insert(0, event)
        del self.state.events[self.MAX_EVENTS:]
        self.file_log.info("%s | %s", status, text)
        if status != "보류":                      # 보류는 잡음이라 알림하지 않는다
            self._notify(text)

    def _say(self, text: str) -> None:
        log.info("[볼린저] %s", text)
        self.file_log.info("%s", text)
        self.state.events.insert(0, {"ts": _now(), "액션": "SYSTEM", "상태": "정보",
                                     "내용": text})
        del self.state.events[self.MAX_EVENTS:]

    def _notify(self, text: str) -> None:
        if not self.notify:
            return
        try:
            self.notify("📊 <b>볼린저 반전 매매</b>\n" + text)
        except Exception as exc:
            log.warning("알림 전송 실패: %s", exc)


_LABEL = {"BUY": "매수", "SELL": "전량매도", "TAKE_PROFIT": "부분익절", "STOP": "손절매도",
          "HOLD": "보유", "ERROR": "오류", "NODATA": "데이터부족"}


SESSION_MINUTES = 390        # 정규장 09:00~15:30 = 390분
_BARS_PER_DAY = 600          # 하루치 1분봉 개수 어림 (정규장 390 + 장전·시간외 여유)


def _minutes_per_bar(interval: str) -> int:
    return {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}.get(interval, 15)


def _minutes_to_fetch(session_minutes: int) -> int:
    """정규장 `session_minutes` 분을 확보하려면 1분봉을 몇 개 받아야 하는지.

    시간외 봉이 섞여 오므로 하루치를 넉넉히 잡고, 주말·휴장은 봉 자체가 없어
    개수 기준 페이지네이션이 알아서 건너뛴다. 하루 여유를 더 둔다.
    """
    days = -(-session_minutes // SESSION_MINUTES) + 1        # 올림 + 1일
    return days * _BARS_PER_DAY


def _file_logger(path: Path | str | None) -> logging.Logger:
    """봇 전용 파일 로거. 콘솔에는 상위 로거로 전파된다."""
    logger = logging.getLogger("screener.bollinger.trades")
    logger.setLevel(logging.INFO)
    if path and not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
        except OSError as exc:
            log.warning("볼린저 로그 파일을 열지 못했습니다: %s", exc)
    return logger


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _krw(node) -> float:
    """토스 금액 노드에서 원화 값을 꺼낸다.

    응답은 통화별 dict 다: `{"krw": "72283750", "usd": "6586.43"}`.
    (초기 구현은 `amount` 키를 찾아 항상 0 이 나왔다 — 회귀 테스트 있음)
    """
    if isinstance(node, dict):
        return _to_float(node.get("krw"))
    return _to_float(node)


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
