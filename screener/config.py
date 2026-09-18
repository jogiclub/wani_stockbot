"""전략 파라미터 및 환경 설정."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv 미설치 시에도 동작
    def load_dotenv(*_args, **_kwargs):
        return False

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


@dataclass
class StrategyConfig:
    """세력 추정 매매법 전략 파라미터.

    STEP 1(기준봉) → STEP 2(매물 소화) → STEP 3(전일 고가 돌파) 각 단계의
    임계값을 한 곳에서 관리한다. 백테스트/최적화 시 이 객체만 바꾸면 된다.
    """

    # ---- STEP 1: 기준봉(매집봉) ----
    bottom_lookback: int = 120           # 바닥권 판별에 사용할 조회 기간(일)
    bottom_drawdown_min: float = 0.30    # 기간 내 최고가 대비 최소 하락률 (30%)
    ma_long: int = 120                   # 장기 이동평균선
    ma_long_band: float = 1.10           # 종가 <= MA120 * 1.10 이면 '장기선 근접' 인정
    upper_tail_min: float = 0.35         # 윗꼬리 / 전체 변동폭 최소 비율
    vol_lookback: int = 20               # 대량 거래량 비교 기간
    vol_mult_min: float = 5.0            # 20일 평균 거래량 대비 배수 (500%)
    require_vol_max: bool = True         # 20일 내 최고 거래량 조건도 함께 볼지
    vol_condition_mode: str = "or"       # "or" = 최고거래량 또는 평균배수, "and" = 둘 다
    base_range_min: float = 0.05         # 기준봉 당일 변동폭(고가/저가-1) 최소치

    # ---- STEP 2: 조정 / 매물 소화 ----
    adjust_min_days: int = 1             # 기준봉 이후 최소 경과 영업일
    adjust_max_days: int = 10            # 기준봉 이후 최대 경과 영업일
    adjust_vol_max_ratio: float = 0.60   # 조정 구간 평균 거래량 / 기준봉 거래량 상한
    adjust_hold_base_low: bool = True    # 조정 중 기준봉 저가 이탈 시 패턴 무효
    adjust_max_gain: float = 0.15        # 조정 중 기준봉 고가를 이 비율 이상 넘으면 이미 시세 분출 → 제외

    # ---- STEP 3: 돌파 시그널 ----
    trigger_vol_ratio: float = 1.0       # 당일 거래량 / 전일 거래량 최소 배수 (1.0 = 초과)
    trigger_break_prev_high: bool = True # 전일 고가 돌파 필수
    trigger_break_buffer: float = 0.0    # 전일 고가 * (1 + buffer) 를 돌파 기준으로 (0.003 = 0.3%)

    # ---- 리스크 관리 ----
    stop_mode: str = "base_low"          # "base_low" | "prev_low" | "pct"
    stop_pct: float = 0.05               # stop_mode == "pct" 일 때 손절 폭
    target_r_multiples: tuple = (1.0, 2.0, 3.0)  # 손절폭 대비 목표가 배수

    # ---- 유니버스 필터 ----
    markets: tuple = ("KOSPI", "KOSDAQ")
    min_price: float = 500.0             # 동전주 제외
    max_price: float = 300_000.0
    min_avg_turnover: float = 5e8        # 20일 평균 거래대금 하한 (5억원)
    max_market_cap: float = 3e12         # 시가총액 상한 (3조) - 중소형주 타깃, 0이면 미적용
    exclude_preferred: bool = True       # 우선주 제외
    min_history: int = 130               # 판정에 필요한 최소 일봉 개수

    # ---- 시장 국면(regime) 필터 ----
    # 코스닥 지수가 20일 이동평균 위일 때만 우호적 국면으로 본다.
    # 3년 백테스트: 통과 구간 +0.173R vs 이탈 구간 -0.117R (MA 5/10/20/60 모두 같은 방향)
    regime_symbol: str = "KOSDAQ"
    regime_ma: int = 20
    use_regime: bool = True       # 국면을 점수·표시에 반영
    regime_blocks: bool = False   # True 면 이탈 국면에서 시그널을 아예 제외

    # ---- 매수 유의사항(경고종목) 필터 ----
    # 시그널이 난 종목에 한해 조회한다 (STOCK 그룹 5 TPS 이므로 전수 조회는 비용이 큼).
    exclude_warned: bool = True
    blocking_warnings: tuple = (
        "LIQUIDATION_TRADING",   # 정리매매 (상장폐지 절차)
        "INVESTMENT_WARNING",    # 투자경고종목
        "INVESTMENT_RISK",       # 투자위험종목
        "OVERHEATED",            # 단기과열종목
        "STOCK_WARRANTS",        # 신주인수권증서/증권
    )
    # 차단하지 않고 표시만 하는 유형 (VI 는 일시적 이벤트)
    notice_warnings: tuple = ("VI_STATIC", "VI_DYNAMIC", "VI_STATIC_AND_DYNAMIC")

    # ------------------------------------------------------------ 프로파일
    @classmethod
    def spec(cls, **overrides) -> "StrategyConfig":
        """원본 전략 스펙 그대로의 기본값 (윗꼬리 35%, 거래량 or 조건, 조정 1~10일).

        2023~2026 3년 백테스트 결과 **손실**(거래 15,099건, 승률 48.0%,
        평균 -0.084R, 수익팩터 0.84). 비교·연구용으로만 쓴다.
        """
        return cls(**overrides)

    @classmethod
    def tuned(cls, **overrides) -> "StrategyConfig":
        """백테스트로 선별한 운영 프로파일.

        3년 2,563종목 백테스트: 거래 1,230건, 승률 55.2%, 평균 +0.076R,
        수익팩터 1.17, 최대낙폭 40.5R, 최대 연속손실 7.
        - 윗꼬리 45% 이상  (35~45% 구간은 평균 -0.062R 로 손실)
        - 기준봉 거래량 20일 최고 **그리고** 20일 평균의 20배 이상 (or → and)
        - 조정 기간 1~3일  (7~10일 구간은 평균 -0.169R)
        """
        params = dict(upper_tail_min=0.45, vol_mult_min=20.0,
                      vol_condition_mode="and", adjust_max_days=3)
        params.update(overrides)
        return cls(**params)

    def describe(self) -> dict:
        return asdict(self)


@dataclass
class AppConfig:
    """실행 환경 설정 (자격증명·캐시·알림)."""

    toss_client_id: str = field(default_factory=lambda: os.getenv("TOSS_CLIENT_ID", ""))
    toss_client_secret: str = field(default_factory=lambda: os.getenv("TOSS_CLIENT_SECRET", ""))
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    cache_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "cache")

    # ---- 실거래 (기본 OFF) ----
    # 켜기 전에 반드시 모의(dry-run)로 흐름을 확인할 것. 실제 돈이 나간다.
    trading_enabled: bool = field(
        default_factory=lambda: os.getenv("TOSS_TRADING_ENABLED", "").lower() == "true")
    max_order_amount: float = field(
        default_factory=lambda: float(os.getenv("TOSS_MAX_ORDER_AMOUNT", "1000000")))
    account_seq: int | None = field(
        default_factory=lambda: int(os.getenv("TOSS_ACCOUNT_SEQ")) if os.getenv("TOSS_ACCOUNT_SEQ") else None)
    order_log: Path = field(default_factory=lambda: PROJECT_ROOT / "out" / "orders.jsonl")

    # ---- 로그인 · 설정 저장 (SQLite) ----
    # `app_secret_key` 는 DB 에 저장된 자격증명을 푸는 열쇠다. 바꾸면 저장값을 못 읽는다.
    # 초기 계정 비밀번호는 **코드에 두지 않는다** — `.env` 의 APP_ADMIN_PASSWORD 로 준다.
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("APP_DB_PATH") or (PROJECT_ROOT / "cache" / "app.db")))
    app_secret_key: str = field(default_factory=lambda: os.getenv("APP_SECRET_KEY", "jogiclub"))
    admin_user: str = field(default_factory=lambda: os.getenv("APP_ADMIN_USER", "jogiclub"))
    admin_password: str = field(default_factory=lambda: os.getenv("APP_ADMIN_PASSWORD", ""))
    # 회원가입: 켜면 로그인 화면에서 누구나 **일반 사용자** 계정을 만들 수 있다.
    # 일반 사용자는 자기 토스 키로만 주문·봇을 쓴다 (서버 `.env` 계좌는 관리자 전용).
    # APP_SIGNUP_CODE 를 넣으면 그 초대 코드를 아는 사람만 가입할 수 있다.
    signup_enabled: bool = field(
        default_factory=lambda: os.getenv("APP_SIGNUP", "true").lower() in ("1", "true", "yes", "on"))
    signup_code: str = field(default_factory=lambda: os.getenv("APP_SIGNUP_CODE", ""))

    def require_toss(self) -> None:
        if not self.toss_client_id or not self.toss_client_secret:
            raise RuntimeError(
                "TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이 없습니다. .env 파일을 확인하세요."
            )
