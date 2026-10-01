# stock-find (왔니 주식) — 자동 매매 전략 대시보드

> 이 문서는 프로젝트 지침서다. **새로 확인된 사실·결정·API 스펙은 여기에 계속 누적 기록한다.**

## 프로젝트 목표
세 전략을 한 대시보드에서 돌린다. 상단 GNB 로 화면을 오간다 (브랜드: **왔니 주식**).
GNB 순서: **수급포착매매 → 세력추정매매 → 볼린저 밴드 반전 매매**.

0. **수급포착매매** (`/supply`) — 장 초반 거래량 급증 + 수급 연속성으로 주도주를 포착하는
   관찰용 스크리너. 주문 없음, **백테스트 미검증** (아래 "수급 포착 매매" 절).
1. **세력추정매매** (`/`) — 바닥권에서 대량 거래량을 동반한 윗꼬리 캔들(기준봉/매집봉) 발생 →
   매물 소화(조정) → **전일 고가**를 거래량과 함께 돌파하는 순간을 포착해 KOSPI/KOSDAQ 을 스크리닝.
2. **볼린저 밴드 반전 매매** (`/bollinger`) — 박스권에서 밴드 하한 + RSI 과매도에 사고
   상한 + 과매수에 파는 평균 회귀 전략. **토스 OpenAPI 로 실제 시장가 매수·매도까지 자동 실행**한다.

## 모듈 구조
| 파일 | 역할 |
|---|---|
| `screener/config.py` | `StrategyConfig`(전략 임계값) · `AppConfig`(자격증명/캐시). `.env` 로드 |
| `screener/toss_client.py` | 토스증권 OpenAPI REST 클라이언트 (토큰 자동 갱신, 그룹별 TPS 제한, 429/5xx 백오프) |
| `screener/data_loader.py` | `DataLoader` 인터페이스 + `TossDataLoader` / `CsvDataLoader` / `SyntheticDataLoader`, 일봉 디스크 캐시 |
| `screener/pattern_detector.py` | STEP1~3 판정 엔진 (`enrich`, `PatternDetector`, `BaseCandle`, `Signal`) |
| `screener/screener.py` | 유니버스 필터 → 병렬 스캔 → 시그널 집계 (`Screener`, `ScanReport`) |
| `screener/notifier.py` | 콘솔 표 / 텔레그램 / **이메일(Gmail SMTP)** / JSONL / 중복제거 래퍼 |
| `screener/trading.py` | 토스 주문 실행 (`TradingService`) — 매수 + OCO 손절·익절 |
| `screener/evidence.py` | 백테스트 실측 근거 수치의 **단일 출처**. README·CLAUDE.md·`/api/guide` 가 모두 참조 |
| `screener/regime.py` | 시장 국면 판정 (`MarketRegime`) — 지수 vs 이동평균 |
| `screener/scoring.py` | 진입 점수 100점 산정 (`score_signal`) — 배점은 백테스트 실측 기반 |
| `screener/backtest.py` | 백테스트 엔진. `Backtester`(시그널 재현 + 청산 시뮬), `ExitPolicy`, `Trade`, `BacktestResult` |
| `screener/supply.py` | 수급 포착 매매 (`SupplyConfig`·`evaluate`·`SupplyScanner`·`SupplyService`) |
| `screener/static/supply.html` | 수급포착매매 화면 (`/supply`). 당일 1분봉·VWAP·누적 거래량·누적 매수세 SVG |
| `screener/static/stockmenu.js` | **공통 종목 메뉴 모달** (토스로 보기 / 반전 매매에 종목추가) |
| `screener/bollinger.py` | 볼린저 반전 전략 지표·판정 엔진 (`BollingerConfig`, `rsi`, `bollinger_bands`, `prepare`, `decide`, `evaluate`) |
| `screener/bollinger_bot.py` | 볼린저 자동매매 루프 (`BollingerBot`) — 잔고 동기화 → 판정 → 시장가 주문 |
| `screener/web.py` | FastAPI 대시보드 + JSON API. 요청마다 세션/서버 스캐너를 골라 쓴다 |
| `screener/scan_service.py` | `ScanService`·`ScanState` (주기 스캔 스레드). web·sessions 공용 — 순환 import 방지용 분리 |
| `screener/sessions.py` | 접속자별 토스 API 키 세션 (`SessionManager`·`UserSession`·`SessionSettings`) |
| `screener/static/dashboard.html` | 세력추정매매 화면 (`/`). vanilla JS + 인라인 SVG 캔들/거래량 차트 |
| `screener/static/bollinger.html` | 볼린저 반전 매매 화면 (`/bollinger`). 밴드+RSI 인라인 SVG 차트 |
| `screener/static/ui.js` | **공통 UI 기반** — Tailwind 설정(색 토큰 매핑) + `UI.cls`(버튼·폼·카드 클래스 묶음) + `Grid.create()`(AG Grid 헬퍼) |
| `screener/static/common.css` | **테마 토큰(CSS 변수)만** — 화면 스타일은 Tailwind 로 쓴다. `.tint-*`·`.dot-*`·AG Grid 미세 규칙·`[hidden]` 가드만 남김 |
| `screener/static/gnb.js` | **공통 GNB**(왔니 주식 브랜드 + 메뉴) + 테마 토글 |
| `screener/static/session.js` | **공통 모달 2개** — 🔑 API 키(자격증명) / ⚙ 설정(스캔·알림·주문·자동매수) |
| `main.py` | CLI: `scan` `watch` `poll` `web` `backtest` `explain` `demo` `bollinger` `adduser` `supply` |
| `poc.py` | API 없이 도는 최소 동작 예시 |
| `tests/` | pytest 단위 테스트 (외부 API 불필요) |

**DataFrame 계약**: index=DatetimeIndex(KST, 오름차순), columns=`open,high,low,close,volume`(float).

## 알고리즘 요약
- **STEP 1 기준봉**: `(바닥권: 120일 최고 종가 대비 -30% 이상 하락 OR 종가 ≤ MA120×1.10)`
  AND `윗꼬리비율 = (고가-max(시가,종가))/(고가-저가) ≥ 0.35` AND `당일변동폭 ≥ 5%`
  AND `(20일 내 최고 거래량 OR 20일 평균 거래량 ×5 이상)`
- **STEP 2 매물 소화**: 기준봉 이후 1~10영업일, 조정 구간 평균거래량/기준봉 거래량 ≤ 0.60,
  기준봉 저가 종가 이탈 시 무효, 기준봉 고가 +15% 초과 상회 시 이미 분출로 제외
- **STEP 3 돌파**: `당일 거래량 ≥ 전일 거래량 ×1.0` AND `가격 > 전일 고가 ×(1+buffer)`
  (장중에는 `live_price` 로 실시간 판정)
- **리스크**: 매수가=전일 고가, 손절=기준봉 저가(`stop_mode`로 변경 가능), 목표=1R/2R/3R

**미래 정보 차단(look-ahead 방지)**: 거래량 평균·최고는 `shift(1)` 기준으로 계산한다. 이 규칙을 깨지 말 것.

## 증권사 연동 — 토스증권 OpenAPI
- 문서: https://developers.tossinvest.com/docs , LLM용 스펙 `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`
- REST Base: `https://openapi.tossinvest.com` / WebSocket: `wss://openapi-ws.tossinvest.com/ws/v1`
- 인증: `POST /oauth2/token` (form-urlencoded, `grant_type=client_credentials`).
  **클라이언트당 유효 토큰 1개** — 재발급 시 이전 토큰이 즉시 무효화된다.
  - 같은 자격증명으로 여러 프로세스(web·poller)를 띄우면 서로 토큰을 무효화시켜
    **주문 POST 가 `401 invalid-token` 으로 실패**한다. 실제로 발생했던 버그다.
  - 해결: `_SharedTokenStore` 가 `cache/token.json` + 파일 락으로 토큰을 프로세스 간 공유한다
    (`TossClient(token_cache=...)`). 발급을 락 안에서 직렬화하고 결과를 나눠 쓴다.
  - 추가 방어: `post()` 는 **401 에 한해 1회만** 재시도한다. 401 은 인증 계층 거절이라
    주문 처리에 도달하지 못한 것이 확실하므로 중복 체결 위험이 없다.
    타임아웃·5xx·429 는 여전히 재시도하지 않는다.
- **TPS 제한은 프로세스별**이라 web·poller 를 함께 돌리면 합산 초과로 429 가 난다.
  둘 다 필요하면 자격증명을 분리하거나 한쪽만 띄울 것 (web 이 poller 기능을 포함한다).
