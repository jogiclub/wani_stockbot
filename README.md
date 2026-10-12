# 왔니 주식 — 자동 매매 전략 대시보드

두 전략을 한 화면에서 돌립니다. 시세·주문 모두 **토스증권 OpenAPI** 를 씁니다.

| 메뉴 | 경로 | 무엇을 하나 |
|---|---|---|
| **세력추정매매** | `/` | 바닥권 대량거래 **윗꼬리 기준봉(매집봉)** → **매물 소화(조정)** → **전일 고가 돌파** 3단계를 자동 탐지하는 KOSPI/KOSDAQ 스크리너 |
| **볼린저 밴드 반전 매매** | `/bollinger` | 박스권에서 밴드 하한+RSI 과매도에 사고 상한+과매수에 파는 평균 회귀 전략. 조건 충족 시 **실제 시장가 매수·매도까지 자동 실행** |

상단 GNB 로 화면을 오갑니다. 테마 전환과 개인 API 키 설정도 GNB 에 있습니다.

## 설치
```bash
python3 -m venv .venv-linux
.venv-linux/bin/pip install -r requirements.txt
cp .env.example .env   # TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 입력
```

## 빠른 시작 (API 없이 동작 확인)
```bash
.venv-linux/bin/python poc.py
```

## 실행
```bash
# 거래대금 상위 100 종목 일봉 스캔
.venv-linux/bin/python main.py scan --top 100

# 전체 유니버스 스캔 + CSV 저장
.venv-linux/bin/python main.py scan --csv-out signals.csv

# 장중 실시간 돌파 감시 (60초 주기, 텔레그램 알림)
.venv-linux/bin/python main.py poll --top 200 --interval 60

# 아직 돌파 전인 '대기' 종목 (STEP1+2 통과)
.venv-linux/bin/python main.py watch --top 200

# 특정 종목 조건 진단
.venv-linux/bin/python main.py explain --symbols 005930 000660
```

주요 옵션: `--tail 0.35` `--vol-mult 5` `--max-adjust 10` `--drawdown 0.30`
`--limit N` `--top N` `--workers 8` `--live` `--out signals.jsonl`

## 웹 대시보드 (http://localhost:8083)
```bash
docker compose up -d web
```
브라우저에서 **http://localhost:8083** 접속. 5분 주기로 자동 스캔하며 화면이 갱신됩니다.

- **매수 시그널** 탭 — 조건을 모두 통과한 종목 (기준봉일·윗꼬리·거래량 배수·조정일수·전일고가·현재가·거래량 증가율·매수가·손절가·손절폭)
- **돌파 대기** 탭 — STEP1+2 통과, 전일 고가 돌파만 남은 관심 종목
- **전략 설정** 탭 — 현재 적용 중인 임계값 전체
- 행을 클릭하면 최근 60일 **캔들 + 거래량 차트**가 열립니다. 기준봉은 노란 띠로 강조되고,
  매수가·손절가가 기준선으로 그려집니다. 마우스를 올리면 십자선과 OHLCV 툴팁이 뜹니다.
- **가이드 · 근거** 탭 — 전략 3단계 설명, 배점표와 그 근거, 등급별 실측 성과, 청산 규칙,
  검증 기록(프로파일·손절방식·국면 비교), 한계까지 화면 안에서 모두 확인할 수 있습니다.
- 표 헤더에 마우스를 올리면 각 지표가 무슨 뜻이고 어느 구간이 좋은지 설명이 뜹니다.
- 시그널 상세에 **포지션 계산기**가 있습니다 — 계좌 잔고와 1회 허용 손실(%)을 넣으면
  매수 수량·투입 금액·손절 시 손실액·1R 도달 시 수익을 바로 계산합니다(입력값은 브라우저에 저장).
