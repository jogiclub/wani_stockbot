"""시그널 통보 채널 (콘솔 / 텔레그램 / 파일)."""

from __future__ import annotations

import abc
import html
import json
import logging
import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

import requests

from .pattern_detector import Signal

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_emails(text: str) -> list[str]:
    """콤마·세미콜론·공백·줄바꿈으로 나뉜 이메일 목록 → 검증 통과분만 (중복 제거, 소문자)."""
    seen: dict[str, None] = {}
    for tok in re.split(r"[\s,;]+", str(text or "")):
        e = tok.strip().lower()
        if e and EMAIL_RE.match(e):
            seen.setdefault(e, None)
    return list(seen)

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    """텔레그램 설정·전송 실패. 메시지는 화면에 그대로 보여줄 한국어 안내다."""


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
        # 토스 종목 페이지 링크를 함께 보낸다 (raw URL — 미리보기는 _post 가 끈다).
        # HTML 모드라 종목명은 이스케이프한다(F&F·동국S&C 등 `&` 안전).
        url = f"https://www.tossinvest.com/stocks/{s.symbol}"
        return (f"🚨 <b>세력 매집 돌파 포착</b>\n{head}"
                f"<b>{html.escape(s.name)} ({s.symbol})</b>\n"
                f"· 기준봉: {s.base.date:%Y-%m-%d} (윗꼬리 {s.base.upper_tail_ratio*100:.0f}%, "
                f"거래량 x{s.base.volume_ratio:.1f})\n"
                f"· 매물소화: {s.adjust_days}영업일 (거래량비 {s.adjust_vol_ratio:.2f})\n"
                f"· 전일고가 {s.prev_high:,.0f} 돌파 / 현재 {s.last_price:,.0f}\n"
                f"· 거래량 전일 대비 +{(s.volume_ratio_prev-1)*100:.0f}%\n"
                f"· 매수 {s.entry:,.0f} / 손절 {s.stop:,.0f} ({s.risk_pct:.1f}%)\n"
                f"· 목표 {' / '.join(f'{t:,.0f}' for t in s.targets)}\n"
                f"{url}")

    # 설정 창의 "테스트 전송" 이 쓰는 검증 경로.
    # 흔한 실수가 **토큰을 콜론에서 쪼개 Chat ID 칸에 넣는 것**이라 형식부터 잡아준다.
    TEST_TEXT = ("\u2705 <b>왔니 주식</b> 텔레그램 연결 테스트\n"
                 "이 메시지가 보이면 알림 설정이 정상입니다.")

    def check(self) -> str:
        """테스트 메시지를 1건 보낸다. 성공하면 대화방 표시명, 실패하면 TelegramError."""
        token, chat = self.bot_token.strip(), self.chat_id.strip()
        if not token or not chat:
            raise TelegramError("Bot Token 과 Chat ID 를 모두 입력하세요.")
        if not re.fullmatch(r"\d{5,}:[\w-]{20,}", token):
            raise TelegramError(
                "Bot Token 형식이 올바르지 않습니다. BotFather 가 준 "
                "123456789:AA… 전체를 콜론까지 포함해 넣으세요.")
        if not re.fullmatch(r"-?\d{1,20}|@\w{4,64}", chat):
            raise TelegramError(
                "Chat ID 는 숫자(그룹이면 -100… )이거나 @채널명 이어야 합니다. "
                "봇 토큰의 일부를 넣지 마세요.")
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": self.TEST_TEXT,
                                    "parse_mode": "HTML", "disable_web_page_preview": True},
                              timeout=self.timeout)
        except requests.RequestException as exc:
            raise TelegramError(f"텔레그램 서버에 연결하지 못했습니다: {exc}") from exc

        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 200 and body.get("ok"):
            info = (body.get("result") or {}).get("chat") or {}
            return (info.get("title") or info.get("username")
                    or info.get("first_name") or chat)

        raise TelegramError(_telegram_hint(
            r.status_code, str(body.get("description") or r.text[:200])))

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


def _telegram_hint(status: int, desc: str) -> str:
    """텔레그램 오류를 사람이 고칠 수 있는 안내 문구로 바꾼다."""
    low = desc.lower()
    if status == 401 or "unauthorized" in low:
        return "Bot Token 이 잘못됐거나 폐기(revoke)됐습니다. BotFather 에서 다시 확인하세요."
    if "chat not found" in low:
        return ("Chat ID 를 찾을 수 없습니다. 텔레그램에서 봇에게 먼저 아무 메시지나 보낸 뒤 "
                "getUpdates 로 받은 숫자 ID 를 넣으세요.")
    if "bot was blocked" in low:
        return "사용자가 봇을 차단했습니다. 텔레그램에서 차단을 해제하세요."
    if "not enough rights" in low or "kicked" in low:
        return "봇이 그 대화방에 메시지를 보낼 권한이 없습니다. 봇을 다시 초대하세요."
    return f"텔레그램 오류 ({status}): {desc}"