- 성공 응답 envelope: `{"result": ...}` / 실패: `{"error": {...}}`
- 주요 엔드포인트
  - `GET /api/v1/candles?symbol&interval=1d|1m&count(≤200)&before&adjusted` → `{candles:[{timestamp,openPrice,...,volume}], nextBefore}`
    - 가격/거래량은 **문자열 decimal** → float 변환 필요. `before`의 `+`는 `%2B` 인코딩.
  - `GET /api/v1/stocks/all?market=KOSPI|KOSDAQ&status=ACTIVE&securityType=STOCK&commonShare=true` → 유니버스 (하루 1회 캐싱 권장)
  - `GET /api/v1/stocks?symbols=` (최대 200건 콤마) → 종목명/시장/발행주식수/`koreanMarketDetail.krxTradingSuspended`
  - `GET /api/v1/prices?symbols=` (최대 200건) → 현재가
  - `GET /api/v1/rankings?type=MARKET_TRADING_AMOUNT&marketCountry=KR&duration=1d&count=100` → 거래대금 상위
    - **`count` 상한 100. 초과하면 400 `invalid-request`** (원인 규명 완료).
    - 응답 목록 키는 **`rankings`** (`items` 아님). `{"result":{"rankedAt":..., "rankings":[...]}}`
    - 100 종목 초과 후보가 필요하면 `MARKET_TRADING_AMOUNT` + `MARKET_TRADING_VOLUME` + `TOP_GAINERS`
      를 합쳐 중복 제거한다 (`TossDataLoader.top_by_turnover` 구현).
  - `GET /api/v1/stocks/{symbol}/warnings` → 정리매매·투자경고·VI 등
  - `GET /api/v1/holdings` (그룹 `ASSET`) → `{totalPurchaseAmount, marketValue, profitLoss, items[]}`
    - **금액은 통화별 dict** 다: `totalPurchaseAmount: {"krw": "...", "usd": "..."}`,
      `marketValue: {amount: {krw, usd}}`. `amount` 키를 바로 찾으면 항상 0 이 나온다 (겪은 버그).
    - `items[]`: `symbol, name, quantity, averagePurchasePrice, marketCountry, currency` (문자열 decimal).
  - `GET /api/v1/buying-power?currency=KRW` (그룹 **`ORDER_INFO`**) → `{currency, cashBuyingPower}`
    - **`currency` 가 필수**다. 빠뜨리면 400 `invalid-request`. **`symbol` 파라미터는 없다**
      (초기 구현이 `symbol` 을 넘겨 항상 400 이었다 — 회귀 테스트 있음).
  - `GET /api/v1/sellable-quantity?symbol=` (그룹 `ORDER_INFO`) → `{sellableQuantity}`
    - 미수·결제 미도래 물량을 뺀 실제 처분 가능분. 매도 수량은 이 값으로 상한을 건다.
  - `POST /api/v1/orders` 의 `orderType` 은 `LIMIT` | `MARKET`.
    **`MARKET` 에 `price` 를 넣으면 400** 이다. 국내 주식은 수량 기반 정수만 허용.
- Rate Limits (초당): `AUTH` 5, `STOCK` 5, `STOCK_ALL` 1, `MARKET_DATA` 15,
  `MARKET_DATA_CHART` 20, `RANKING` 5, `MARKET_INFO` 3, `ASSET` 5, `ORDER` 10,
  `ORDER_HISTORY` 5, `ORDER_INFO` 6, `CONDITIONAL_ORDER` 5. 429 시 `Retry-After` 준수.
  - 스펙 JSON 의 각 엔드포인트 `description` 에 `**Rate Limits Group**: \`NAME\`` 로 적혀 있다.
- **캔들 `interval` 은 `1m` / `1d` 둘뿐**이다 (스펙 enum 확인). 그 외 주기는 직접 리샘플해야 한다.
- 계좌·주문 API는 `X-Tossinvest-Account` 헤더 필요(현재 미사용 — 스크리너는 시세만 사용).
- REST/WS 모두 **허용 IP 등록** 필요할 수 있음(미등록 IP는 403).
- **2026-09-01 라이브 검증 완료**: 현재 자격증명·IP 로 토큰 발급, `/candles`, `/stocks`,
  `/stocks/all`(KOSPI+KOSDAQ 2,603 종목), `/prices`, `/rankings` 모두 200 정상.
- 캔들 응답은 **최신 봉이 먼저** 오므로 DataFrame 변환 시 반드시 index 오름차순 정렬한다(`candles_to_df` 처리됨).

## 자격증명
`.env` 에 보관하며 **git 커밋 금지**(`.gitignore` 등록됨). 코드에 하드코딩하지 않는다.
`TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET`, 선택적으로 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.
**서버 `.env` 키는 이제 선택 사항**이다 — 없으면 웹은 접속자가 설정 창에 넣은 개인 키로 돈다(아래).

### 접속자별 개인 API 키 (screener/sessions.py)
- **창은 둘로 나뉜다** (2026-09-18): GNB 의 **🔑 API 키 설정** 은 자격증명만
  (`POST /api/session`), **⚙ 설정** 은 스캔 범위·텔레그램·주문·자동매수
  (`POST /api/session/settings`, `_SETTING_KEYS` 만 받고 **자격증명은 받지 않는다**).
  예전에는 설정을 조금 고칠 때도 client_id/secret 을 다시 입력해야 했다.
  - 설정 저장은 저장된 키로 **세션을 다시 연다**(`_open_session`) — 스캔 주기·알림·주문 설정이
    스캐너·봇 생성 시점에 박히기 때문. 실행 중이던 볼린저 봇은 멈추므로 화면에서 다시 시작해야 한다.
  - `SessionSettings.from_body(body, defaults=...)` 는 **누락 키를 defaults 로 채운다**
    (`trading_opt_in`·`auto_buy`·텔레그램도 포함 — 예전엔 이 셋만 False/빈값으로 떨어져
    키만 다시 넣으면 설정이 초기화됐다). 빈 봇 토큰은 저장값 유지.
  - 키가 없는 사용자가 ⚙ 설정을 열면 400 이므로 화면이 자동으로 🔑 창으로 보낸다.
- 대시보드 상단 **🔑 API 키 설정** → `POST /api/session` → `SessionManager.create` 가 그 자격증명 전용
  `TossClient` → `TossDataLoader` → `Screener` → `ScanService`(+`TradingService`) 를 만들고
  세션 쿠키 `sf_session`(HttpOnly·SameSite=Lax)을 발급한다.
- **키는 서버 메모리에만 둔다.** 디스크 기록·응답 반환 금지. 화면에는 `mask()` 한 client_id 만.
  `UserSession.info()` 에 secret 계열 필드를 추가하지 말 것 (회귀 테스트 있음).
- 저장 시 `client.prices(["005930"])` 로 실호출 검증 → 400/401/403 이면 키·허용 IP 문제로 안내.
- 세션마다 스캔 스레드가 1개 뜬다. `--max-sessions`(기본 20) 상한, 유휴 6시간이면 `sweep()` 이 폐기
  (볼린저 봇이 실행 중인 세션은 제외).
- 일봉 캐시(`cache/candles`)는 시세라 전 세션이 공유한다. **토큰 캐시는 `token_cache_path()` 로
  client_id 해시별 분리** (`cache/tokens/<hash>.json`) — 같은 키는 공유해 서로 무효화하지 않고,
  다른 키는 갈라진다. `main.build_loader` 도 같은 함수를 쓴다(예전 `cache/token.json` 은 폐기).
- 실주문은 **서버 `TOSS_TRADING_ENABLED` 와 사용자 `trading_opt_in` 이 둘 다 참일 때만** 켜지고,
  1회 한도는 `min(사용자 값, 서버 TOSS_MAX_ORDER_AMOUNT)`. 이 이중 게이트를 약화시키지 말 것.
- 쿠키가 곧 자격증명 접근권이다. 외부 공개 시 HTTPS 필수, CSRF 는 SameSite=Lax 로 막는다.

## Docker 실행
- `Dockerfile` — `python:3.12-slim`, TZ=`Asia/Seoul` 고정, 비root 실행, 이미지 385MB.
  ENTRYPOINT=`python main.py` 이므로 **compose command 에는 CLI 서브커맨드만** 넣는다 (`["scan","--top","100"]`).
- **UID 매칭 필수**: bind mount 한 `./cache`, `./out` 에 쓰려면 컨테이너 UID = 호스트 UID 여야 한다.
  기본 1000, 다르면 `docker build --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g) .`
  (compose 는 `APP_UID`/`APP_GID` 환경변수로 전달)
- `docker-compose.yml` 서비스
  - `web` — **기본 운영 서비스**. `docker compose up -d` 로 뜨는 건 이것뿐이다.
    커맨드는 `web --port 8083 --top 200 --interval 120 --live` 로, poller 가 하던
    **장중 실시간 돌파 감시(`--live`)를 web 의 `ScanService` 가 대신한다.**
    `--live` 가 빠지면 일봉 종가 기준이라 돌파를 그날 장 마감 후에야 잡는다.
    실측: 200종목 1주기 약 40초 → 120초 주기에 여유가 있고 429 는 나지 않았다(2주기 관찰).
  - `poller` / `screener` / `demo` — `profiles: ["manual"]` 이라 `up` 시 안 뜬다.
    `docker compose --profile manual run --rm <서비스>` (poller 는 `--profile manual up -d poller`) 로 실행.
    **poller 를 기본에서 뺀 이유**: web 이 poller 기능을 포함하는데 둘을 같은 자격증명으로 띄우면
    토큰을 서로 무효화하고 TPS 합산 초과로 429 가 난다. 굳이 함께 돌리려면 자격증명을 분리할 것.
- **`.env` 를 고쳤으면 `restart` 가 아니라 `up -d` 로 컨테이너를 재생성해야 한다.**
  `env_file` 은 **컨테이너 생성 시점에 한 번만** 주입되므로 `docker compose restart` 는
  옛 환경변수를 그대로 재사용한다. 실제로 겪은 일: 키를 갱신했는데도 컨테이너가 계속
  `401 invalid_client` 로 크래시 루프를 돌았다. 확인법:
  `[ "$(docker exec <컨테이너> printenv TOSS_CLIENT_ID)" = "$TOSS_CLIENT_ID" ]`
- **`/healthz` 가 healthy 라고 자격증명이 정상인 건 아니다.** 헬스체크는 합성 데이터로
  탐지 엔진만 검증한다. 키 상태는 로그의 `랭킹 후보 N 종목` / `스캔 완료` 로 판단할 것.
- `.env` 는 이미지에 넣지 않고(`.dockerignore`) `env_file` 로 런타임 주입한다.
- HEALTHCHECK 는 합성 데이터로 탐지 엔진만 검증하므로 API 자격증명 없이도 통과한다.

