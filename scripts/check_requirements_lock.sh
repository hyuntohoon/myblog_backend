#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="${ROOT_DIR}/requirements.lock"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/requirements-check.XXXXXX")"
trap 'rm -rf "${TEMP_DIR}"' EXIT
COMPILED_LOCK="${TEMP_DIR}/requirements.lock"

if [[ ! -f "${LOCK_FILE}" ]]; then
  echo "error: requirements.lock is missing" >&2
  exit 1
fi

# Seeding is intentional: pip-compile keeps the existing exact resolution, while
# the generated source digest still makes every requirements.txt edit detectable.
cp "${LOCK_FILE}" "${COMPILED_LOCK}"
"${ROOT_DIR}/scripts/compile_requirements.sh" "${COMPILED_LOCK}"

if ! cmp -s "${LOCK_FILE}" "${COMPILED_LOCK}"; then
  echo "error: requirements.lock is out of date; run ./scripts/compile_requirements.sh" >&2
  diff -u "${LOCK_FILE}" "${COMPILED_LOCK}" || true
  exit 1
fi

echo "requirements.lock is up to date."
