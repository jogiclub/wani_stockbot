# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# 한국 시장 기준이므로 컨테이너 타임존을 KST 로 고정한다.
ENV TZ=Asia/Seoul \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 레이어를 먼저 굽는다 (소스만 바뀌면 재설치하지 않음)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY screener/ ./screener/
COPY main.py poc.py ./

# 캐시·산출물 디렉터리 (compose 에서 볼륨 마운트)
# 호스트 사용자와 UID 를 맞춰야 bind mount 한 cache/out 에 쓸 수 있다.
# 호스트 UID 가 1000 이 아니면: docker build --build-arg APP_UID=$(id -u) ...
ARG APP_UID=1000
ARG APP_GID=1000
RUN mkdir -p /app/cache /app/out \
 && groupadd -g ${APP_GID} screener 2>/dev/null || true \
 && useradd -m -u ${APP_UID} -g ${APP_GID} screener 2>/dev/null || true \
 && chown -R ${APP_UID}:${APP_GID} /app
USER ${APP_UID}:${APP_GID}

# 로그인 없이도 도는 헬스체크: 탐지 엔진이 살아있는지 확인
HEALTHCHECK --interval=5m --timeout=20s --start-period=20s --retries=3 \
  CMD python -c "from screener.data_loader import SyntheticDataLoader as L; \
from screener.pattern_detector import PatternDetector as P; \
import sys; sys.exit(0 if P().detect(L().ohlcv('900001'),'900001') else 1)"

EXPOSE 8083

ENTRYPOINT ["python", "main.py"]
CMD ["scan", "--top", "100"]
