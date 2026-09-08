"""시그널 통보 채널 (콘솔 / 텔레그램 / 파일)."""

from __future__ import annotations

import abc
import json
import logging
from datetime import datetime
from pathlib import Path

import requests

from .pattern_detector import Signal

log = logging.getLogger(__name__)


class Notifier(abc.ABC):
    @abc.abstractmethod
    def send(self, signals: list[Signal]) -> None: ...


class ConsoleNotifier(Notifier):
    """터미널 표 출력."""

    HEADER = ("점수", "등급", "종목코드", "종목명", "기준봉일", "조정", "전일고가",
              "현재가", "거래량↑", "매수가", "손절가", "손절%")

    def send(self, signals: list[Signal]) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not signals:
            print(f"[{stamp}] 조건 충족 종목 없음")
            return
        rows = [[str(s.score.total) if s.score else "-", s.score.grade if s.score else "-",
                 s.symbol, s.name[:10], s.base.date.strftime("%m-%d"), f"{s.adjust_days}일",
                 f"{s.prev_high:,.0f}", f"{s.last_price:,.0f}",
                 f"+{(s.volume_ratio_prev-1)*100:.0f}%", f"{s.entry:,.0f}",
                 f"{s.stop:,.0f}", f"{s.risk_pct:.1f}%"] for s in signals]
        widths = [max(_w(h), *(_w(r[i]) for r in rows)) for i, h in enumerate(self.HEADER)]
        line = "-+-".join("-" * w for w in widths)
        print(f"\n[{stamp}] 세력 추정 매매법 시그널 {len(signals)}건")
        print(" | ".join(_pad(h, w) for h, w in zip(self.HEADER, widths)))
        print(line)
        for r in rows:
            print(" | ".join(_pad(c, w) for c, w in zip(r, widths)))
        print()
        for s in signals:
            print("  · " + s.format_line())
        print()


class TelegramNotifier(Notifier):
    """텔레그램 봇 메시지 전송."""

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0):
        self.bot_token, self.chat_id, self.timeout = bot_token, chat_id, timeout

    def send(self, signals: list[Signal]) -> None:
        if not signals or not self.bot_token or not self.chat_id:
            return
        for s in signals:
            self._post(self._format(s))

    def _format(self, s: Signal) -> str:
        head = f"[{s.score.total}점 {s.score.grade} · {s.score.advice}]\n" if s.score else ""
        return (f"🚨 <b>세력 매집 돌파 포착</b>\n{head}"
                f"<b>{s.name} ({s.symbol})</b>\n"
                f"· 기준봉: {s.base.date:%Y-%m-%d} (윗꼬리 {s.base.upper_tail_ratio*100:.0f}%, "
                f"거래량 x{s.base.volume_ratio:.1f})\n"
                f"· 매물소화: {s.adjust_days}영업일 (거래량비 {s.adjust_vol_ratio:.2f})\n"
                f"· 전일고가 {s.prev_high:,.0f} 돌파 / 현재 {s.last_price:,.0f}\n"
                f"· 거래량 전일 대비 +{(s.volume_ratio_prev-1)*100:.0f}%\n"
                f"· 매수 {s.entry:,.0f} / 손절 {s.stop:,.0f} ({s.risk_pct:.1f}%)\n"
                f"· 목표 {' / '.join(f'{t:,.0f}' for t in s.targets)}")

    def send_text(self, text: str) -> None:
        """시그널이 아닌 임의 메시지(봇 체결 알림 등) 전송."""
        if self.bot_token and self.chat_id:
            self._post(text)

    def _post(self, text: str) -> None:
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                              json={"chat_id": self.chat_id, "text": text,
                                    "parse_mode": "HTML", "disable_web_page_preview": True},
                              timeout=self.timeout)
            if r.status_code != 200:
                log.warning("텔레그램 전송 실패 %s: %s", r.status_code, r.text[:200])
        except requests.RequestException as exc:
            log.warning("텔레그램 전송 오류: %s", exc)


class JsonFileNotifier(Notifier):
    """시그널을 JSON Lines 로 적재 (백테스트·복기용)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, signals: list[Signal]) -> None:
        with self.path.open("a", encoding="utf-8") as fp:
            for s in signals:
                fp.write(json.dumps(s.to_dict(), ensure_ascii=False) + "\n")


class MultiNotifier(Notifier):
    def __init__(self, *notifiers: Notifier):
        self.notifiers = [n for n in notifiers if n]

    def send(self, signals: list[Signal]) -> None:
        for n in self.notifiers:
            try:
                n.send(signals)
            except Exception as exc:
                log.warning("%s 통보 실패: %s", type(n).__name__, exc)


class DedupeNotifier(Notifier):
    """동일 종목·시그널일 중복 알림 방지 래퍼 (장중 폴링용)."""

    def __init__(self, inner: Notifier):
        self.inner = inner
        self._seen: set[tuple[str, str]] = set()

    def send(self, signals: list[Signal]) -> None:
        fresh = [s for s in signals
                 if (s.symbol, s.signal_date.strftime("%Y-%m-%d")) not in self._seen]
        self._seen.update((s.symbol, s.signal_date.strftime("%Y-%m-%d")) for s in fresh)
        if fresh:
            self.inner.send(fresh)


def _w(text: str) -> int:
    """한글 폭 2 로 계산한 표시 너비."""
    return sum(2 if ord(c) > 0x1100 else 1 for c in str(text))


def _pad(text: str, width: int) -> str:
    return str(text) + " " * max(0, width - _w(text))
