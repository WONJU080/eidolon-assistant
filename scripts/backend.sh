#!/bin/sh
# Run only the local backend; open the printed URL in any browser.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

./scripts/check.sh
exec .venv/bin/python -m eidolon_assistant serve "$@"
