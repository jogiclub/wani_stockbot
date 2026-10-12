"""OHLCV 데이터 수집 계층.

모든 로더는 동일한 DataFrame 계약을 지킨다:
    index : DatetimeIndex (KST, 오름차순, 중복 없음)
    columns: open, high, low, close, volume  (float)
"""

from __future__ import annotations

import abc
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .toss_client import TossClient

log = logging.getLogger(__name__)

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class StockMeta:
    symbol: str
    name: str
    market: str = ""
    shares_outstanding: float = float("nan")
    is_common: bool = True
    suspended: bool = False
    security_type: str = ""            # STOCK | ETF | ETN … (토스 `securityType`)

    @classmethod
    def from_toss(cls, d: dict) -> "StockMeta":
        kr = d.get("koreanMarketDetail") or {}
        return cls(
            symbol=d["symbol"],
            name=d.get("name") or d.get("englishName") or d["symbol"],
            market=d.get("market", ""),
            shares_outstanding=_to_float(d.get("sharesOutstanding")),
            is_common=bool(d.get("isCommonShare", True)),
            suspended=bool(kr.get("krxTradingSuspended", False)) or bool(kr.get("liquidationTrading", False)),
            security_type=d.get("securityType") or "",
        )


class DataLoader(abc.ABC):
    """OHLCV 로더 인터페이스."""

    @abc.abstractmethod
    def universe(self) -> list[StockMeta]:
        """스크리닝 대상 종목 목록."""

    @abc.abstractmethod
    def ohlcv(self, symbol: str, count: int = 260) -> pd.DataFrame:
        """일봉 OHLCV DataFrame."""

    def warnings(self, symbol: str) -> list[dict]:
        """매수 유의사항 목록. 지원하지 않는 로더는 빈 목록을 돌려준다."""
        return []

    def index_ohlcv(self, symbol: str = "KOSDAQ", count: int = 800) -> pd.DataFrame:
        """시장 지수 일봉. 지원하지 않는 로더는 빈 DataFrame 을 돌려준다."""
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    def ohlcv_batch(self, symbols: list[str], count: int = 260) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            try:
                df = self.ohlcv(sym, count)
                if not df.empty:
                    out[sym] = df
            except Exception as exc:  # 한 종목 실패가 전체를 막지 않도록
                log.warning("%s OHLCV 조회 실패: %s", sym, exc)
        return out


