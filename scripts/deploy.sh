#!/usr/bin/env bash
# EC2 에서 실행: 이미지 빌드 + 컨테이너 재시작 + 동작 확인
#   GitHub Actions 가 코드를 올린 뒤 자동으로 실행한다. (수동 실행도 가능)
#   사용법: bash scripts/deploy.sh
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/fastapi-parking}"
cd "$APP_DIR"

if [ ! -f backend/.env ]; then
  echo "ERROR: $APP_DIR/backend/.env 가 없습니다. API 키 파일을 먼저 서버에 올려 주세요." >&2
  exit 1
fi
mkdir -p data

# 서버 루트의 .env (WEB_PORT 등) 가 있으면 docker compose 가 자동으로 읽음
PORT="$(grep -s '^WEB_PORT=' .env | cut -d= -f2 || true)"
PORT="${PORT:-80}"

echo "==> 이미지 빌드 및 컨테이너 재시작"
docker compose up -d --build --remove-orphans

echo "==> 사용하지 않는 이미지 정리 (디스크 절약)"
docker image prune -f >/dev/null

echo "==> 동작 확인 (http://localhost:$PORT/api/health)"
for i in $(seq 1 20); do
  if curl -fsS "http://localhost:$PORT/api/health"; then
    echo
    echo "배포 성공"
    docker compose ps
    exit 0
  fi
  sleep 3
done

echo "ERROR: 헬스체크 실패. 로그:" >&2
docker compose logs --tail=50
exit 1