- 우상단 **⚙ 설정** 버튼에서 **접속자 각자의 토스 API 키**를 넣을 수 있습니다 (아래 참고).
- 우상단 **지금 스캔** 버튼으로 즉시 재스캔, **◐ 테마** 버튼으로 라이트/다크 전환.
- URL 딥링크: `?tab=guide|signals|watch|config`, `?theme=dark|light`

로컬(도커 없이) 실행:
```bash
.venv-linux/bin/python main.py web --port 8083 --top 200 --interval 300
```

### 개인 API 키로 쓰기 (여러 사람이 한 서버를 공유)
서버 `.env` 에 키를 고정하지 않아도 됩니다. 대시보드 상단 **⚙ 설정** 에서 접속자가 자기
토스 OpenAPI `client_id` / `client_secret` 을 입력하면, 그 키 전용 스캐너가 세션별로 뜹니다.

```bash
# 서버 키 없이 띄우기 — 접속자가 각자 키를 입력한다
python main.py web --port 8083 --max-sessions 20
```

- 설정 창에서 스캔 범위(거래대금 상위 N·주기·장중 실시간), 텔레그램 알림, 주문 한도·계좌를 각자 지정합니다.
- 저장 시 실제 API 를 한 번 호출해 키 유효성과 허용 IP 등록 여부를 즉시 확인합니다.
- 로그인 계정별로 설정이 **서버 DB(`cache/app.db`)에 저장**됩니다. secret·봇 토큰은 암호화, 비밀번호는 scrypt 해시.
  응답에는 절대 실리지 않고 화면에는 마스킹된 `client_id` 만 보여줍니다.
- 다른 사람은 로그인 화면의 **회원가입** 탭으로 계정을 만듭니다 (`APP_SIGNUP=true`, 기본).
  외부에 공개한다면 `APP_SIGNUP_CODE` 로 초대 코드를 걸어 두세요. 계정은 GNB 의 아이디를 눌러
  비밀번호 변경·탈퇴할 수 있습니다. 로그인은 5분에 5회 실패하면 잠깁니다.
- 세션은 로그인 계정에 묶입니다 — 같은 브라우저에서 계정을 바꿔도 앞사람의 키·계좌에 접근할 수 없습니다.
- 6시간 동안 요청이 없는 세션은 폐기됩니다(볼린저 봇이 도는 세션은 제외). 동시 세션 상한은 `--max-sessions`(기본 20).
- `.env` 에 서버 키가 있으면 시세 화면(스캔)은 모두가 공유하지만, **서버 계좌로 주문·봇을 쓰는 건
  관리자 계정만** 가능합니다 (`APP_ADMIN_USER`, 또는 `python main.py adduser <아이디> --admin`).
- 주문·봇 로그는 사용자별로 `out/users/<아이디>/` 에 남습니다.
- 실주문은 여전히 **서버의 `TOSS_TRADING_ENABLED=true` + 사용자의 '실주문 허용' 체크** 가 둘 다 있어야 켜지며,
  1회 한도는 서버 `TOSS_MAX_ORDER_AMOUNT` 를 넘을 수 없습니다.

> 공개된 곳에 올릴 때는 HTTPS 뒤에 두세요. 세션 쿠키가 곧 그 사람의 자격증명 접근권입니다.

### API
| 엔드포인트 | 설명 |
|---|---|
| `GET /api/state` | 최신 스캔 상태 + 시그널 + 대기 종목 (`configured`·`source` 포함) |
| `GET /api/session` | 현재 세션이 어떤 키로 도는지 (마스킹된 `client_id`, secret 없음) |
| `POST /api/session` | 개인 API 키 등록 → 세션 쿠키 발급 + 개인 스캐너 시작 |
| `DELETE /api/session` | 개인 세션 해제 (스캔 스레드 종료) |
| `GET /api/signals` · `GET /api/watch` | 각각의 목록만 |
| `GET /api/chart/{symbol}?count=60` | 상세 차트용 일봉 |
| `GET /api/config` | 전략 파라미터 |
| `GET /api/guide` | 전략 설명 + 배점 근거 + 백테스트 실측치 (가이드 탭 데이터) |
| `POST /api/scan` | 즉시 재스캔 (진행 중이면 409) |
| `GET /api/trading/status` | 실거래 on/off · 계좌(마스킹) · 금액 한도 |
| `POST /api/trading/preview` | 호가 정렬·검증만 (주문 없음) |
| `POST /api/trading/order` | 매수 + OCO 등록 (`dry_run=false` + `confirm="실행"` 필요) |
| `GET /healthz` · `GET /docs` | 헬스체크 · OpenAPI 문서 |

