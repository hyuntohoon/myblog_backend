#!/usr/bin/env bash
set -euo pipefail

# Verification delegates to compile_requirements.sh rather than
# reimplementing the resolution: one code path means CI cannot drift away
# from what a developer runs locally. Because that script pins both the
# resolution target and the index freeze, and seeds nothing from the previous
# lock, this check produces the same verdict on the x86_64 CI runner as on an
# arm64 laptop -- and it rejects a hand-edited pin, not just a hand-edited
# requirements.txt.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="${ROOT_DIR}/requirements.lock"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/requirements-check.XXXXXX")"
trap 'rm -rf "${TEMP_DIR}"' EXIT
COMPILED_LOCK="${TEMP_DIR}/requirements.lock"

if [[ ! -f "${LOCK_FILE}" ]]; then
  echo "error: requirements.lock is missing" >&2
  exit 1
fi

# The lock must carry no environment markers. uv evaluates and strips them
# against the pinned target, but pip's --platform does not affect marker
# evaluation at install time -- only --python-version does. A marker reaching
# the lock would therefore be resolved against the installing host, which is
# exactly the host-dependence this whole mechanism removes.
if grep -v '^[[:space:]]*#' "${LOCK_FILE}" | grep -q ';'; then
  echo "error: requirements.lock contains an environment marker; markers are resolved" >&2
  echo "       against the installing host, not the pinned target" >&2
  grep -n -v '^[[:space:]]*#' "${LOCK_FILE}" | grep ';' >&2
  exit 1
fi

"${ROOT_DIR}/scripts/compile_requirements.sh" "${COMPILED_LOCK}"

if ! cmp -s "${LOCK_FILE}" "${COMPILED_LOCK}"; then
  echo "error: requirements.lock is out of date; run ./scripts/compile_requirements.sh" >&2
  diff -u "${LOCK_FILE}" "${COMPILED_LOCK}" || true
  exit 1
fi

echo "requirements.lock is up to date."