class EmailNotifier(Notifier):
    """세력 매집 돌파 시그널을 이메일(Gmail SMTP)로 보낸다.

    발신 계정은 서버 공용(`MAIL_SMTP_*`), 수신 주소는 사용자가 여러 개 넣을 수 있다.
    수신자끼리 서로의 주소가 보이지 않도록 **Bcc** 로 보낸다.
    wani_v2 의 `Message_send_service.send_email` 과 같은 SMTP 계정·설정을 쓴다.
    """

    def __init__(self, recipients: list[str], *, host: str, port: int, user: str,
                 password: str, crypto: str = "tls", from_name: str = "왔니 주식",
                 timeout: float = 15.0):
        self.recipients = list(recipients)
        self.host, self.port, self.user, self.password = host, int(port), user, password
        self.crypto = (crypto or "tls").lower()
        self.from_name = from_name or "왔니 주식"
        self.timeout = timeout

    def ready(self) -> bool:
        return bool(self.recipients and self.host and self.user and self.password)

    def send(self, signals: list[Signal]) -> None:
        if not signals or not self.ready():
            return
        n = len(signals)
        head = signals[0]
        subject = (f"[왔니 주식] 세력 매집 돌파 {head.name} 등 {n}종목"
                   if n > 1 else f"[왔니 주식] 세력 매집 돌파 — {head.name}")
        self._deliver(subject, self._html(signals))

    def send_text(self, text: str) -> None:
        """임의 텍스트 1건 (테스트·기타 알림)."""
        if not self.ready():
            return
        first = text.strip().splitlines()[0] if text.strip() else "알림"
        self._deliver(f"[왔니 주식] {first[:60]}",
                      f"<pre style='font:14px/1.6 sans-serif'>{html.escape(text)}</pre>")

    def check(self) -> str:
        """설정 확인용 테스트 메일 1건. 성공하면 수신자 요약, 실패하면 EmailError."""
        if not self.recipients:
            raise EmailError("받는 사람 이메일을 한 개 이상 입력하세요.")
        if not (self.host and self.user and self.password):
            raise EmailError("서버에 이메일 발신 계정이 설정되지 않았습니다 (MAIL_SMTP_* 확인).")
        self._deliver("[왔니 주식] 이메일 알림 테스트",
                      "<p>이 메일이 보이면 세력추정매매 알림이 정상으로 설정됐습니다.</p>")
        return f"{len(self.recipients)}명({', '.join(self.recipients[:3])}"
        # 표시용 요약만 — 실제 수신자 전체를 응답에 담지 않는다

    # ------------------------------------------------------------ 내부
    def _html(self, signals: list[Signal]) -> str:
        rows = []
        for s in signals:
            url = f"https://www.tossinvest.com/stocks/{s.symbol}"
            grade = f"{s.score.total}점 {s.score.grade}" if s.score else ""
            rows.append(
                f"<tr>"
                f"<td style='padding:8px 10px'><a href='{url}'>{html.escape(s.name)}</a>"
                f"<br><span style='color:#888'>{s.symbol}</span></td>"
                f"<td style='padding:8px 10px'>{grade}</td>"
                f"<td style='padding:8px 10px;text-align:right'>매수 {s.entry:,.0f}<br>"
                f"손절 {s.stop:,.0f} ({s.risk_pct:.1f}%)</td>"
                f"<td style='padding:8px 10px;text-align:right'>전일고가 {s.prev_high:,.0f}<br>"
                f"현재 {s.last_price:,.0f}</td></tr>")
        return (
            "<div style='font:14px/1.6 -apple-system,sans-serif;color:#222'>"
            "<h2 style='margin:0 0 4px'>세력 매집 돌파 포착</h2>"
            "<p style='color:#888;margin:0 0 12px'>바닥권 매집 뒤 전일 고가를 거래량과 함께 "
            "돌파한 종목입니다. 투자 판단의 참고 자료이며 수익을 보장하지 않습니다.</p>"
            "<table style='border-collapse:collapse;width:100%;max-width:640px'>"
            "<thead><tr style='background:#f3f3f0;text-align:left'>"
            "<th style='padding:8px 10px'>종목</th><th style='padding:8px 10px'>점수</th>"
            "<th style='padding:8px 10px;text-align:right'>매수·손절</th>"
            "<th style='padding:8px 10px;text-align:right'>가격</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")

    def _deliver(self, subject: str, html_body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((str(self.from_name), self.user))
        msg["To"] = self.user                     # 자기 주소 (수신자는 Bcc 로 가린다)
        msg["Bcc"] = ", ".join(self.recipients)
        msg.set_content("HTML 메일입니다. HTML 을 지원하는 클라이언트에서 확인하세요.")
        msg.add_alternative(html_body, subtype="html")
        try:
            if self.crypto == "ssl":
                with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                      context=ssl.create_default_context()) as smtp:
                    smtp.login(self.user, self.password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                    smtp.ehlo()
                    if self.crypto == "tls":
                        smtp.starttls(context=ssl.create_default_context())
                        smtp.ehlo()
                    smtp.login(self.user, self.password)
                    smtp.send_message(msg)
        except (smtplib.SMTPAuthenticationError,) as exc:
            raise EmailError("발신 계정 인증에 실패했습니다 (앱 비밀번호·계정 확인).") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise EmailError(f"메일 발송에 실패했습니다: {exc}") from exc


class EmailError(RuntimeError):
    """이메일 설정·전송 실패. 메시지는 화면에 그대로 보여줄 한국어 안내다."""


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
