#!/usr/bin/env bash
set -euo pipefail

# Verification deliberately delegates to compile_requirements.sh rather than
# reimplementing the resolution: one script means CI cannot drift away from
# what a developer runs locally. Because that script pins the resolution
# target, this check produces the same verdict on the x86_64 CI runner as on
# an arm64 laptop.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="${ROOT_DIR}/requirements.lock"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/requirements-check.XXXXXX")"
trap 'rm -rf "${TEMP_DIR}"' EXIT
COMPILED_LOCK="${TEMP_DIR}/requirements.lock"

if [[ ! -f "${LOCK_FILE}" ]]; then
  echo "error: requirements.lock is missing" >&2
  exit 1
fi

# Seeding is intentional: uv keeps the existing exact resolution where it is
# still valid, while the generated source digest still makes every
# requirements.txt edit detectable.
cp "${LOCK_FILE}" "${COMPILED_LOCK}"
"${ROOT_DIR}/scripts/compile_requirements.sh" "${COMPILED_LOCK}"

if ! cmp -s "${LOCK_FILE}" "${COMPILED_LOCK}"; then
  echo "error: requirements.lock is out of date; run ./scripts/compile_requirements.sh" >&2
  diff -u "${LOCK_FILE}" "${COMPILED_LOCK}" || true
  exit 1
fi

echo "requirements.lock is up to date."
