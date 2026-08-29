#!/usr/bin/env bash
set -euo pipefail

ARCH="${1:-arm64}"            # arm64 or amd64
PLATFORM="linux/${ARCH}"
: "${SHARED_DB_PAT:?SHARED_DB_PAT is required for the private shared_db package}"

# 생성물: ./bundle.zip
rm -rf build
mkdir -p build

docker run --rm \
  --platform "${PLATFORM}" \
  -e SHARED_DB_PAT \
  -v "$PWD":/var/task -w /var/task \
  --entrypoint /bin/bash \
  public.ecr.aws/lambda/python:3.12 \
  -lc '
    echo "== pip install =="
    git config --global url."https://${SHARED_DB_PAT}@github.com/".insteadOf "https://github.com/"
    python -m pip install --no-deps -r requirements.lock -t build --only-binary=:all:
    # (필요 시) 누락 방지용 강제 설치 예: pip install pydantic-settings -t build
    echo "== copy app =="
    cp -r app build/
    echo "== prune =="
    find build -name "__pycache__" -type d -exec rm -rf {} +
    find build -name "*.pyc" -delete
    echo "== zip =="
    cd build && python -m zipfile -c ../bundle.zip . && cd -
    ls -lh bundle.zip
  '

rm -rf build
echo "✅ bundle.zip ready"
