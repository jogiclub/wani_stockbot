"""유니버스 스캔 오케스트레이션."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import pandas as pd

from .config import StrategyConfig
from .data_loader import DataLoader, StockMeta, TossDataLoader
from .pattern_detector import PatternDetector, Signal, enrich
from .regime import MarketRegime
from .scoring import score_signal

log = logging.getLogger(__name__)


WARNING_LABELS = {
    "LIQUIDATION_TRADING": "정리매매",
    "OVERHEATED": "단기과열",
    "INVESTMENT_WARNING": "투자경고",
    "INVESTMENT_RISK": "투자위험",
    "STOCK_WARRANTS": "신주인수권",
    "VI_STATIC": "VI(정적)",
    "VI_DYNAMIC": "VI(동적)",
    "VI_STATIC_AND_DYNAMIC": "VI(정적+동적)",
}


@dataclass
class ScanReport:
    signals: list[Signal]
    scanned: int
    skipped: int
    errors: int
    blocked: list[dict] = field(default_factory=list)
    regime: dict = field(default_factory=dict)

    def to_frame(self) -> pd.DataFrame:
        if not self.signals:
            return pd.DataFrame()
        return pd.DataFrame([s.to_dict() for s in self.signals])


class Screener:
    """유니버스 필터링 → OHLCV 수집 → 패턴 판정 → 시그널 목록 생성."""

    def __init__(self, loader: DataLoader, cfg: StrategyConfig | None = None,
                 max_workers: int = 8):
        self.loader = loader
        self.cfg = cfg or StrategyConfig()
        self.detector = PatternDetector(self.cfg)
        self.max_workers = max_workers
        self._regime: MarketRegime | None = None

    # ------------------------------------------------------------ 유니버스
    def build_universe(self, limit: int | None = None,
                       symbols: list[str] | None = None) -> list[StockMeta]:
        if symbols:
            metas = []
            for sym in symbols:
                m = self.loader.meta(sym) if hasattr(self.loader, "meta") else None
                metas.append(m or StockMeta(sym, sym))
            kept = [m for m in metas if self._passes_meta(m)]
            if len(kept) != len(metas):
                log.info("종목 지정 필터링: %d → %d 종목 (거래정지·정리매매·우선주 제외)",
                         len(metas), len(kept))
            return kept

        metas = self.loader.universe()
        filtered = [m for m in metas if self._passes_meta(m)]
        log.info("유니버스 필터링: %d → %d 종목", len(metas), len(filtered))
        return filtered[:limit] if limit else filtered

    def _passes_meta(self, m: StockMeta) -> bool:
        """시장·거래정지·우선주 등 종목 메타 수준 필터 (모든 경로 공통)."""
        cfg = self.cfg
        if cfg.markets and m.market and m.market not in cfg.markets:
            return False
        if m.suspended:                     # 거래정지 / 정리매매
            return False
        if cfg.exclude_preferred and not m.is_common:
            return False
        return True

    # ------------------------------------------------------------ 시장 국면
    def regime(self, refresh: bool = False) -> MarketRegime | None:
        """시장 국면 판정기 (스캔마다 재사용, `refresh=True` 면 갱신)."""
        if not self.cfg.use_regime:
            return None
        if self._regime is None or refresh:
            try:
                self._regime = MarketRegime(self.loader, self.cfg.regime_symbol, self.cfg.regime_ma)
            except Exception as exc:
                log.warning("시장 국면 조회 실패(중립 처리): %s", exc)
                return None
        return self._regime

    # ------------------------------------------------------------ 스캔
    def scan(self, limit: int | None = None, symbols: list[str] | None = None,
             live: bool = False) -> ScanReport:
        """유니버스를 병렬로 스캔해 시그널을 모은다.

        live=True 면 현재가/장중 거래량을 반영해 실시간 돌파를 판정한다.
        """
        universe = self.build_universe(limit, symbols)
        prices = self._live_prices([m.symbol for m in universe]) if live else {}

        signals: list[Signal] = []
        scanned = skipped = errors = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._scan_one, m, prices.get(m.symbol)): m
                       for m in universe}
            for fut in as_completed(futures):
                meta = futures[fut]
                try:
                    status, sig = fut.result()
                except Exception as exc:
                    errors += 1
                    log.warning("%s 스캔 오류: %s", meta.symbol, exc)
                    continue
                if status == "skipped":
                    skipped += 1
                    continue
                scanned += 1
                if sig:
                    signals.append(sig)

        signals, blocked = self._filter_warned(signals)

        regime = self.regime(refresh=True)
        regime_on = regime.is_on() if regime and regime.available else None
        regime_info = regime.status() if regime else {"available": False}

        if self.cfg.regime_blocks and regime_on is False:
            blocked.extend({"종목코드": s.symbol, "종목명": s.name,
                            "사유": f"{self.cfg.regime_symbol} 지수 {self.cfg.regime_ma}일선 이탈"}
                           for s in signals)
            signals = []

        for sig in signals:
            sig.score = score_signal(sig, regime_on)
        signals.sort(key=lambda s: (s.score.total if s.score else 0, s.volume_ratio_prev),
                     reverse=True)

        log.info("스캔 완료: %d 종목 판정, %d 제외, %d 오류, 시그널 %d건%s",
                 scanned, skipped, errors, len(signals),
                 f" (제외 {len(blocked)}건)" if blocked else "")
        return ScanReport(signals, scanned, skipped, errors, blocked, regime_info)

    # ------------------------------------------------------------ 경고종목
    def _filter_warned(self, signals: list[Signal]) -> tuple[list[Signal], list[dict]]:
        """정리매매·투자경고·단기과열 등 매수 유의 종목을 시그널에서 걸러낸다.

        전수 조회는 비싸므로(STOCK 5 TPS) **시그널이 난 종목만** 조회한다.
        VI 같은 일시적 항목은 차단하지 않고 `notes` 로 표시만 한다.
        """
        cfg = self.cfg
        if not cfg.exclude_warned or not signals:
            return signals, []

        kept: list[Signal] = []
        blocked: list[dict] = []
        for sig in signals:
            try:
                items = self.loader.warnings(sig.symbol)
            except Exception as exc:
                log.warning("%s 유의사항 조회 실패(통과 처리): %s", sig.symbol, exc)
                kept.append(sig)
                continue

            types = [w.get("warningType") for w in items or []]
            hits = [t for t in types if t in cfg.blocking_warnings]
            if hits:
                labels = ", ".join(WARNING_LABELS.get(t, t) for t in hits)
                blocked.append({"종목코드": sig.symbol, "종목명": sig.name, "사유": labels})
                log.info("유의종목 제외: %s %s (%s)", sig.symbol, sig.name, labels)
                continue
            notices = [t for t in types if t in cfg.notice_warnings]
            if notices:
                sig.notes.extend(WARNING_LABELS.get(t, t) for t in notices)
            kept.append(sig)
        return kept, blocked

    def _scan_one(self, meta: StockMeta, live_price: float | None):
        cfg = self.cfg
        df = self.loader.ohlcv(meta.symbol, count=max(cfg.min_history + 40, 260))
        if len(df) < cfg.min_history:
            return "skipped", None

        data = enrich(df, cfg)
        last = data.iloc[-1]
        if not self._passes_liquidity(last, meta):
            return "skipped", None

        sig = self.detector.detect(data, meta.symbol, meta.name,
                                   live_price=live_price, at=len(data) - 1)
        return "scanned", sig

    def _passes_liquidity(self, last: pd.Series, meta: StockMeta) -> bool:
        cfg = self.cfg
        price = float(last["close"])
        if not (cfg.min_price <= price <= cfg.max_price):
            return False
        turnover = float(last.get("turnover_ma", float("nan")))
        if turnover == turnover and turnover < cfg.min_avg_turnover:
            return False
        if cfg.max_market_cap and meta.shares_outstanding == meta.shares_outstanding:
            if price * meta.shares_outstanding > cfg.max_market_cap:
                return False
        return True

    def _live_prices(self, symbols: list[str]) -> dict[str, float]:
        if not isinstance(self.loader, TossDataLoader):
            return {}
        try:
            return self.loader.last_prices(symbols)
        except Exception as exc:
            log.warning("현재가 조회 실패: %s", exc)
            return {}

    # ------------------------------------------------------------ 관심종목 감시
    def watch_candidates(self, limit: int | None = None,
                         symbols: list[str] | None = None,
                         exclude: set[str] | frozenset[str] = frozenset()) -> list[dict]:
        """아직 돌파 전이지만 STEP 1+2 를 통과한 '대기' 종목 목록.

        STEP 3(돌파)는 보지 않으므로 오늘 이미 시그널이 뜬 종목도 걸린다.
        매수 시그널과 겹치지 않게 그런 종목은 `exclude` 로 넘겨 뺀다.

        판정 기준은 `detect` 와 같다 — 마지막 봉을 '돌파 판정일' 로 두고
        그 전날까지를 조정 구간으로 본다. 그래서 감시 돌파가는 마지막 봉의
        **전일 고가**(`prev_high`)다. 마지막 봉 자신의 고가를 쓰면 시그널의
        전일고가와 값이 어긋난다 (실제로 겪은 혼동).
        """
        out: list[dict] = []
        for meta in self.build_universe(limit, symbols):
            if meta.symbol in exclude:
                continue
            try:
                df = self.loader.ohlcv(meta.symbol, count=max(self.cfg.min_history + 40, 260))
                if len(df) < self.cfg.min_history:
                    continue
                data = enrich(df, self.cfg)
                if not self._passes_liquidity(data.iloc[-1], meta):
                    continue
                bases = self.detector.find_base_candles(data)
                if not bases:
                    continue
                base = bases[0]
                ok, ratio, _ = self.detector.check_adjustment(data, base, len(data) - 1)
                if not ok:
                    continue
                out.append({
                    "종목코드": meta.symbol, "종목명": meta.name,
                    "판정일": data.index[-1].strftime("%Y-%m-%d"),
                    "기준봉일": base.date.strftime("%Y-%m-%d"),
                    "경과일": base.bars_since,
                    "감시_돌파가(전일고가)": round(float(data["prev_high"].iloc[-1]), 2),
                    "기준봉저가(손절)": round(base.low, 2),
                    "조정거래량비": round(ratio, 2),
                })
            except Exception as exc:
                log.debug("%s 관심종목 판정 실패: %s", meta.symbol, exc)
        return out
