#!/usr/bin/env bash
# EC2(Ubuntu) 최초 1회 실행: Docker 설치 + 스왑 메모리 + 앱 폴더 준비
#   사용법: bash server-setup.sh
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/fastapi-parking}"

echo "==> 1. Docker 설치"
if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y ca-certificates curl rsync
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" |
    sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker "$USER"
  sudo systemctl enable --now docker
else
  echo "    이미 설치됨: $(docker --version)"
fi

echo "==> 2. 스왑 메모리 2GB (t3.micro 1GB 메모리로 React 빌드 시 메모리 부족 방지)"
if ! swapon --show | grep -q '/swapfile'; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
else
  echo "    이미 설정됨"
fi

echo "==> 3. 앱 폴더 준비: $APP_DIR"
mkdir -p "$APP_DIR/backend" "$APP_DIR/data"

echo
echo "완료. docker 그룹 권한 적용을 위해 SSH 를 한 번 끊었다가 다시 접속하세요."
