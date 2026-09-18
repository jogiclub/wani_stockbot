"""텔레그램 시그널 포맷 회귀 테스트 (외부 전송 없음)."""

from __future__ import annotations

import pandas as pd

from screener.notifier import TelegramNotifier
from screener.pattern_detector import BaseCandle, Signal


def _signal(symbol="005930", name="삼성전자") -> Signal:
    ts = pd.Timestamp("2026-09-18")
    base = BaseCandle(ts, 9000, 11000, 9000, 9500, 1e6, 0.5, 30, True, -0.4)
    return Signal(symbol=symbol, name=name, signal_date=ts, base=base, prev_high=12500,
                  trigger_price=12500, last_price=12800, volume=1e6, volume_ratio_prev=1.8,
                  entry=12500, stop=11200, targets=(13800, 15100, 16400),
                  adjust_days=2, adjust_vol_ratio=0.15)


def test_format_includes_toss_link():
    msg = TelegramNotifier("t", "c")._format(_signal())
    assert "https://www.tossinvest.com/stocks/005930" in msg
    assert "세력 매집 돌파 포착" in msg


def test_format_escapes_ampersand_in_name():
    """F&F·동국S&C 같은 종목명이 HTML 파싱을 깨지 않도록 이스케이프한다."""
    msg = TelegramNotifier("t", "c")._format(_signal(name="F&F"))
    assert "F&amp;F" in msg and "<b>F&F" not in msg