## Docker 로 실행
```bash
docker compose build            # 호스트 UID 가 1000 이 아니면 아래 참고
docker compose --profile manual run --rm demo      # API 없이 동작 확인
docker compose --profile manual run --rm screener  # 1회 스캔 → out/signals.csv
docker compose up -d web                           # 웹 대시보드 (8083)
docker compose up -d poller                        # 장중 상시 감시 데몬
docker compose logs -f poller
docker compose down
```
임의 옵션으로 1회 실행:
```bash
docker compose --profile manual run --rm screener scan --top 200 --live
```
호스트 UID 가 1000 이 아니면 (`./cache`, `./out` 쓰기 권한):
```bash
APP_UID=$(id -u) APP_GID=$(id -g) docker compose build
```
- 타임존은 컨테이너 안에서 `Asia/Seoul` 로 고정됩니다.
- `.env` 는 이미지에 포함되지 않고 `env_file` 로 런타임 주입됩니다.
- `./cache`(일봉 캐시), `./out`(signals.csv / signals.jsonl) 이 호스트에 마운트됩니다.

## 탐지 조건
| 단계 | 조건 |
|---|---|
| STEP 1 기준봉 | 120일 최고 종가 대비 −30% 이상 하락(또는 종가 ≤ MA120×1.10) · 윗꼬리 ≥ 35% · 일중 변동폭 ≥ 5% · 20일 최고 거래량 또는 20일 평균 ×5 |
| STEP 2 조정 | 기준봉 후 1~10영업일 · 평균 거래량이 기준봉의 60% 이하 · 기준봉 저가 종가 미이탈 · 기준봉 고가 +15% 초과 상회 없음 |
| STEP 3 돌파 | 당일 거래량 ≥ 전일 거래량 · 가격 > **전일 고가** (장중은 현재가로 판정) |

출력: 종목코드/종목명, 기준봉 발생일, 전일고가(돌파선), 현재가, 거래량 증가율,
매수가(전일 고가), 손절가(기준봉 저가), 1R/2R/3R 목표가.

## 전략 프로파일
| 프로파일 | 내용 | 3년 백테스트 |
|---|---|---|
| `tuned` **(기본)** | 윗꼬리 ≥45%, 기준봉 거래량 20일최고 **AND** 평균×20, 조정 1~3일 | 1,230거래 · 승률 55.2% · 평균 +0.076R · 수익팩터 1.17 |
| `spec` | 원본 전략 스펙 (윗꼬리 ≥35%, 거래량 OR 조건, 조정 1~10일) | 15,099거래 · 승률 48.0% · 평균 **-0.084R** · 수익팩터 0.84 |

원본 스펙 그대로는 3년 기준 손실이라, 백테스트로 선별한 `tuned` 를 기본값으로 씁니다.
원본으로 돌리려면 `--profile spec`.

## 진입 점수 (100점 만점)
각 시그널을 **백테스트 실측 성과에서 역산한 배점**으로 채점해 우선순위를 매깁니다.

| 요인 | 배점 | 만점 구간 |
|---|---|---|
| 손절폭 | 25 | 8% 이하 |
| 기준봉 거래량 | 20 | 20일 평균의 50~100배 |
| 조정 거래량비 | 20 | 0.10~0.20 |
| 조정 기간 | 15 | 2영업일 |
| 윗꼬리 비율 | 10 | 55~65% |
| 시장 국면 | 10 | 지수가 이동평균 위 |

