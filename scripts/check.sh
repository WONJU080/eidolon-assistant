#!/bin/sh
# Check the local source installation before starting the app.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
fail=0

missing() {
  echo "MISSING: $1"
  fail=1
}

if [ ! -x .venv/bin/python ]; then
  missing ".venv/bin/python (run ./scripts/setup.sh)"
elif ! .venv/bin/python -c 'import eidolon_assistant' >/dev/null 2>&1; then
  echo "MISSING: backend dependencies (run ./scripts/setup.sh)"
  fail=1
fi

if [ ! -f "models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/tokens.txt" ]; then
  missing "KWS model (run ./scripts/fetch_models.sh)"
fi

if [ ! -f config/local.env ]; then
  missing "config/local.env (run ./scripts/setup.sh, then fill in the keys)"
fi

if [ ! -f config/characters/ramlethal/character.toml ]; then
  missing "default character (run ./scripts/setup.sh)"
fi

if [ ! -f desktop/dist/renderer/index.html ]; then
  missing "desktop build (run ./scripts/setup.sh)"
fi

if [ "$fail" -ne 0 ]; then
  echo
  echo "Run ./scripts/setup.sh to create the missing pieces."
  exit 1
fi

echo "Basic checks passed."
