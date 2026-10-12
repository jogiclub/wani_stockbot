#!/usr/bin/env bash
# 개발서버(webhows-dev)에 현재 소스를 반영한다 (rsync → docker compose up -d --build web).
# 서버의 .env · cache/(app.db 계정·설정, 일봉/분봉 캐시) · out/(주문·신호 기록) 는 절대 덮어쓰거나 지우지 않는다.
# 사용: ./deploy.sh [-n]   (-n = 파일 전송 미리보기만, 아무것도 바꾸지 않음)
set -euo pipefail
cd "$(dirname "$0")"
HOST=webhows-dev
DIR=stock-find
PORT=8083

DRY=""
[ "${1:-}" = "-n" ] && DRY="-n"

# 커밋된 소스(HEAD)만 보낸다. 작업 트리는 autocrlf 로 CRLF 가 섞일 수 있어 git archive 로 LF 사본을 만든다.
[ -z "$(git status --porcelain --untracked-files=no)" ] || echo "⚠ 커밋되지 않은 변경은 배포되지 않습니다 (HEAD 기준)" >&2
SRC=$(mktemp -d)
trap 'rm -rf "$SRC"' EXIT
git -c core.autocrlf=false archive HEAD | tar -x -C "$SRC"

# 원격 main 에서 합쳐진 옛 wani_stockbot 파일(app/ data/ devserver 로그)과 로컬 전용 파일은 보내지 않는다.
rsync -az $DRY --delete --itemize-changes \
  --exclude '.git' --exclude '.idea' --exclude '.venv' --exclude '.venv-linux' \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '*:Zone.Identifier' \
  --exclude '.env' --exclude 'cache/' --exclude 'out/' \
  --exclude '/app/' --exclude '/data/' --exclude '/devserver.*.log' --exclude '/docs/' \
  "$SRC/" "$HOST:$DIR/"

[ -n "$DRY" ] && { echo "(미리보기만 했습니다)"; exit 0; }

ssh "$HOST" "cd ~/$DIR && docker compose up -d --build web 2>&1 | tail -3"
for i in 1 2 3 4 5 6; do
  sleep 5
  out=$(ssh "$HOST" "curl -s localhost:$PORT/healthz" || true)
  [ -n "$out" ] && { echo "healthz: $out"; exit 0; }
done
echo "healthz 응답 없음 — ssh $HOST 'docker logs --tail 50 stock-find-web' 로 확인" >&2
exit 1
