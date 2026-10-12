#!/usr/bin/env bash
# 푸시 + 개발서버 반영을 한 번에: 테스트 → git push origin main → ./deploy.sh
# 커밋은 미리 해 둔다 (커밋되지 않은 변경은 배포되지 않는다). 푸시가 실패하면 배포하지 않는다.
set -euo pipefail
cd "$(dirname "$0")"

[ "$(git branch --show-current)" = "main" ] || { echo "main 브랜치에서만 실행합니다" >&2; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "커밋되지 않은 변경이 있습니다 — 먼저 커밋하세요" >&2; exit 1; }

PY=.venv-linux/bin/python
[ -x "$PY" ] || PY=python3
"$PY" -m pytest -q 2>&1 | tail -2

git push origin main
./deploy.sh