등급: **85+ A+**(적극 검토) · **75+ A**(검토 가능) · **65+ B**(관망) · **50+ C**(관망) · **50 미만 D**(회피)

**실전 컷오프는 75점입니다.** 과거 거래를 이 점수로 사후 채점한 결과:
A+ 승률 69.1%·평균 +0.412R, A 60.0%·+0.165R 인 반면 **B(65~74)는 50.1%·−0.048R 로 적자**였습니다.
75점 이상만 매매했다면 523건·승률 62.5%·평균 +0.234R.
(배점을 뽑은 표본과 같은 데이터라 in-sample 결과입니다 — 미래 성과 보장이 아닙니다.)
대시보드 표에 점수·등급·막대가 뜨고, 행을 클릭하면 요인별 획득 점수와 근거가 펼쳐집니다.
시그널은 점수 내림차순으로 정렬됩니다.

> 거래량 배수·조정 거래량비·윗꼬리는 **클수록 좋은 게 아니라 중간 구간이 최적**입니다
> (예: 조정 거래량비 0.10~0.20 이 +0.315R 로 최고, 0.10 미만은 +0.111R). 실측 결과를 그대로 반영했습니다.

## 손절 방식
`stop_mode` 기본값은 **기준봉 저가(`base_low`)** 입니다. 3년 백테스트에서 유일하게 흑자였습니다.

| 손절 기준 | 승률 | 평균 R | 수익팩터 | 최대낙폭 |
|---|---|---|---|---|
| **기준봉 저가 (기본)** | 55.2% | **+0.076R** | 1.17 | 40.5R |
| 전일 저가 | 49.8% | −0.080R | 0.85 | 132.6R |
| 고정 5% | 50.8% | −0.023R | 0.95 | 80.9R |

손절을 타이트하게 잡으면(전일 저가, 평균 보유 2.2일) 휩쏘에 털려 오히려 성과가 나빠집니다.

## 시장 국면 필터
코스닥 지수가 20일 이동평균 **아래**면 배너로 경고하고 점수에서 10점을 깎습니다.
3년 실측상 이 구간의 시그널은 평균 **−0.117R**(손실), 위쪽 구간은 **+0.173R** 이었습니다.
아예 시그널을 버리려면 `StrategyConfig(regime_blocks=True)`.

## 볼린저 밴드 반전 매매 (자동매매)

박스권 평균 회귀 전략입니다. **기본은 모의(dry-run)** 이며, 실주문은 게이트 3개를 모두 켜야 나갑니다.

### 매매 규칙
| 액션 | 조건 | 수량 |
|---|---|---|
| 매수 | 현재가 ≤ 밴드 하한선 **그리고** RSI(14) ≤ 30 | 예수금 × 설정 비율(기본 20%), 1회 주문 한도 이내 |
| 전량 매도 | 현재가 ≥ 밴드 상한선 **그리고** RSI ≥ 70 | 보유 전량 |
| 부분 익절 | 보유 중 중앙선(20봉 이평) 도달 (매수가 위, 1회만) | 보유의 50% (끌 수 있음) |
| 손절 | 매수가 대비 −5% 이탈 | 전량 (조건 불문) |

판정 우선순위는 **손절 → 청산 → 부분익절 → 진입** 입니다. 하한선을 타고 계속 밀리는
‘밴드 라이딩’ 구간에서 매수 조건이 계속 참이 되어 물타기로 빠지는 것을 막습니다.

### 화면에서 쓰기
1. `/bollinger` → **전략 설정** 탭에서 감시 종목(6자리 코드)·봉 주기·지표·자금 운용·손절 폭 저장
2. **봇 시작** → 설정한 주기(기본 60초)마다 판정하고, 조건이 맞으면 주문
3. **감시 현황** 탭에서 종목별 밴드·RSI·%B 와 판정 사유 확인 (행 클릭 시 밴드+RSI 차트)
4. **체결 · 로그** 탭에서 주문 이력 확인 (`out/bollinger.log` 에도 남습니다)