## 볼린저 밴드 반전 매매 (screener/bollinger.py · bollinger_bot.py)
화면 `/bollinger`, CLI `python main.py bollinger --symbols 005930 --bb-interval 15m`.

### 지표
- 볼린저 밴드: 기간 20 · 표준편차 2 (**모집단 std, ddof=0**). RSI: 기간 14 **Wilder 평활**
  (`ewm(alpha=1/period, adjust=False)`) — TA-Lib `RSI` 와 같은 정의다. TA-Lib 에 의존하지 않는다.
- RSI 0 나눗셈 보정: 하락이 없으면 100, 상승·하락이 모두 없으면(완전 횡보) 50. 워밍업 구간은 NaN 유지.
- **봉 주기**: 토스 캔들 API 는 `1m`/`1d` **둘뿐**이다. 5·15·30·60분봉은 1분봉을 `resample` 해 만든다.
- **정규장 필터 필수**(`regular_session`): 1분봉 응답에는 시간외 단일가 봉이 섞여 온다.
  유동성이 달라 이동평균·표준편차를 왜곡하므로 09:00~15:30 만 남긴다.
  그래서 필요한 정규장 봉 수보다 **넉넉히** 받아야 한다(`_minutes_to_fetch`) — 딱 맞춰 받으면
  장 초반에 "데이터 부족" 이 난다 (실제로 겪은 버그).
- 판정과 차트는 **같은 전처리 함수 `prepare()`** 를 쓴다. 화면과 주문 근거가 갈라지면 안 된다.

### 판정 우선순위 — **손절 > 청산 > 부분익절 > 진입**
하한선을 타고 계속 밀리는 '밴드 라이딩' 구간에서는 매수 조건이 계속 참이라,
손절을 뒤에 두면 물타기로 빠져든다. 이 순서를 바꾸지 말 것.

| 액션 | 조건 | 수량 |
|---|---|---|
| `BUY` | 현재가 ≤ 하한선 **AND** RSI ≤ 30 | 예수금 × `cash_ratio`, 1회 한도 이내 · **지정가**(현재가 +`buy_slippage`) |
| `SELL` | 현재가 ≥ 상한선 **AND** RSI ≥ 70 | 전량 |
| `TAKE_PROFIT` | 보유 중 중앙선 도달 (매수가 위, 1회만) | `mid_take_ratio` (기본 50%) |
| `STOP` | 매수가 대비 −`stop_loss_pct` (기본 5%) | 전량 (조건 불문) |

### 안전장치 — 약화시키지 말 것
- **3중 게이트**: 서버 `TOSS_TRADING_ENABLED` + 사용자 `trading_opt_in` + 봇 `live` 스위치.
  `BollingerBot.live` 는 `self._live and self.trading.enabled` 라 하나만 꺼져도 모의로 떨어진다.
  `POST /api/bollinger/live` 는 `confirm="실행"` 을 요구한다.
- **정규장 밖에서는 주문하지 않는다** (`market_is_open`, 09:00~15:30 평일). 판정·표시만 한다.
- **감시 목록에 있는 국내 종목만** 관리한다. 계좌의 다른 보유분은 세지도 팔지도 않는다.
- **감시 목록 삭제는 보유 중(진행 중) 종목을 거부한다** (`DELETE /api/bollinger/symbols`, 409).
  감시에서 빼면 봇이 그 물량의 손절·익절을 관리하지 못하기 때문. 화면(감시 그리드 맨 왼쪽 ✕)도
  보유 종목엔 버튼 대신 자물쇠만 보여준다. 청산(수량 0) 후에는 삭제할 수 있다.
  삭제 시 **`state.decisions`·`positions` 에서도 즉시 뺀다** — 안 그러면 다음 판정 주기까지 화면에
  남아 "지금 점검" 을 눌러야 사라진다(겪은 문제). 화면 `removeSymbol` 은 **하나로 통합**(그리드 ✕ +
  종목추가 모달 목록 공용, `DELETE` 사용 — 예전엔 모달용이 `config` 전체 교체라 보유 거부·즉시 반영이
  안 됐고 함수가 중복 정의됐다). 추가·삭제 뒤엔 `burstRefresh`(0·0.7·1.6·3.2·6초)로 run 반영을 따라잡는다.
- **`manage_existing=False` (기본)**: 봇이 사지 않은 기존 보유분은 **청산하지 않는다.**
  이 게이트가 없으면 감시 목록에 장기 보유 종목을 넣는 순간 첫 주기에 털린다
  (실제 계좌로 검증 중 삼성전자·SK하이닉스 물량이 즉시 부분익절 대상이 됐다).
  화면은 `state.adopted` 로 경고 배너를 띄운다.
- 1회 주문 금액 상한은 `TradingService.max_order_amount`. **매도에는 금액 한도를 적용하지 않는다**
  — 손절 청산이 한도에 걸려 막히면 위험이 오히려 커진다.
- 멱등키 `bl-{종목}-{B|S}-{MMDDHHMM}-{수량}`. **분 단위 시각을 넣는 이유**: 멱등키는 10분간
  유효해서, 시각이 없으면 같은 수량의 정상 재진입까지 막힌다.
- 보유 수량의 진실은 **항상 토스 계좌 잔고**다. 로컬 상태(매수가·부분익절 여부)는 메모일 뿐이라
  재시작하면 계좌를 다시 읽어 복구한다.
- 모든 판정·주문은 `out/bollinger.log`(텍스트) + `out/orders.jsonl`(`event: bollinger_order`) 에 남는다.
  텔레그램을 설정했으면 체결·손절 시 알림이 간다 (`TelegramNotifier.send_text`).

### 주문 방식 — 매수 지정가 / 매도 시장가 (2026-09-18 변경)
- **매수는 지정가**(`TradingService.limit_buy`) — 시장가 슬리피지로 현재가와 크게 벌어지는 것을 막는다.
  지정가 = `round_tick(현재가 × (1 + buy_slippage), up)`(기본 +0.5%). 이 값 **이하로만** 체결된다.
  `buy_cancel_after`(기본 180초) 안에 다 안 채워지면 **미체결분을 백그라운드로 취소(회수)** 한다
  (`_cancel_unfilled` — 확률 낮은 주문을 오래 두지 않는다. 이미 체결/취소된 주문은 손대지 않는다).
  멱등키는 지정가 태그 `blL-...`(시장가 `bl-...` 와 구분).
- **매도(전량·부분익절·손절)는 시장가**(`market_order`) — 확실한 청산을 위해. **`MARKET` 에 `price` 를
  넣으면 400.** 매도에는 금액 한도를 적용하지 않는다.
- **한계**: 급락 중엔 지정가 매수가 미체결로 진입을 놓칠 수 있다(반전 전략상 감수). `buy_slippage=0` 이면
  현재가 지정가라 더 자주 놓친다. 설정 창(자금 운용)에서 `매수 지정가 상단%`·`미체결 회수(초)` 조절.

### 한계 — 문서에 반드시 남길 것
- 세력추정매매와 달리 **이 전략은 이 저장소에서 백테스트로 검증된 수치가 없다.**
  승률·기대값을 아직 모른다. 소액·모의로 관찰한 뒤 쓸 것.
- 매수 지정가는 슬리피지를 제한하지만 급락 중엔 미체결로 진입을 놓칠 수 있다.
  매도(손절·익절)는 시장가라 호가가 얇으면 슬리피지가 크다.
- 추세장에서는 밴드를 타고 밀려 손절이 반복된다 (전략 자체의 성질).

## 로그인 · 설정 서버 저장 (screener/store.py)
- 대시보드는 **아이디/비밀번호 로그인**을 거친다. 저장소는 SQLite `cache/app.db`
  (`APP_DB_PATH` 로 변경). `main.py web` 이 `Store` 를 만들어 `create_app(store=...)` 로 넘긴다.
  `--no-login` 이면 예전처럼 인증 없이 뜬다.
- 테이블: `users(username, password_hash, …)` · `settings(username, payload, …)` ·
  `auth_tokens(token_hash, username, expires_at)` · `bollinger_configs(owner, payload, …)`.
- **볼린저 설정 영속화**: `POST /api/bollinger/config`·`/symbols` 가 저장하고(`persisted` 응답),
  세션 생성(`_restore`, `POST /api/session`)과 서버 시작 시 `_bb_load` 로 되살린다.
  owner 는 `user:<아이디>`(개인 세션 봇) 또는 `server`(서버 기본 봇). 로그인을 안 쓰면 저장하지 않는다.
  **`manage_existing` 은 저장하지 않는다**(`BB_NOT_PERSISTED`) — 기존 보유분 청산 스위치라
  재시작 뒤에는 항상 False 로 돌아가야 한다. 복구해도 봇은 **자동 시작하지 않는다**.
  `DELETE /api/session`(연결 해제)은 API 키 설정만 지우고 볼린저 설정은 남긴다.
- **비밀번호는 scrypt 해시로만** 저장한다(복호화 불가). **로그인 토큰도 sha256 해시만** 저장해
  DB 가 새도 쿠키를 만들 수 없다. 쿠키 `sf_auth`(HttpOnly·SameSite=Lax·30일).
- 설정(`SessionSettings` 전체)은 로그인 사용자별로 저장된다. `SECRET_FIELDS`
  (`client_secret`, `telegram_bot_token`)는 **Fernet 으로 암호화**해 넣는다.
  키는 `APP_SECRET_KEY`(기본 `jogiclub`) → scrypt 로 늘려 파생. **키를 바꾸면 저장값을 못 푼다**
  (복호화 실패 시 빈 값으로 돌려 다시 입력받는다 — 회귀 테스트 있음).
  → 즉 **DB 파일만 유출되면 안전하지만 `.env` 까지 함께 새면 풀린다.** 백업 시 둘을 같이 두지 말 것.
