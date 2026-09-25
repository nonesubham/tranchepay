#!/usr/bin/env bash
#
# Run the opt-in Razorpay sandbox integration suite.
#
#   ./run_integration.sh                # every integration test
#   ./run_integration.sh -k exact       # anything else is forwarded to pytest
#
# Credentials come from the environment, or from a gitignored .env file
# (copy .env.example and fill in your TEST keys). No key is ever hardcoded.
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

if [[ -z "${RAZORPAY_KEY_ID:-}" || -z "${RAZORPAY_KEY_SECRET:-}" ]]; then
  echo "error: RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are not set." >&2
  echo "       cp .env.example .env  and fill in your Razorpay TEST keys." >&2
  exit 1
fi

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x .venv/bin/python ]]; then
    PYTHON=.venv/bin/python
  else
    PYTHON=python3
  fi
fi

echo "==> live Razorpay sandbox tests, key ${RAZORPAY_KEY_ID:0:12}..."
exec "$PYTHON" -m pytest tests/test_integration_razorpay.py -v -m integration "$@"