# --------------------------------------------------------------------------- Toss
class TossDataLoader(DataLoader):
    """토스증권 OpenAPI 기반 로더 (일봉 캐시 지원)."""

    def __init__(self, client: TossClient, markets=("KOSPI", "KOSDAQ"),
                 cache_dir: Path | None = None, cache_ttl: float = 3600.0):
        self.client = client
        self.markets = tuple(markets)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl
        if self.cache_dir:
            for sub in ("candles", "warnings"):
                (self.cache_dir / sub).mkdir(parents=True, exist_ok=True)
        self._meta: dict[str, StockMeta] = {}

    # -- universe --------------------------------------------------------
    def universe(self) -> list[StockMeta]:
        metas: list[StockMeta] = []
        for market in self.markets:
            raw = self._cached_json(f"universe_{market}.json", 12 * 3600,
                                    lambda m=market: self.client.stocks_all(m))
            for d in raw:
                meta = StockMeta.from_toss(d)
                metas.append(meta)
                self._meta[meta.symbol] = meta
        log.info("유니버스 %d 종목 (%s)", len(metas), ", ".join(self.markets))
        return metas

    def meta(self, symbol: str) -> StockMeta | None:
        if symbol not in self._meta:
            found = self.client.stocks([symbol])
            if found:
                self._meta[symbol] = StockMeta.from_toss(found[0])
        return self._meta.get(symbol)

    def top_by_turnover(self, count: int = 100, duration: str = "1d") -> list[str]:
        """거래대금 상위 랭킹으로 후보를 좁힐 때 사용 (빠른 스캔용).

        랭킹 API 는 종류당 상위 100 위까지만 주므로, 100 을 넘게 요청하면
        거래대금·거래량·등락률 랭킹을 합쳐 순서를 유지한 채 중복을 제거한다.
        """
        types = ["MARKET_TRADING_AMOUNT"]
        if count > 100:
            types += ["MARKET_TRADING_VOLUME", "TOP_GAINERS"]

        out: list[str] = []
        seen: set[str] = set()
        for t in types:
            for item in self.client.rankings(t, "KR", duration, 100):
                sym = item.get("symbol")
                if sym and sym not in seen:
                    seen.add(sym)
                    out.append(sym)
            if len(out) >= count:
                break
        log.info("랭킹 후보 %d 종목 (%s)", len(out[:count]), ", ".join(types))
        return out[:count]

    # -- candles ---------------------------------------------------------
    def ohlcv(self, symbol: str, count: int = 260) -> pd.DataFrame:
        key = f"candles/{symbol}_{count}.json"
        raw = self._cached_json(key, self.cache_ttl,
                                lambda: self.client.candles_paged(symbol, "1d", count))
        return candles_to_df(raw)

    def intraday(self, symbol: str, count: int = 200) -> pd.DataFrame:
        """1분봉 (돌파 시점 정밀 추적용). 캐시하지 않는다."""
        return candles_to_df(self.client.candles(symbol, "1m", count))

    def warnings(self, symbol: str) -> list[dict]:
        """매수 유의사항(정리매매·투자경고·단기과열·VI 등). 10분 캐시."""
        return self._cached_json(f"warnings/{symbol}.json", 600.0,
                                 lambda: self.client.warnings(symbol)) or []

    # -- 금요일 마감 돌파 (screener/friday.py) -------------------------------
    def turnover_leaders(self, count: int = 100) -> list[tuple[str, float]]:
        """당일 거래대금 상위 (종목코드, 누적 거래대금). 랭킹 API 상한 100."""
        out = []
        for item in self.client.rankings("MARKET_TRADING_AMOUNT", "KR", "1d", min(count, 100)):
            sym = item.get("symbol")
            if sym:
                out.append((sym, _to_float(item.get("tradingAmount"))))
        return out

    def metas(self, symbols: list[str]) -> dict[str, StockMeta]:
        """종목 메타를 한 번에 채운다 (이미 아는 종목은 다시 묻지 않는다)."""
        missing = [s for s in symbols if s not in self._meta]
        if missing:
            for d in self.client.stocks(missing):
                meta = StockMeta.from_toss(d)
                self._meta[meta.symbol] = meta
        return {s: self._meta[s] for s in symbols if s in self._meta}

    def session_minutes(self, symbol: str, max_pages: int = 6) -> pd.DataFrame:
        """가장 최근 정규장 하루치 1분봉 (09:00 ~ 판정 시각).

        1분봉 응답에는 시간외(NXT 등) 봉이 섞여 오고 최신순이다. 정규장 봉이 있는
        가장 최근 날짜를 찾고, 그날 09:00 이전까지 페이지를 넘겨 받는다.
        """
        from .intraday import SESSION_OPEN, session_bars

        frames: list[pd.DataFrame] = []
        before: str | None = None
        session = None
        for _ in range(max_pages):
            page = self.client.candles_page(symbol, "1m", 200, before)
            chunk = page.get("candles") or []
            if not chunk:
                break
            df = candles_to_df(chunk)
            frames.append(df)
            if session is None:
                reg = session_bars(df)
                if not reg.empty:
                    session = reg.index.max().date()
            if session is not None and df.index.min() <= pd.Timestamp.combine(session, SESSION_OPEN):
                break
            before = page.get("nextBefore")
            if not before:
                break
        if session is None or not frames:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        merged = pd.concat(frames).sort_index()
        merged = merged[~merged.index.duplicated(keep="last")]
        return session_bars(merged, session)

    def minute_profile(self, symbol: str, day, minutes: int = 60) -> pd.Series:
        """과거 영업일 `day` 의 09:00~09:00+minutes 1분 거래량 (index = 분 오프셋).

        지나간 날의 분봉은 바뀌지 않으므로 파일로 영구 캐시한다.
        """
        from .intraday import minute_offset, session_bars

        key = f"intraday/{symbol}_{day.isoformat()}_{minutes}.json"

        def fetch():
            end = pd.Timestamp.combine(day, pd.Timestamp("09:00").time()) + pd.Timedelta(minutes=minutes)
            page = self.client.candles_page(symbol, "1m", 200,
                                            end.strftime("%Y-%m-%dT%H:%M:59+09:00"))
            df = session_bars(candles_to_df(page.get("candles") or []), day)
            return [[minute_offset(ts), float(v)] for ts, v in df["volume"].items()
                    if minute_offset(ts) <= minutes]

        raw = self._cached_json(key, 10 * 365 * 86400, fetch) or []
        return pd.Series({int(k): v for k, v in raw}, dtype=float).sort_index()

    def price_at(self, symbol: str, day, minutes: int = 30) -> float:
        """과거 영업일 `day` 의 09:00+minutes 분 시점 가격(그 시각까지 마지막 정규장 봉 종가).

        금요일 마감 매매의 월요일 09:30 결과 채우기용. 지나간 날이라 영구 캐시한다.
        봉이 없으면 NaN.
        """
        from .intraday import session_bars

        key = f"intraday/{symbol}_{day.isoformat()}_px{minutes}.json"

        def fetch():
            end = pd.Timestamp.combine(day, pd.Timestamp("09:00").time()) + pd.Timedelta(minutes=minutes)
            page = self.client.candles_page(symbol, "1m", 200,
                                            end.strftime("%Y-%m-%dT%H:%M:59+09:00"))
            df = session_bars(candles_to_df(page.get("candles") or []), day)
            return float(df["close"].iloc[-1]) if not df.empty else None

        raw = self._cached_json(key, 10 * 365 * 86400, fetch)
        return float("nan") if raw is None else float(raw)

    def index_ohlcv(self, symbol: str = "KOSDAQ", count: int = 800) -> pd.DataFrame:
        """시장 지수 일봉 (시장 국면 필터용). 6시간 캐시."""
        raw = self._cached_json(f"index_{symbol}_{count}.json", 6 * 3600,
                                lambda: self.client.index_candles_paged(symbol, "1d", count))
        return candles_to_df(raw)

    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        return {p["symbol"]: _to_float(p.get("lastPrice")) for p in self.client.prices(symbols)}

    # -- cache -----------------------------------------------------------
    def _cached_json(self, key: str, ttl: float, producer):
        if not self.cache_dir:
            return producer()
        path = self.cache_dir / key
        if path.exists() and (time.time() - path.stat().st_mtime) < ttl:
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                pass
        data = producer()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False))
        return data