- 인증 게이트는 `web.py` 의 `require_login` 미들웨어다. 공개 경로는 `PUBLIC_PATHS`
  (`/login`, `/api/login`, `/api/signup`, `/api/auth/options`, `/healthz`, `/favicon.ico`)와 `/static/**` 뿐.
  HTML GET 은 `/login` 으로 302, 그 외에는 401 JSON.
- 로그인하면 저장된 설정으로 **토스 세션을 자동 복구**한다(`_restore`). 복구 실패는 60초 쿨다운을
  둬 매 요청마다 토스 API 를 두드리지 않는다. `user_sids` 가 사용자→sid 를 들고 있어
  쿠키를 잃어도(서버 재시작·다른 브라우저) 로그인만으로 이어진다.
- `POST /api/session` 은 저장까지 하고, `DELETE /api/session` 은 **저장된 설정도 지운다**.
  로그아웃(`POST /api/logout`)은 토큰만 버리고 설정은 남긴다.
- 계정 관리 CLI: `python main.py adduser <아이디>` (없으면 생성, 있으면 비밀번호 변경).
  비밀번호는 `getpass` 로 받는다 — `--password` 는 셸 기록에 남으니 피할 것.
  첫 실행 시 `.env` 의 `APP_ADMIN_USER`/`APP_ADMIN_PASSWORD` 가 있으면 그 계정을 만든다.
  **비밀번호 기본값을 코드에 넣지 말 것** (`AppConfig.admin_password` 기본값은 빈 문자열).
- 의존성 `cryptography` 추가됨 (requirements.txt).

### 다중 사용자 (2026-09-17) — 약화시키지 말 것
- **관리자 / 일반 사용자 구분** (`users.is_admin`, 예전 DB 는 시작 시 열 자동 추가).
  서버 `.env` 계좌를 쓰는 **주문(`/api/trading/*`)·서버 볼린저 봇은 관리자만** 쓴다
  (`_server_trading`/`_server_bot`). 일반 사용자는 자기 토스 키 세션으로만 주문·봇을 쓴다.
  시세 화면(세력추정 스캔·수급)은 서버 키로 모든 로그인 사용자가 공유한다.
  관리자 지정: `APP_ADMIN_USER` 계정이 자동 관리자(관리자가 하나도 없으면 승격),
  또는 `python main.py adduser <아이디> --admin`. 마지막 관리자는 탈퇴할 수 없다.
- **세션-계정 결속**: `UserSession.owner` = 로그인 아이디. `_session()` 은 쿠키의 세션이라도
  owner 가 현재 로그인 계정과 다르면 무시한다. 예전에는 같은 브라우저에서 A 로그아웃 → B 로그인 시
  `sf_session` 쿠키가 남아 **B 가 A 의 키·계좌로 주문할 수 있었다** (회귀 테스트 있음).
  로그아웃·로그인·가입 응답은 `sf_session` 쿠키를 지운다(복구된 내 세션이면 새로 준다).
  세션 재설정·연결 해제도 남의 세션은 drop 하지 않는다(`_drop_own_cookie_session`).
- **회원가입** `POST /api/signup` — `APP_SIGNUP`(기본 true) / `APP_SIGNUP_CODE`(초대 코드, 외부 공개 시 권장).
  가입자는 항상 일반 사용자. 아이디 규칙 `[A-Za-z0-9][A-Za-z0-9_.-]{2,31}`, 비밀번호 8자 이상.
  로그인 화면은 `GET /api/auth/options` 로 가입 탭 노출 여부를 정한다 (`?signup` 딥링크).
- **내 계정 창**(GNB 아이디 클릭): `POST /api/account/password`(변경 시 다른 기기 토큰 전부 폐기),
  `DELETE /api/account`(비밀번호 확인 → 세션 정지 + users·settings·auth_tokens·bollinger_configs 삭제).
- **로그인 시도 제한**: (IP, 아이디)당 5분에 5회 실패하면 429 (메모리 보관, 재시작 시 초기화).
  가입 초대 코드 오입력도 같은 방식으로 제한한다.
- **주문·볼린저 로그 사용자별 분리**: 로그인 세션은 `out/users/<아이디>/orders.jsonl`·`bollinger.log`
  (`SessionManager.log_paths`). 서버 기본 봇·로그인 미사용 서버는 기존 `out/` 그대로.
- **자동매매 봇이 도는 세션은 유휴 6시간이 지나도 폐기하지 않는다** (`UserSession.idle`).
  예전에는 화면을 안 보면 봇이 조용히 멈췄다.
- `--max-sessions` 기본 8 → **20** (여러 사용자 동시 접속).
- 시작 시 `APP_SECRET_KEY` 가 기본값(`jogiclub`/`change-me`)이면 경고를 찍는다.
  **운영 DB 가 이미 기본 키로 암호화돼 있으므로 바꾸면 모든 사용자가 키를 다시 입력해야 한다.**

## 텔레그램 알림
- 세션 설정 창(⚙ 설정)의 **Bot Token 은 BotFather 문자열 전체**(`123456789:AA…`, 콜론 포함),
  **Chat ID 는 대화방 숫자 ID** 다. 실제로 토큰을 콜론에서 쪼개 뒷부분만 Chat ID 칸에 넣고
  Bot Token 을 비워 두는 실수가 있었다 — 이때 `sessions.create` 는 두 값이 **모두** 있어야
  `TelegramNotifier` 를 붙이므로 알림이 조용히 꺼진다.
- 그래서 설정 창에 **"테스트 전송"** 버튼을 뒀다: `POST /api/session/telegram/test`
  → `TelegramNotifier.check()` 가 형식 검증 후 실제로 1건 보내고, 실패하면
  `TelegramError` 한국어 안내(401=토큰 폐기, `chat not found`=챗ID·선대화 필요 등)를 400 으로 준다.
  입력 칸이 비어 있으면 **저장된 세션 값**으로 확인한다(저장 후 토큰 칸은 비워지기 때문).
- 응답에 토큰·챗ID 를 절대 담지 않는다. 성공 시 대화방 표시명만 돌려준다.
- 시그널 알림은 **새 시그널이 있을 때만** 나간다(`TelegramNotifier.send` 는 빈 목록이면 무시,
  `DedupeNotifier` 가 중복 제거). 조용한 게 정상일 수 있다.
- 세력추정매매 알림(`_format`)은 **토스 종목 페이지 링크**를 raw URL 로 함께 보낸다
  (미리보기는 `disable_web_page_preview=True` 로 끔). HTML 모드라 종목명은 `html.escape` 한다.
- Chat ID 확인: 봇에게 먼저 메시지를 보낸 뒤 `https://api.telegram.org/bot<토큰>/getUpdates`.

## 이메일 알림 (screener/notifier.py · EmailNotifier)
- **세력추정매매에 새 시그널이 올라오면 이메일 발송.** 텔레그램과 같은 지점(`ScanService.notifier`)에서
  나가고 `DedupeNotifier` 로 중복 제거, 여러 채널은 `MultiNotifier` 로 묶는다.
- **발신 계정은 서버 공용**(`.env` 의 `MAIL_SMTP_*`), **수신 주소만 사용자가 설정 창에 입력**한다
  (⚙ 설정 → 이메일 알림, 여러 개는 콤마·줄바꿈). `SessionSettings.alert_emails` 로 저장(암호화 아님).
  `parse_emails` 로 형식 검증·소문자화·중복 제거(최대 20개). 수신자끼리 주소가 안 보이게 **Bcc** 로 보낸다.
- SMTP 설정은 **wani_v2 `application/libraries/Message_send_service.php` 의 `send_email` 과 동일**:
  `smtp.gmail.com:587` TLS, 계정 `hello@webhows.com` + **Gmail 앱 비밀번호**. `AppConfig.mail_*`
  (`.env` 우선). **비밀번호는 코드 fallback 없이 `MAIL_SMTP_PASS` 에서만** 읽는다.
  `.env` 는 값에 공백이 있어도(`앱 비밀번호`, `왔니 주식`) 따옴표 없이 그대로 둔다.
- 서버에 발신 계정이 없으면(`AppConfig.mail_enabled=False`) 설정 창의 이메일 섹션이 숨는다.
  `SessionManager(mail=...)` 로 발신 계정을 넘긴다(`main.py`). `mail_enabled` 를 `/api/session` 이 내려준다.
- 설정 창 **"테스트 전송"** → `POST /api/session/email/test` (입력 비면 저장값). `EmailNotifier.check()` 가
  1건 실제 발송, 실패 시 `EmailError` 한국어 안내(401=앱 비밀번호 확인 등)를 400 으로. 응답에 수신자 전체를 담지 않는다.
- 메일은 HTML(종목·점수·매수/손절/가격 표) + 토스 링크. 종목명은 `html.escape`.

## 공통 GNB · 정적 파일 (2026-09-18 Tailwind + AG Grid 전환)
- `<head>` 스크립트 순서(모든 화면 동일): **tailwind CDN → `/static/ui.js` → ag-grid CDN → `/static/common.css`
  → gnb.js → session.js → (dashboard·supply 만) stockmenu.js.** ui.js 가 tailwind.config 를 세팅하므로
  tailwind 다음에 와야 한다.