### CLI
```bash
# 모의 실행 (주문 안 나감)
.venv-linux/bin/python main.py bollinger --symbols 005930 000660 --bb-interval 15m

# 파라미터 조정
.venv-linux/bin/python main.py bollinger --symbols 005930 \
  --bb-interval 15m --bb-period 20 --bb-stddev 2 \
  --rsi-buy 30 --rsi-sell 70 --cash-ratio 0.2 --stop-loss 0.05 --max-positions 3

# ⚠ 실주문 (.env 의 TOSS_TRADING_ENABLED=true 도 함께 필요)
.venv-linux/bin/python main.py bollinger --symbols 005930 --live-orders
```

### 안전장치
- **실주문 게이트 3개**: 서버 `TOSS_TRADING_ENABLED=true` + 설정의 ‘실주문 허용’ + 화면의 **실주문으로 전환**.
  하나라도 꺼져 있으면 모의로만 돕니다.
- **정규장(09:00~15:30) 밖에서는 주문하지 않습니다.** 판정·표시만 합니다.
- **감시 목록에 있는 국내 종목만** 건드립니다. 계좌의 다른 보유분은 관리 대상이 아닙니다.
- **기존 보유분은 기본적으로 팔지 않습니다.** 감시 목록에 이미 들고 있던 종목이 있으면
  판정만 표시하고 청산하지 않습니다 — 장기 보유 종목을 넣었다가 첫 주기에 털리는 사고를 막습니다.
  의도한 경우에만 설정에서 **‘기존 보유분도 관리’** 를 켜세요.
- 1회 주문 금액은 `TOSS_MAX_ORDER_AMOUNT` 를 넘지 못합니다 (매도에는 적용하지 않습니다 —
  손절이 한도에 막히면 위험이 더 커지므로).
- 모든 판정·주문은 `out/bollinger.log` 와 `out/orders.jsonl` 에 기록되고, 텔레그램을 설정했다면
  체결·손절 시 알림이 갑니다.

### 알아둘 점
- 토스 캔들 API 는 `1분봉`/`일봉`만 주므로 5·15·30·60분봉은 1분봉을 합쳐 만듭니다.
  시간외 단일가 봉은 지표에서 제외합니다(유동성이 달라 밴드를 왜곡).
- **이 전략은 이 저장소에서 백테스트로 검증된 수치가 없습니다.** 세력추정매매와 달리
  승률·기대값을 아직 모릅니다. 소액·모의로 충분히 관찰한 뒤 쓰세요.
- 시장가 주문이라 호가가 얇은 종목에서는 슬리피지가 큽니다.

## 토스 주문 실행 (매수 + 손절·익절 자동 등록)
시그널 상세의 포지션 계산기 아래에 주문 버튼이 있습니다.

| 버튼 | 동작 |
|---|---|
| **모의 주문 확인** | 실주문 없이 호가 정렬·검증 결과만 보여줍니다. 항상 사용 가능 |
| **토스 매수 주문** | 지정가 매수 → **체결되면 손절·익절을 OCO 조건주문으로 자동 등록** |

주문 흐름
1. `POST /api/v1/orders` — 지정가 매수 (매수가 = 전일 고가)
2. 백그라운드에서 체결 감시 (`GET /api/v1/orders/{orderId}` 폴링, 기본 10분)
3. 체결 수량만큼 `POST /api/v1/conditional-orders` — **OCO**
   - `first` = 익절(1R 목표가), `second` = 손절(기준봉 저가), 둘 다 SELL 지정가
   - 하나가 발동하면 나머지는 자동 취소됩니다

### 실거래 켜기 (기본 꺼짐)
```bash
# .env
TOSS_TRADING_ENABLED=true      # 이걸 켜야 실주문이 나갑니다
TOSS_MAX_ORDER_AMOUNT=1000000  # 1회 주문 금액 상한
# TOSS_ACCOUNT_SEQ=            # 비우면 첫 번째 계좌 사용
```
```bash
docker compose up -d --force-recreate web
```