def candles_to_df(candles: list[dict]) -> pd.DataFrame:
    """토스 캔들 응답 → 표준 OHLCV DataFrame (문자열 decimal → float)."""
    if not candles:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    df = pd.DataFrame(candles)
    df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601", utc=True)
    df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Seoul").dt.tz_localize(None)
    df = df.rename(columns={
        "openPrice": "open", "highPrice": "high", "lowPrice": "low",
        "closePrice": "close", "volume": "volume",
    })
    df = df[["timestamp", *OHLCV_COLUMNS]].astype({c: "float64" for c in OHLCV_COLUMNS})
    return (df.set_index("timestamp")
              .sort_index()
              .loc[lambda d: ~d.index.duplicated(keep="last")])


# --------------------------------------------------------------------------- CSV
class CsvDataLoader(DataLoader):
    """`<dir>/<symbol>.csv` (date,open,high,low,close,volume) 를 읽는 오프라인 로더."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def universe(self) -> list[StockMeta]:
        return [StockMeta(p.stem, p.stem) for p in sorted(self.directory.glob("*.csv"))]

    def ohlcv(self, symbol: str, count: int = 260) -> pd.DataFrame:
        df = pd.read_csv(self.directory / f"{symbol}.csv", parse_dates=[0], index_col=0)
        df.columns = [c.lower() for c in df.columns]
        return df[OHLCV_COLUMNS].astype(float).sort_index().tail(count)


# ----------------------------------------------------------------- Synthetic PoC
class SyntheticDataLoader(DataLoader):
    """API 없이 알고리즘을 검증하기 위한 가상 데이터 생성기.

    `signal_symbols` 로 지정한 종목에는 기준봉 → 매물 소화 → 전일 고가 돌파
    패턴을 의도적으로 심어 넣는다.
    """

    def __init__(self, n_symbols: int = 8, signal_symbols: tuple[str, ...] = ("900001",),
                 seed: int = 20260901, days: int = 260):
        self.n_symbols = n_symbols
        self.signal_symbols = set(signal_symbols)
        self.seed = seed
        self.days = days

    def universe(self) -> list[StockMeta]:
        syms = [f"90000{i}" for i in range(1, self.n_symbols + 1)]
        return [StockMeta(s, f"테스트{s[-1]}", "KOSDAQ", 20_000_000) for s in syms]

    def ohlcv(self, symbol: str, count: int = 260) -> pd.DataFrame:
        rng = np.random.default_rng(self.seed + int(symbol[-3:]))
        n = self.days
        dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)

        # 완만한 하락 후 바닥 횡보 → 바닥권 조건 충족
        drift = np.linspace(0.0, -0.55, n) + rng.normal(0, 0.012, n).cumsum() * 0.3
        close = 10_000 * np.exp(drift)
        high = close * (1 + np.abs(rng.normal(0, 0.012, n)))
        low = close * (1 - np.abs(rng.normal(0, 0.012, n)))
        open_ = np.clip(close * (1 + rng.normal(0, 0.008, n)), low, high)
        volume = rng.lognormal(12.0, 0.35, n)

        if symbol in self.signal_symbols:
            b = n - 7                                  # 기준봉: 6영업일 전
            base = close[b - 1]
            open_[b] = base
            close[b] = base * 1.09
            high[b] = base * 1.30                      # 윗꼬리 = 변동폭의 약 60%
            low[b] = base * 0.98
            volume[b] = volume[b - 20:b].max() * 9

            for k in range(b + 1, n - 1):              # 매물 소화: 거래량 감소 + 눌림
                decay = 0.30 * (0.85 ** (k - b))
                volume[k] = volume[b] * decay
                close[k] = close[b] * (0.955 - 0.008 * (k - b))
                open_[k] = close[k] * 1.012
                high[k] = max(open_[k], close[k]) * 1.012
                low[k] = min(open_[k], close[k]) * 0.985

            t = n - 1                                   # 돌파일
            prev_high = high[t - 1]
            open_[t] = close[t - 1] * 1.005
            close[t] = prev_high * 1.045
            high[t] = prev_high * 1.06
            low[t] = open_[t] * 0.995
            volume[t] = volume[t - 1] * 3.2

        df = pd.DataFrame({"open": open_, "high": high, "low": low,
                           "close": close, "volume": volume}, index=dates)
        return df.round(2).tail(count)


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")