- **색은 CSS 변수를 가리키는 Tailwind 토큰**(`bg-s0/s1/s2`, `border-line`, `text-txt/txt2/txt3`,
  `text-up/down/good/warn/bad/accent`)이라 라이트/다크가 클래스 교체 없이 따라온다. 그래서 `dark:` 변형은
  쓰지 않는다. **CSS 변수 색이라 투명도 수식(`bg-good/12`)은 못 쓴다** — 옅은 배경은 `.tint-*` 유틸.
- 목록은 `Grid.create(el, {columns, rows, onRow, options})` 로 만든다. 컨테이너에 높이를 주고
  (`h-[min(60vh,560px)]`), 폴링 갱신은 `setGridOption("rowData", …)`, 탭 전환으로 목록이 사라지면
  `destroy()` (매 주기 새로 만들지 말 것). 선택 행 강조는 `rowClassRules`+`redrawRows()`.
  종목코드·종목명 셀은 `StockMenu.link` cellRenderer, 행 클릭은 `StockMenu.fromEvent` 가 true 면 상세 열기를 건너뛴다.
  헤더 설명은 `headerTooltip`. **cellRenderer 가 참조하는 헬퍼 함수는 반드시 `<script>` 스코프에 정의**할 것
  (전환 중 `scoreCell`·`scoreBar`·`gradeBadge` 가 유실돼 그리드가 빈 셀로 나온 적 있다 — 회귀 주의).
- 세 화면은 `/static/common.css`(테마 토큰) + `/static/ui.js`(Tailwind·그리드) + `/static/gnb.js`(GNB·테마 토글)
  + `/static/session.js`(API 키·설정 모달) + `/static/stockmenu.js`(종목 메뉴) 를 공유한다. `web.py` 가 `/static` 을 mount 한다.
- 테마 토글과 ⚙설정 버튼은 **GNB 로 옮겼다.** 페이지는 `<div data-gnb="supply|screener|bollinger">` 자리표시자와
  `[data-gnb-action]` 버튼만 두면 `gnb.js` 가 GNB 를 그리고 버튼을 오른쪽으로 옮긴다.
- 테마가 바뀌면 `themechange` 이벤트가 뜬다 — 인라인 SVG 차트는 이걸 듣고 다시 그린다.
- 설정 저장·해제 후에는 `sessionchange` 이벤트가 뜬다.
- **테마 토큰을 화면 HTML 에 다시 정의하지 말 것** — `common.css` 한 곳에만 둔다.

## 웹 대시보드
- `python main.py web --port 8083` / 컨테이너는 compose `web` 서비스 (8083:8083).
- `ScanService` 가 `interval` 초마다 스캔해 결과를 메모리에 보관하고, 브라우저는 `/api/state` 를 5초마다 폴링한다.
  `scan_once()` 는 논블로킹 락으로 중복 실행을 막는다(진행 중이면 현재 상태 반환, `POST /api/scan` 은 409).
- 프런트엔드는 **Tailwind CSS(Play CDN) + AG Grid(CDN, 33.3.2)** 를 쓴다 (2026-09-18 도입).
  종목 목록은 전부 AG Grid, 화면 스타일은 전부 Tailwind 유틸리티. 차트는 여전히 인라인 SVG 직접 생성.
  **외부 CDN 이 필요하다** — 폐쇄망에 올리려면 `ui.js` 위 두 CDN 을 자체 호스팅으로 바꿔야 한다.
- 색상: 상승/양봉 `--up`(적색), 하락/음봉 `--down`(청색) — 한국 시장 관례. 라이트/다크 각각
  `#e34948/#2a78d6`, `#e66767/#3987e5` 로 CVD·대비 검증 통과(all-pairs). 기준봉은 `--warning` 띠로 강조.
- 라이트/다크 모두 지원: `:root` 기본 + `prefers-color-scheme` + `[data-theme]` 토글(localStorage 저장),
  `?theme=dark|light` 딥링크(스크린샷·임베드용).
- FastAPI 는 deprecated `on_event` 대신 `lifespan` 컨텍스트를 쓴다.
- `create_app(loader=None, ...)` 이 허용된다 — 서버 키가 없으면 `/api/state` 가
  `configured:false` 를 내려주고 화면은 설정 안내 배너 + 모달을 띄운다.
  시그널·차트·스캔 API 는 이때 503 이다.
- 모든 데이터 API 는 요청 쿠키로 세션을 먼저 찾고(`_service(request)` / `_bot(request)`),
  없으면 서버 기본 스캐너·봇을 쓴다. 새 API 를 추가할 때 전역 `service`/`bollinger` 를 직접 참조하지 말 것.
- 수급 API: `GET /api/supply/state` · `POST /api/supply/scan` · `GET /api/supply/chart/{symbol}`
  (`_supply(request)` 로 세션 > 서버 순). 서버에 수급 서비스가 없으면 state 는 `configured:false`, 나머지는 503.
- 볼린저 API: `GET /api/bollinger/state` · `POST /api/bollinger/{config,symbols,live,start,stop,run}` ·
  `DELETE /api/bollinger/symbols`(감시 목록에서 종목 1개 제거 — **보유 중이면 409 로 거부**) ·
  `GET /api/bollinger/chart/{symbol}`. 봇은 세션마다 하나씩 만들되 **자동 시작하지 않는다**
  (주문을 내는 루프라 사용자가 명시적으로 시작해야 한다).

## 실행 환경
- WSL2(Linux)에서 개발. 기존 `.venv/` 는 **Windows용 venv**라 Linux에서 못 씀 →
  Linux 실행은 `.venv-linux/` 사용: `.venv-linux/bin/python main.py demo`
- 의존성: `pandas`, `requests`, `python-dotenv`, `fastapi`, `uvicorn[standard]`,
  (테스트) `pytest`, `httpx`(TestClient)

## 매수 유의 종목 필터
- `GET /api/v1/stocks/{symbol}/warnings` 응답은 **배열**이며 활성 항목이 없으면 빈 배열.
  유형: `LIQUIDATION_TRADING` `OVERHEATED` `INVESTMENT_WARNING` `INVESTMENT_RISK`
  `VI_STATIC` `VI_DYNAMIC` `VI_STATIC_AND_DYNAMIC` `STOCK_WARRANTS`.
- STOCK 그룹은 5 TPS 이므로 **전수 조회 금지**. 시그널이 확정된 종목만 조회한다(`Screener._filter_warned`).
- VI 는 차단하지 않고 `Signal.notes` 로 표시만 한다(`notice_warnings`). 차단 목록은 `blocking_warnings`.
- 종목 메타 필터(`_passes_meta`)는 유니버스 경로와 `--top`/`--symbols` 경로 **양쪽 모두**에 적용된다.
  (초기 구현은 symbols 경로에서 필터가 통째로 누락돼 있었음 — 회귀 테스트 있음)

## 백테스트
- `Backtester` 는 `PatternDetector.detect` 를 그대로 호출한다 — 라이브와 판정 로직이 갈라지지 않게 할 것.
- 돌파 조건을 벡터 마스크로 미리 걸러 sparse 한 위치에서만 `detect` 를 호출한다(O(n²) 회피).
- **데이터 끝에 걸린 미결 거래는 제외**한다(`n = len(data) - max_hold`). 포함하면 성과가 왜곡된다.
- 알려진 낙관 편향: 진입 판정에 **당일 전체 거래량**을 쓴다. 실제 장중 돌파 시점엔 아직 그만큼
  누적되지 않았을 수 있다. 결과를 그대로 실전 기대치로 읽지 말 것.
- 3년 전 종목(2,603개) 캔들 캐시는 약 400MB 를 쓴다.

## 백테스트 결과 (2026-09-01, 3년 · 2,563종목 · 208만 봉)
청산 규칙: 1R 절반 익절 → 본전 스톱, 최종 3R, 시간손절 15일, 슬리피지 0.3% + 비용 0.15%

| 프로파일 | 거래수 | 승률 | 평균 R | 수익팩터 | 총 R | 최대낙폭 | 최대연속손실 |
|---|---|---|---|---|---|---|---|
| `spec` (원본 스펙) | 15,099 | 48.0% | **-0.084** | 0.84 | -1,272R | 1,363R | 24 |
| `tuned` (운영 기본) | 1,230 | 55.2% | **+0.076** | 1.17 | +94R | 40.5R | 7 |

**원본 스펙 그대로는 손실이다.** `tuned` 프로파일(`StrategyConfig.tuned()`)이 흑자로 돌린 세 가지:
- 윗꼬리 45% 이상 (35~45% 구간 평균 -0.062R)
- 기준봉 거래량 = 20일 최고 **AND** 20일 평균의 20배 이상 (`vol_condition_mode="and"`)
  — ×8 미만 구간 -0.125R, ×50 이상 +0.102R 로 단조 증가
- 조정 기간 1~3일 (7~10일 구간 -0.169R)

CLI `--profile tuned|spec` (기본 `tuned`). 파라미터를 바꾸면 **반드시 백테스트를 다시 돌려**
이 표를 갱신할 것. 평균 +0.076R 은 얇은 우위라 슬리피지 가정이 나빠지면 사라질 수 있다.

## 시장 국면(regime) 필터
- 지수 캔들: `GET /api/v1/market-indicators/{symbol}/candles?interval=1d&count<=200` (그룹 `MARKET_INDICATOR_CHART`, 5 TPS).
  심볼 카탈로그 8종(`KOSPI`, `KOSDAQ`, `KR_BOND_*` …), 카탈로그 밖 심볼은 400 `unsupported-symbol`.
  개별 종목 캔들(`/api/v1/candles`)과 **다른 엔드포인트**다.