### 종목 바로가기
매수 시그널·돌파 대기 표의 **종목코드·종목명을 클릭**하면 토스증권 해당 종목 페이지가
새 창으로 열립니다(`https://www.tossinvest.com/stocks/{코드}`). 상세 패널에도
**「토스증권에서 열기 ↗」** 버튼이 있습니다.

### 안전장치
- **기본은 모의 전용** — `TOSS_TRADING_ENABLED=true` 가 아니면 실주문 API 자체가 거부됩니다
- **금액 상한** — `TOSS_MAX_ORDER_AMOUNT` 초과 주문은 서버에서 차단
- **이중 확인** — 브라우저 확인창 + 서버가 `dry_run=false` 와 `confirm="실행"` 을 **둘 다** 요구
- **멱등키** — 같은 종목·가격·수량·날짜의 주문은 `clientOrderId` 로 중복 체결 방지
- **주문 POST 는 재시도 안 함** — 네트워크 오류 시 중복 체결 위험을 없앰
- **호가 단위 자동 정렬** — KRX 호가 그리드에 맞춰 내림 정렬 (`invalid-tick-size` 방지)
- **전량 기록** — 요청·응답·OCO 결과가 `out/orders.jsonl` 에 남습니다
- 강제로 끄려면 `--no-trading` (환경변수보다 우선)

### 알려진 함정 — 토큰은 클라이언트당 1개
토스는 **자격증명당 유효 토큰이 1개**라, `web` 과 `poller` 를 같이 띄우면 서로 토큰을
무효화시켜 **주문이 `401 invalid-token` 으로 실패**합니다. 두 가지로 막았습니다.
- `cache/tokens/<client_id 해시>.json` + 파일 락으로 **프로세스·세션 간 토큰 공유**
  (자격증명이 다르면 파일도 갈라져 서로를 무효화하지 않습니다)
- 주문 POST 는 **401 에 한해 1회 재시도** (인증 거절이라 중복 체결 위험 없음)

다만 **TPS 제한은 프로세스별**이라 둘을 함께 돌리면 합산 초과로 429 가 납니다.
`web` 이 `poller` 기능(주기 스캔 + 텔레그램 알림)을 모두 포함하므로 **`web` 만 띄우는 걸 권장**합니다.
```bash
docker compose stop poller
```

> ⚠️ OCO 등록은 **매수 체결 이후**에 이뤄집니다. 체결이 안 되거나 OCO 등록이 실패하면
> `out/orders.jsonl` 에 `oco_failed` / `oco_skipped` 로 남으니, 그 경우 **직접 손절을 걸어야 합니다.**

## 백테스트 — 전략 검증
과거 데이터로 승률·손익비·최대낙폭을 측정합니다. **라이브 스크리너와 같은 탐지 엔진**을 쓰므로
화면에 뜨는 시그널과 동일한 조건이 재현됩니다.
```bash
.venv-linux/bin/python main.py backtest --years 3 --csv-out out/backtest_trades.csv
```
```bash
.venv-linux/bin/python main.py backtest --limit 300 --years 2 --max-hold 10 --target-r 2
```
청산 규칙(옵션으로 조정): `--tp-r 1`(1차 익절 R) · `--target-r 3`(최종 목표) ·
`--max-hold 15`(시간 손절 영업일) · `--slippage 0.003`

리포트에는 전체 지표 외에 **윗꼬리 비율별 / 기준봉 거래량별 / 조정 거래량비별 / 조정 기간별**
성과 분해가 함께 나옵니다 — 어떤 구간이 실제로 돈이 됐는지 보고 `StrategyConfig` 를 조이는 근거로 쓰세요.
거래 내역은 CSV 로 저장됩니다.

