"""주기 스캔 서비스.

웹 계층(`screener.web`)과 세션 계층(`screener.sessions`) 이 함께 쓰는 얇은 서비스라
별도 모듈로 둔다 (순환 import 방지).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from .notifier import Notifier
from .screener import Screener

log = logging.getLogger(__name__)


@dataclass
class ScanState:
    """마지막 스캔 결과 스냅샷."""

    signals: list[dict] = field(default_factory=list)
    watch: list[dict] = field(default_factory=list)
    scanned: int = 0
    skipped: int = 0
    errors: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    duration: float = 0.0
    running: bool = False
    error: str | None = None
    universe_size: int = 0
    blocked: list[dict] = field(default_factory=list)   # 유의사항·국면으로 제외된 종목
    regime: dict = field(default_factory=dict)          # 시장 국면 상태

    def as_dict(self) -> dict:
        return {**self.__dict__}


class ScanService:
    """주기 스캔 + 상태 보관. 웹 계층과 스크리너를 잇는 얇은 서비스."""

    def __init__(self, screener: Screener, interval: int = 300, top: int = 200,
                 limit: int | None = None, live: bool = False,
                 notifier: Notifier | None = None, with_watch: bool = True):
        self.screener = screener
        self.interval = interval
        self.top = top
        self.limit = limit
        self.live = live
        self.notifier = notifier
        self.with_watch = with_watch
        self.state = ScanState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- 스캔
    def _symbols(self) -> list[str] | None:
        loader = self.screener.loader
        if self.top and hasattr(loader, "top_by_turnover"):
            try:
                return loader.top_by_turnover(self.top)
            except Exception as exc:
                log.warning("랭킹 후보 조회 실패, 전체 유니버스로 대체: %s", exc)
        return None

    def scan_once(self) -> ScanState:
        if not self._lock.acquire(blocking=False):
            return self.state                      # 이미 스캔 중이면 그대로 반환
        try:
            self.state.running = True
            self.state.error = None
            self.state.started_at = _now()
            t0 = time.monotonic()

            symbols = self._symbols()
            report = self.screener.scan(limit=self.limit, symbols=symbols, live=self.live)
            watch = self.screener.watch_candidates(limit=self.limit, symbols=symbols) \
                if self.with_watch else []

            self.state.signals = [s.to_dict() for s in report.signals]
            self.state.blocked = report.blocked
            self.state.regime = report.regime
            self.state.watch = watch
            self.state.scanned = report.scanned
            self.state.skipped = report.skipped
            self.state.errors = report.errors
            self.state.universe_size = len(symbols) if symbols else report.scanned + report.skipped
            self.state.duration = round(time.monotonic() - t0, 1)
            self.state.finished_at = _now()

            if self.notifier and report.signals:
                self.notifier.send(report.signals)
        except Exception as exc:
            self.state.error = f"{type(exc).__name__}: {exc}"
            self.state.finished_at = _now()
            log.exception("스캔 실패")
        finally:
            self.state.running = False
            self._lock.release()
        return self.state

    # ------------------------------------------------------------- 루프
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scan-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.scan_once()
            self._stop.wait(self.interval)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
