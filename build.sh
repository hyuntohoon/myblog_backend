#!/usr/bin/env bash
set -euo pipefail

# The lock is resolved for aarch64 only and the pip call below hard-targets it,
# so this builds the arm64 Lambda bundle and nothing else. It used to take an
# arch argument; against a target-pinned lock that could only ever produce an
# arm64 bundle inside an amd64 container.
PLATFORM="linux/arm64"
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
    # resolved for this exact target, so pinning it here makes the local bundle
    # hold the same distributions, from the same wheels, as the one CI builds.
    # (The zips themselves are not byte-identical -- this script and the deploy
    # job use different zip tools and neither normalizes mtimes.)
    python -m pip install --no-deps --no-cache-dir -r requirements.lock -t build \
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
