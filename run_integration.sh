#!/usr/bin/env bash
#
# Run the opt-in live sandbox integration suites.
#
#   ./run_integration.sh                 # every suite whose credentials are set
#   ./run_integration.sh -k phonepe      # anything else is forwarded to pytest
#
# Credentials come from the environment, or from a gitignored .env file (copy
# .env.example and fill in your TEST keys). No key is ever hardcoded, and a
# suite whose credentials are absent is skipped rather than failed.
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

files=()
if [[ -n "${RAZORPAY_KEY_ID:-}" && -n "${RAZORPAY_KEY_SECRET:-}" ]]; then
  files+=("tests/test_integration_razorpay.py")
fi
if [[ -n "${PHONEPE_MERCHANT_ID:-}" && -n "${PHONEPE_SALT_KEY:-}" ]]; then
  files+=("tests/test_integration_phonepe.py")
fi

if [[ ${#files[@]} -eq 0 ]]; then
  echo "error: no sandbox credentials are set." >&2
  echo "       cp .env.example .env and fill in the Razorpay and/or PhonePe TEST keys." >&2
  exit 1
fi

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x .venv/bin/python ]]; then
    PYTHON=.venv/bin/python
  else
    PYTHON=python3
  fi
fi

echo "==> live sandbox tests: ${files[*]}"
exec "$PYTHON" -m pytest "${files[@]}" -v "$@"