- `screener/regime.py` 의 `MarketRegime`: 지수 종가 > N일 이동평균이면 매매 우호 국면.
- 실측(tuned 프로파일 1,230건 대상, 진입일 기준 사후 필터):

  | 기준 | 통과 건수 | 승률 | 평균 R | 수익팩터 | MDD | 제외 구간 평균 |
  |---|---|---|---|---|---|---|
  | 없음 | 1,230 | 55.2% | +0.076R | 1.17 | 40.5R | – |
  | KOSDAQ MA5 | 706 | 59.6% | +0.176R | 1.45 | 18.4R | -0.058R |
  | KOSDAQ MA10 | 736 | 60.3% | +0.206R | 1.54 | 15.9R | -0.117R |
  | **KOSDAQ MA20 (기본)** | 734 | 59.0% | +0.173R | 1.44 | 15.6R | -0.067R |
  | KOSDAQ MA60 | 681 | 59.8% | +0.202R | 1.53 | 16.3R | -0.080R |
  | KOSPI MA20 | 792 | 58.3% | +0.145R | 1.36 | 23.3R | -0.047R |

  MA 5/10/20/60 이 모두 같은 방향 → 특정 값에 과최적화된 결과가 아니다. 기본은 통상적인 MA20.
- 기본은 `regime_blocks=False` (점수 -10점 + 배너 경고만). `True` 로 두면 이탈 국면에서 시그널을 아예 버린다.

## 손절 방식 비교 (3년, tuned 프로파일)
| `stop_mode` | 거래수 | 승률 | 평균 R | 수익팩터 | MDD | 평균보유일 |
|---|---|---|---|---|---|---|
| **`base_low` 기준봉 저가 (기본)** | 1,230 | 55.2% | **+0.076R** | 1.17 | 40.5R | 6.1일 |
| `prev_low` 전일 저가 | 1,248 | 49.8% | -0.080R | 0.85 | 132.6R | 2.2일 |
| `pct` 고정 5% | 1,243 | 50.8% | -0.023R | 0.95 | 80.9R | 3.9일 |

타이트한 손절(전일 저가)은 평균 보유 2.2일로 **휩쏘에 털린다**. 기준봉 저가가 유일한 흑자 방식이므로
`stop_mode` 기본값을 바꾸지 말 것.

## 진입 점수 (100점)
`screener/scoring.py`. **배점은 백테스트 실측 평균 R 에서 역산했다** — 임의 가중치가 아니다.
표본: tuned + KOSDAQ MA20 통과 734건 (전체 평균 +0.173R).

| 요인 | 배점 | 근거 (구간별 평균 R) |
|---|---|---|
| 손절폭 | 25 | ~8% +0.214 · 8~12% +0.158 · 12~18% +0.052 · 18%~ **-0.152** |
| 기준봉 거래량 | 20 | 20~30배 +0.113 · 30~50배 +0.214 · 50~100배 +0.228 · 100배~ +0.152 |
| 조정 거래량비 | 20 | ~0.10 +0.111 · **0.10~0.20 +0.315** · 0.20~0.35 +0.148 · 0.35~ +0.086 |
| 조정 기간 | 15 | 2일 +0.285 · 3일 +0.081 |
| 윗꼬리 | 10 | 45~55% +0.135 · **55~65% +0.226** · 65~80% +0.153 · 80%~ +0.186 |
| 시장 국면 | 10 | 통과 +0.173 · 이탈 -0.117 (국면 정보 없으면 5점 중립) |

등급별 실측 성과(같은 표본을 사후 채점 — **in-sample** 이라 그대로 미래 기대치로 읽으면 안 된다):

| 등급 | 거래수 | 승률 | 평균 R |
|---|---|---|---|
| A+ (85~) | 152 | 69.1% | **+0.412R** |
| A (75~84) | 370 | 60.0% | +0.165R |
| B (65~74) | 447 | 50.1% | -0.048R |
| C (50~64) | 218 | 51.4% | +0.017R |
| D (~49) | 42 | 38.1% | -0.260R |

→ **실전 컷오프 75점**(`scoring.ENTRY_CUTOFF`). 75점 이상만 매매 시 523건·승률 62.5%·평균 +0.234R.
65~74(B) 구간은 승률 50%로 비용을 못 넘기므로 등급 문구도 "관망"이다.
**거래량비·거래량배수·윗꼬리는 단조 증가가 아니라 중간 구간이 최적**이므로 "클수록 좋다"로 바꾸지 말 것.
파라미터나 청산 규칙을 바꾸면 배점 근거가 무너지니 백테스트를 다시 돌려 이 표를 갱신한다.

## 대시보드 가이드 탭
- `/api/guide` 가 `screener/evidence.py` + `scoring.py` 를 그대로 직렬화해 내려준다.
  **숫자를 HTML 에 하드코딩하지 말 것** — 백테스트를 다시 돌리면 `evidence.py` 한 곳만 고치면 된다.
- 구성: ①전략 3단계 ②배점과 근거 ③컷오프 75점과 등급별 실측 ④포지션 사이징 ⑤청산 규칙
  ⑥검증 기록(프로파일·손절방식·국면) ⑦한계.
- 표 헤더 툴팁(`TIPS`), 시그널 상세의 포지션 계산기(계좌 잔고·허용 손실은 localStorage 저장),
  URL 딥링크 `?tab=`·`?theme=` 지원.
- `hidden` 속성은 `display:flex` 에 밀리므로 `.regime[hidden] { display:none }` 같은 가드를 반드시 둘 것.

## 주문 실행 (screener/trading.py)
- **실제 돈이 나가는 코드다.** 아래 안전장치를 절대 약화시키지 말 것.
  - `TOSS_TRADING_ENABLED=true` 아니면 실주문 거부 (기본 OFF), `--no-trading` 이 환경변수보다 우선
  - `TOSS_MAX_ORDER_AMOUNT` 금액 상한 (기본 100만원)
  - HTTP 계층에서 `dry_run=false` + `confirm="실행"` 둘 다 요구
  - 멱등키 `clientOrderId` = `sf-{종목}-{MMDD}-{매수가}-{수량}` (10분간 유효)
  - `TossClient.post` 는 **재시도하지 않는다** — 중복 체결 방지. GET 만 재시도한다.
- 주문 흐름: 지정가 매수 → 백그라운드 체결 폴링 → 체결 수량만큼 OCO 조건주문.
- **OCO leg 필수 필드**: `orderSide`("SELL"), `triggerPrice`, `orderPrice`.
  `orderSide` 를 빠뜨리면 400 이다 (초기 구현에서 누락 → 회귀 테스트 있음).
  규칙: `first`(익절) 감시가 > 현재가 > `second`(손절) 감시가, 둘 다 SELL, `orderType=LIMIT` 만 지원.
- **호가 단위**(`round_tick`): 2천 미만 1원 · ~5천 5원 · ~2만 10원 · ~5만 50원 · ~20만 100원 ·
  ~50만 500원 · 그 이상 1,000원. 어긋나면 `invalid-tick-size`.
- 계좌 컨텍스트 API 는 `X-Tossinvest-Account: {accountSeq}` 헤더 필수 (`TossClient._headers`).
- 모든 주문 이벤트는 `out/orders.jsonl` 에 적재. OCO 실패는 `oco_failed` 로 남으므로 운영 시 감시할 것.

## 종목 링크 · 종목 메뉴
- 토스증권 종목 페이지: `https://www.tossinvest.com/stocks/{6자리코드}` (주문 화면은 `/order` 접미사).
- 세력추정매매 표(시그널·돌파대기)의 **종목코드·종목명을 누르면 종목 메뉴 모달**(`#stock-menu`)이 뜬다.
  - **토스로 보기** — 위 URL 을 새 창으로 연다.
  - **반전 매매에 종목추가** — `POST /api/bollinger/symbols {symbol}` 로 볼린저 감시 목록에 1개를 덧붙인다.
    서버에서 합치므로 `/api/bollinger/config` 처럼 다른 설정을 덮어쓰지 않는다. 이미 있으면
    `added:false`, 6자리 숫자가 아니면 400, 봇이 없으면(키 미설정) 503. **봇을 시작하지는 않는다.**
    추가에 성공하면 **즉시 `run_once` 를 백그라운드로 1회 돌린다** — 안 그러면 반전매매 화면에
    새 종목이 봇 루프 다음 주기(최대 `poll_interval`)까지 안 보인다(겪은 문제). 반전매매 화면도
    감시 목록에 있는데 판정 안 된 종목이 있으면 `maybeKickRun` 이 `/api/bollinger/run` 을 1회 쏜다.
- 셀은 `<button class="stock">` 이고, `bindRows` 가 클릭 대상이 이 버튼이면 메뉴만 열고
  상세 패널은 열지 않는다. 행의 다른 칸을 누르면 기존대로 상세가 열린다.
  상세 패널의 "토스증권에서 열기" 버튼은 그대로 직접 링크다.
- **볼린저 설정(감시 목록 포함)은 DB 에 저장된다** (`bollinger_configs` 테이블, 아래 로그인 절 참고).
- 종목 메뉴는 공통 모듈 `static/stockmenu.js` 다 (세력추정·수급포착 화면 공용). 페이지는
  `StockMenu.link(r, text, cls)` 로 셀을 만들고 행 클릭에서 `StockMenu.fromEvent(ev, 코드, 이름)` 가
  true 면 상세 열기를 건너뛴다. 스타일(`button.stock`·`.menu-list`)은 `common.css` 에 있다.
- 공통 모달 `.modal-back` 은 `z-index: 60` 이다. GNB 가 `50`(sticky)이라 그보다 낮으면
  모달 윗부분이 GNB 에 가려진다 (⚙설정 모달도 같은 문제가 있었다).