**진입/청산 가정**
- 진입: 시그널 당일 전일 고가(돌파선)에 체결 + 슬리피지
- 손절: 장중 저가가 손절가 이탈 시 손절가 체결, 갭하락이면 시가 체결
- 1R 도달 시 절반 익절 후 남은 물량은 본전 스톱, 3R 도달 시 전량, `max_hold` 초과 시 종가 청산
- 데이터 끝에 걸려 청산을 관찰할 수 없는 시그널은 **거래로 세지 않습니다**

## 매수 유의 종목 필터
시그널이 확정된 종목에 한해 `GET /api/v1/stocks/{symbol}/warnings` 를 조회해
**정리매매·투자경고·투자위험·단기과열·신주인수권** 종목을 자동 제외합니다
(전수 조회는 STOCK 그룹 5 TPS 제한 때문에 비용이 커서 시그널 종목만 조회).
VI 발동은 일시적 이벤트라 제외하지 않고 화면에 태그로 표시합니다.
거래정지·정리매매·우선주는 유니버스 단계에서 제외됩니다.
끄려면 `StrategyConfig(exclude_warned=False)`.

## 파라미터 튜닝
모든 임계값은 `screener/config.py` 의 `StrategyConfig` 한 곳에 있습니다.
```python
cfg = StrategyConfig(upper_tail_min=0.30, adjust_max_days=15, vol_mult_min=4.0)
```

## 테스트
```bash
.venv-linux/bin/python -m pytest tests -q
```

## 구조
```
screener/config.py           전략·환경 설정
screener/toss_client.py      토스 OpenAPI REST 클라이언트 (TPS 제한/재시도)
screener/data_loader.py      OHLCV 수집 (Toss / CSV / 합성)
screener/pattern_detector.py STEP1~3 탐지 엔진
screener/screener.py         유니버스 스캔 오케스트레이션
screener/notifier.py         콘솔 / 텔레그램 / JSONL 알림
screener/regime.py           시장 국면 판정 (지수 vs 이동평균)
screener/scoring.py          진입 점수 100점 산정
screener/trading.py          토스 주문 실행 (지정가 매수+OCO, 시장가 단건)
screener/bollinger.py        볼린저 반전 전략 지표·판정 엔진 (BB·RSI)
screener/bollinger_bot.py    볼린저 자동매매 루프 (잔고 동기화 → 판정 → 주문)
screener/evidence.py         백테스트 실측 근거 수치 (문서·UI 단일 출처)
screener/backtest.py         백테스트 엔진 (승률·손익비·MDD)
screener/web.py              FastAPI 웹 대시보드 + JSON API
screener/scan_service.py     주기 스캔 서비스 (웹·세션 공용)
screener/sessions.py         접속자별 API 키 세션 (개인 스캐너·주문 서비스)
screener/static/dashboard.html  세력추정매매 화면 (캔들·거래량 차트)
screener/static/bollinger.html  볼린저 반전 매매 화면 (밴드·RSI 차트)
screener/static/common.css      공통 테마·GNB·모달 스타일
screener/static/gnb.js          공통 GNB(왔니 주식) + 테마 토글
screener/static/session.js      공통 설정 모달 (개인 API 키 세션)
main.py                      CLI
poc.py                       최소 동작 예시
Dockerfile                   컨테이너 이미지 (비root, KST, 헬스체크)
docker-compose.yml           web(8083) / poller(데몬) / screener(1회 스캔) / demo
```

## 주의
- 투자 판단의 참고 도구이며 수익을 보장하지 않습니다. 실매매 전 반드시 백테스트/모의투자로 검증하세요.
- 볼린저 반전 매매는 **조건 충족 시 확인 없이 주문을 냅니다.** 실주문을 켜기 전에 모의 모드로
  충분히 관찰하고, 1회 주문 한도와 감시 종목을 반드시 확인하세요.
- `.env` 의 API 키는 절대 커밋하지 마세요 (`.gitignore` 에 등록되어 있습니다).
