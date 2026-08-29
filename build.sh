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
    # Flags mirror scripts/compile_requirements.sh and deploy.yml: the lock was
    # resolved for this exact target, so pinning it here keeps the local bundle
    # byte-comparable with the one CI builds.
    python -m pip install --no-deps -r requirements.lock -t build \
      --platform manylinux2014_aarch64 \
      --python-version 3.12 \
      --implementation cp \
      --abi cp312 \
      --only-binary=:all:
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