## 수급 포착 매매 (screener/supply.py)
화면 `/supply`, CLI `python main.py supply`. **주문을 내지 않는 관찰용**이며 **백테스트 미검증**이다
(점수는 경험칙 배점 — 세력추정매매처럼 실측 근거가 있는 것처럼 문구를 바꾸지 말 것).

### 판정 흐름
| 단계 | 조건 (`SupplyConfig`) |
|---|---|
| 후보 | `rankings MARKET_TRADING_AMOUNT duration=1d` 상위 100 → `stocks` 메타로 ETF(`securityType!=STOCK`)·우선주·거래정지 제외, 거래대금 ≥ 100억, 주가 ≥ 1,000원 |
| 1차 급증 | `min(경과분, 60)` 시점까지 오늘 누적 거래량 ÷ **과거 5영업일 같은 분까지 누적 평균** ≥ 2.0 (못 넘으면 목록에서 제외) |
| 관찰 | 장 시작 30분 전 (`confirm_minutes`) |
| 이탈 | 현재가 < 손절선, 또는 (최근 30분 거래량 < 직전 30분 × 0.5 AND 매수 비중 < 45%) |
| 지속 | 매수 비중 ≥ 55% AND 누적 매수세(CVD) 상승 AND VWAP 위 AND 손절선 위 AND 거래량 유지 |
| 약화 | 그 밖 |
| **진입** | (별도 플래그, status 아님) **지속 AND 갭 과열 아님 AND 고점권 아님 AND 종합 점수 ≥ `entry_score_min`(70)** |

- **'진입'(`decide_entry`, 들어갈 만한 것)** — status 는 그대로 두고 **별도 플래그**로 여러 지표를 종합한다:
  수급 지속 + 갭 과열 아님 + 고점권 아님 + 종합 점수 ≥ `entry_score_min`(기본 70).
  화면은 🔥 배지·필터·근거로 강조한다(텔레그램 발송 없음). `entry_reasons` 에 근거를 담는다.
  `_enrich` 에서 투자자(+5점) 반영 후 `rescore`→`decide_entry` 를 다시 불러 최종 판정한다. 정렬도 진입 우선.
  **이 컷오프도 백테스트로 검증된 값이 아니다**(경험칙) — 재료·뉴스·공시는 사람이 확인한다.
- 손절선 = 5분봉 20이평(`minute_ma`, **완성된 5분봉만** — label=right), 형성 전(대략 10:40 전)엔 VWAP.
- '평소' 를 일평균 × 경과비율로 나누지 않는 이유: 장 초반에 거래가 몰려 선형 비례는 기대치를 과소평가해
  배수가 부풀려진다. 과거 분봉이 2일 미만이면 그 근사로 대체하고 `급증기준="일평균 근사"` 로 표시한다.
- 매수세는 **틱 규칙 추정**(`tick_flow`: 직전 종가 대비 상승=매수, 하락=매도, 보합=봉 방향).
  토스 `/trades` 에는 매수/매도 구분이 없고 최근 50건뿐이라 쓰지 않는다.
- 호가 잔량비(`orderbook_summary`)는 해석이 갈려 **점수에 넣지 않고 표시만** 한다.
- 외국인·기관·프로그램 잠정 순매수(`investor_flow`)는 포착 종목만 조회(3 TPS), 순매수면 +5점.
- 유의종목 차단/표시는 세력추정매매와 **같은 목록**(`StrategyConfig.blocking_warnings/notice_warnings`)을 쓴다.
- 판정·차트 모두 `session_bars`·`continuous_bars`·`tick_flow`·`vwap`·`minute_ma` 같은 함수를 쓴다.

### 토스 1분봉 시각 규칙 (2026-09-16 실측)
- **봉 시각은 봉이 끝나는 시각**이다. 09:00~09:01 거래 → `09:01` 봉. `09:00` 이하 봉은 장전(NXT).
- 접속매매 마지막 봉은 `15:20`, **종가 동시호가 체결은 `15:30` 또는 `15:31` 봉**에 몰린다
  (한선엔지니어링 15:30, 삼성전자 15:31 — 종목마다 다름). 그 뒤는 시간외.
- 그래서 수급 모듈의 정규장은 `09:00 < t ≤ 15:31`, 흐름 계산은 `t ≤ 15:20`(동시호가 제외).
- 참고: 볼린저의 `regular_session` 은 `09:00 ≤ t ≤ 15:30` 이라 장전 `09:00` 봉이 들어가고
  `15:31` 동시호가 봉이 빠진다. 영향은 1~2봉이지만 볼린저를 손볼 때 같이 맞출 것 (아직 미수정).
- `before` 파라미터(`2026-09-15T10:00:59+09:00`)로 과거 특정 시각까지의 분봉을 받을 수 있다.

### 데이터 · 비용
- `TossDataLoader.session_minutes` — 정규장 봉이 있는 **가장 최근 날짜**를 찾고 그날 09:00 이전까지
  페이지를 넘긴다(최대 6쪽). 장 마감 후·장전에는 직전 거래일이 기준일이 된다.
- `minute_profile` — 과거 영업일 09:00~10:00 분봉을 `cache/intraday/{코드}_{날짜}_{분}.json` 에 **영구 캐시**.
  첫 스캔은 70종목 × 5일 호출로 약 40초, 이후 약 16초 (2026-09-16 실측).
- 일봉은 세력추정 스캐너와 같은 `ohlcv(sym, 260)` 캐시를 공유한다.
- `SupplyService` 는 **장중 `interval`(기본 60초, `--supply-interval`), 장 밖 30분**에 한 번 돈다.
  화면을 열었는데 결과가 없으면 `/api/supply/state` 가 즉시 1회 스캔을 띄운다.
  **수급포착은 텔레그램 알림을 보내지 않는다** (2026-10-01 제거 — `SupplyService` 에 `notify` 없음, 화면 확인 전용).
- 세션마다 수급 스레드가 1개 더 뜬다(세션 생성 시 시작, `close()` 에서 정지).
- JSON 으로 나가는 값은 `clean()` 으로 NaN → null 처리한다 (Starlette 는 NaN 을 거부한다).

### 한계 — 문서에 반드시 남길 것
- 승률·기대값을 모른다. 뉴스·공시 확인은 사람이 해야 한다 (API 없음).
- 틱 규칙 매수세는 추정치라 HTS 체결강도와 다르다.
- 스캔 간격(60초) 사이의 급변은 놓친다.

## 세력추정매매 자동 매수 (screener/autobuy.py)
- ⚙설정 → 주문 칸의 **"세력추정매매 시그널 자동 매수"** 체크 + `[점수] 이상` + `[금액]원`.
  `SessionSettings.auto_buy / auto_buy_score(기본 75) / auto_buy_amount` 로 계정 DB 에 저장된다.
  **개인 키 세션에만** 붙는다(`ScanService.auto_buyer`). 서버 기본 스캐너에는 없다.
- 스캔 직후 `AutoBuyer.handle(report.signals)` → 점수 ≥ 컷오프 · 오늘 날짜 시그널 · 09:00~15:20(평일)만.
- 수량 = `min(설정 금액, 1회 주문 한도, 예수금) // 매수가(호가 내림)` — 금액 안에서 최대 수량.
- 주문은 수동 주문과 같은 `TradingService.submit` (지정가 = 전일 고가, 체결 시 OCO 1R 익절 / 손절가).
  **추격 매수하지 않는다** — 백테스트 진입가와 맞추기 위해. 현재가가 이미 1R 이상이면 건너뛴다.
- 자동 주문은 `fill_timeout`=15:30 까지, `cancel_on_timeout=True` 라 **미체결 잔량을 장 마감에 취소**한다
  (손절 없는 늦은 체결 방지). 수동 주문은 예전대로 취소하지 않는다.
- **같은 종목은 하루 1회**(실패해도 재시도 안 함). 재시작 시 주문 로그의 `auto_buy`(실주문) 기록과
  보유 잔고로 중복을 막는다. 모의 기록은 중복 판정에 쓰지 않는다.
- 실주문 게이트는 기존과 동일(서버 `TOSS_TRADING_ENABLED` + `trading_opt_in`). 꺼져 있으면 모의로
  `auto_buy` 이벤트만 남기고 텔레그램으로 알린다. 세션 info 의 `auto_buy_live` 가 실주문 여부.
- 부수 수정: `_filled_qty` — 예전 `int(_to_float(x) or 0)` 은 NaN 이 참이라 `int(nan)` 으로 죽었다.
- 한계: 스캔 주기(기본 300초, 운영 120초) 단위로만 판정한다. 설정의 "장중 실시간 현재가" 를 켜야
  돌파를 장중에 잡는다. 백테스트는 일봉 종가 기준이라 장중 자동 진입의 실측 성과는 아직 없다.

## 돌파 대기(watch) 목록의 의미
- `Screener.watch_candidates` 는 STEP 1+2 만 보고 **STEP 3(돌파)는 보지 않는다.**
  그래서 오늘 이미 시그널이 뜬 종목도 걸린다 — 예전에는 매수 시그널과 돌파대기에 같은 종목이
  동시에 떠서 혼동됐다. 이제 `exclude` 인자로 **시그널 종목 + 유의종목 차단 종목**을 뺀다
  (`ScanService.scan_once`, `main.py watch` 둘 다). 두 목록은 겹치지 않는다 (회귀 테스트 있음).
- 판정 기준은 `detect` 와 같다: **마지막 봉 = 돌파 판정일**, 조정 구간은 그 전날까지.
  따라서 `감시_돌파가(전일고가)` 는 마지막 봉의 `prev_high` 다. 예전 구현은 마지막 봉 자신의
  `high`(=당일 고가)를 이 라벨로 내보내 시그널의 전일고가와 값이 달랐다
  (동국S&C: 시그널 1,440 vs 대기 1,590). 응답에 `판정일` 필드를 함께 싣는다.

