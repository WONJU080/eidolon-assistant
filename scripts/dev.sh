#!/bin/sh
# Development loop: Vite renderer + esbuild main + Electron.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

./scripts/check.sh

if [ ! -d desktop/node_modules ]; then
  echo "Missing desktop/node_modules. Run ./scripts/setup.sh first." >&2
  exit 1
fi

cd desktop
# DSH exports ELECTRON_RUN_AS_NODE=1 into its children; a real Electron launch must not inherit it.
unset ELECTRON_RUN_AS_NODE
exec npm run dev
