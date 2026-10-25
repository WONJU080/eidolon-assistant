#!/bin/sh
# Run the backend tests, TypeScript check, and desktop build.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

if [ ! -x .venv/bin/python ]; then
  echo "Missing .venv. Run ./scripts/setup.sh first." >&2
  exit 1
fi
if [ ! -d desktop/node_modules ]; then
  echo "Missing desktop/node_modules. Run ./scripts/setup.sh first." >&2
  exit 1
fi

if ! .venv/bin/python -m pytest --version >/dev/null 2>&1; then
  .venv/bin/pip install pytest
fi

echo "== backend tests =="
.venv/bin/python -m pytest backend/tests -q

echo "== TypeScript check =="
(cd desktop && npx tsc --noEmit)

echo "== desktop build =="
(cd desktop && npm run build)

echo "All tests/build checks passed."