## 운영 메모
- 거래대금 상위(`--top`)와 중소형주 필터(`max_market_cap=3조`)를 함께 쓰면 대형주가 대량 탈락한다
  (상위 30 중 27 제외). 넓게 보려면 `--limit` 으로 전체 유니버스를 돌리거나 `max_market_cap` 을 올린다.
- 일봉 판정에서 `현재가 < 매수가`(전일 고가)인 시그널이 나올 수 있다 — 장중 고가로 돌파했다가
  종가가 밀린 윗꼬리 마감이며, 정상 동작이다. 장중 실시간 판정은 `--live`(현재가 사용).

## 작업 규칙
- 새 API 스펙·전략 파라미터 변경·운영 중 발견한 제약은 **이 파일에 즉시 추가**한다.
- 전략 임계값은 코드에 흩뿌리지 말고 `StrategyConfig` 에만 둔다.
- 탐지 로직을 바꾸면 `tests/test_pattern_detector.py` 에 회귀 테스트를 함께 추가한다.
- 주석·출력 문구는 한국어로 통일한다.

## 변경 이력
- 2026-10-01: **수급포착매매 텔레그램 알림 제거** (`SupplyService.notify`·`_alert`, main/sessions 배선, 화면 문구). 테스트 245개 통과.
- 2026-09-26: **세력추정매매 이메일 알림 추가** — `EmailNotifier`(Gmail SMTP, Bcc, HTML+토스 링크),
  `SessionSettings.alert_emails`(여러 주소), 설정 창 이메일 섹션·테스트 전송(`POST /api/session/email/test`).
  발신 계정은 서버 `.env`(`MAIL_SMTP_*`, wani_v2 와 동일 계정), 수신만 사용자별. `AppConfig.mail_*`,
  `SessionManager(mail=...)`. SMTP 로그인 실검증 완료. 테스트 245개 통과.
- 2026-09-18: **볼린저 매수를 지정가+미체결 회수로 전환** — `TradingService.limit_buy`(지정가 = 현재가
  ×(1+`buy_slippage`, 기본 0.5%), `buy_cancel_after` 초 미체결 시 백그라운드 취소). 매도는 시장가 유지.
  `BollingerConfig.buy_slippage`·`buy_cancel_after` 추가, 설정 창 노출, 멱등키 태그 분리. 테스트 239개 통과.
- 2026-09-18: **수급포착 '진입' 판정 + 텔레그램 기준 변경** — 여러 지표(지속+갭 과열 아님+고점권 아님+
  점수 컷오프)를 종합한 `SupplySignal.entry` 플래그 신설(`decide_entry`, `SupplyConfig.entry_score_min=70`).
  화면에 🔥 진입 배지·필터·근거 표시, **텔레그램은 '지속' 전체 대신 '진입' 종목만** 발송. 테스트 225개 통과.
- 2026-09-18: **UI 전면 개편** — 프런트엔드를 **Tailwind CSS(Play CDN) + AG Grid(CDN)** 로 전환.
  세 화면(세력추정·수급·볼린저)의 모든 종목 목록을 AG Grid 로, 스타일을 Tailwind 유틸리티로 재작성.
  `static/ui.js` 신설(tailwind.config·`UI.cls`·`Grid.create`), `common.css` 는 테마 토큰만 남김.
  login·gnb·session·stockmenu 도 Tailwind 화. 실주문 위험 표시(빨간 버튼·경고 배너)와 확인 절차는 유지.
  테스트 220개 통과. **외부 CDN 의존이 새로 생김**(폐쇄망 배포 시 자체 호스팅 필요).
- 2026-09-18: **설정 창 분리** — 🔑 API 키(자격증명) / ⚙ 설정(스캔·알림·주문·자동매수),
  `POST /api/session/settings` 추가(키 재입력 없이 저장), `from_body` 기본값 상속 수정.
  자동매수 `_load_today` 가 주입된 clock 대신 `date.today()` 를 쓰던 문제 수정. 테스트 220개 통과.
- 2026-09-17: **세력추정매매 자동 매수** (`screener/autobuy.py`, 설정 창 점수·금액 저장,
  미체결 잔량 장 마감 취소, 하루 1회·보유 종목 제외). 테스트 218개 통과.
- 2026-09-17: **다중 사용자 보완** — 관리자/일반 구분(서버 계좌 주문·봇은 관리자 전용),
  세션 owner 결속(계정 전환 시 남의 세션 사용 차단), 회원가입(초대 코드 선택), 비밀번호 변경·탈퇴,
  로그인 시도 제한, 사용자별 주문 로그, 봇 실행 중 세션 유휴 폐기 방지, 세션 상한 20. 테스트 209개 통과.
- 2026-09-16: **수급포착매매 추가** (`screener/supply.py`, `/supply`, `main.py supply`,
  `/api/supply/*`, 세션·서버별 `SupplyService`, GNB 맨 앞). 토스 클라이언트에 `candles_page`·
  `orderbook`·`investor_trading`·`program_trades`, 로더에 `turnover_leaders`·`metas`·
  `session_minutes`·`minute_profile`·`investor_flow`·`orderbook_summary`, `StockMeta.security_type` 추가.
  토스 1분봉이 **종료 시각 라벨**이고 종가 동시호가가 `15:30/15:31` 봉에 온다는 것 확인.
  종목 메뉴를 공통 `static/stockmenu.js` 로 분리. **볼린저 설정(감시 목록 포함) DB 저장**
  (`bollinger_configs`, `manage_existing` 제외). 테스트 196개 통과.
- 2026-09-16: **돌파대기 정리 + 종목 메뉴** — 돌파대기에서 시그널·차단 종목 제외, 감시 돌파가를
  당일 고가 → 전일 고가(`prev_high`)로 수정, `판정일` 추가. 종목코드·종목명 클릭 시 모달
  (토스로 보기 / 반전 매매에 종목추가), `POST /api/bollinger/symbols` 추가.
  공통 모달 z-index 20→60 (GNB 에 가려지던 문제). 계정 `djaggi` 추가. 테스트 170개 통과.
- 2026-09-16: 운영 정리 — `.env` 자격증명 갱신 후 **poller 서비스를 `profiles: ["manual"]` 로 내리고
  web 단독 운영**으로 전환(토큰 상호 무효화·429 회피). `env_file` 은 컨테이너 생성 시점에만
  주입된다는 사실 확인(`restart` 로는 새 키가 안 먹는다). 저장된 사용자 설정(`cache/app.db`)의
  옛 키는 로그인 후 ⚙설정에서 다시 넣어야 세션 복구가 된다.
  web 커맨드에 `--live` 를 켜고 스캔 주기를 300→120초로 줄여 poller 의 실시간 감시 역할을 흡수.
- 2026-09-14: **로그인 + 설정 서버 저장** 추가 (`screener/store.py` SQLite, scrypt 비밀번호,
  Fernet 설정 암호화, `require_login` 미들웨어, `/login` 화면, `adduser` CLI,
  로그인 시 저장된 설정으로 세션 자동 복구). 테스트 159개 통과.
- 2026-09-14: 설정 창에 **텔레그램 테스트 전송** 추가 (`TelegramNotifier.check`,
  `POST /api/session/telegram/test`, 토큰/챗ID 형식 검증 + 오류 한국어 안내). 테스트 147개 통과.
- 2026-09-08: **볼린저 밴드 반전 매매 자동매매 추가** — `bollinger.py`·`bollinger_bot.py`,
  화면 `/bollinger`, 시장가 매수·매도(`TradingService.market_order`), 3중 실주문 게이트,
  `manage_existing` 안전장치, 정규장 봉 필터. 공통 GNB(**왔니 주식**)·테마·설정 모달을
  `static/{common.css,gnb.js,session.js}` 로 분리해 두 화면이 공유. 테스트 144개 통과.
  부수 수정: `buying-power` 파라미터(`currency` 필수, `symbol` 없음)·rate limit 그룹(`ORDER_INFO`),
  `holdings` 금액의 통화별 dict 파싱.
- 2026-09-07: 접속자별 API 키 설정 창 추가 (`sessions.py`·`scan_service.py` 분리, 세션 쿠키,
  토큰 캐시 client_id 해시 분리, `--max-sessions`, 테스트 99개 통과).
- 2026-09-01: 토스 실주문 기능 추가 (매수 + OCO 손절·익절, 기본 OFF, 테스트 74개 통과).
- 2026-09-01: 대시보드 가이드·근거 탭, 포지션 계산기, 헤더 툴팁 추가 (테스트 50개 통과).
- 2026-09-01: 시장 국면 필터 + 진입 점수 100점 + 손절 방식 검증.
- 2026-09-01: 매수 유의종목 필터 + 백테스트 엔진 추가 (테스트 34개 통과).
- 2026-09-01: 웹 대시보드 추가 (FastAPI, 8083 포트, 캔들+거래량 차트, 테스트 24개 통과).
- 2026-09-01: 라이브 활성화. `poller` 컨테이너 가동(healthy), 실제 시그널 5건 탐지 및 `out/signals.jsonl` 적재.
  랭킹 API `count>100` → 400 버그와 응답 키(`items`→`rankings`) 오류 수정.
- 2026-09-01: Docker 컨테이너화 (Dockerfile / docker-compose.yml / .dockerignore, 비root·KST·헬스체크·UID 매칭).
- 2026-09-01: 초기 구축 (스크리너 파이프라인 + 토스 OpenAPI 연동 + 합성 데이터 PoC + 테스트 17개 통과).
