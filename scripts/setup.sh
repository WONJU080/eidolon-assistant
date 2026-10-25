#!/bin/sh
# Manual, idempotent setup for running Eidolon Assistant from source.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

echo "== Eidolon Assistant setup =="
echo "Project: $root"

case "$(uname -s)" in
  Darwin) ;;
  *) echo "This project currently supports macOS only." >&2; exit 1 ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 was not found. Install Python 3.12 or newer." >&2
  exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "Python 3.12 or newer is required (found $(python3 --version 2>&1))." >&2
  exit 1
fi

if ! command -v node >/dev/null 2>&1; then
  echo "node was not found. Install Node.js 20 or newer." >&2
  exit 1
fi
if ! node -e 'const v=process.versions.node.split(".").map(Number); process.exit(v[0] >= 20 ? 0 : 1)'; then
  echo "Node.js 20 or newer is required (found $(node --version))." >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm was not found. Install Node.js 20 or newer." >&2
  exit 1
fi

if [ ! -e "/Applications/DeepSeek Harness.app" ]; then
  echo
  echo "WARNING: /Applications/DeepSeek Harness.app was not found."
  echo "         Chat and voice can start, but computer-operation tasks need Harness."
  echo "         Install it and sign in, then check the path in config/settings.toml."
  echo
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating Python virtual environment..."
  python3 -m venv .venv
fi

echo "Installing backend dependencies..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e backend

if [ ! -d desktop/node_modules ]; then
  echo "Installing desktop dependencies..."
  (cd desktop && npm ci)
fi

echo "Building the desktop renderer and main process..."
(cd desktop && npm run build)

if [ ! -f "models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/tokens.txt" ]; then
  ./scripts/fetch_models.sh
fi

if [ ! -f config/local.env ]; then
  echo "Creating config/local.env from the example..."
  cp config/local.env.example config/local.env
  chmod 600 config/local.env
fi

# Import the bundled default character into the ignored runtime directory.
.venv/bin/python - <<'PY'
from pathlib import Path
from eidolon_assistant.characters import import_archive
from eidolon_assistant.config import CONFIG_DIR

package = Path("characters/ramlethal.eidolon")
target = CONFIG_DIR / "characters" / "ramlethal" / "character.toml"
if target.exists():
    print("Default character already present:", target)
elif not package.exists():
    raise SystemExit(f"Missing character package: {package}")
else:
    import_archive(CONFIG_DIR, package.read_bytes())
    print("Imported default character:", CONFIG_DIR / "characters" / "ramlethal")
PY

keys=$(grep -cE '^(CHAT_LLM_API_KEY|DASHSCOPE_API_KEY|FISH_AUDIO_API_KEY)=.+' config/local.env 2>/dev/null || true)
if [ "${keys:-0}" -lt 3 ]; then
  echo
  echo "Next: put your API keys in config/local.env"
  echo "  - CHAT_LLM_API_KEY  (DeepSeek or OpenAI-compatible)"
  echo "  - DASHSCOPE_API_KEY (Alibaba Cloud Bailian / DashScope)"
  echo "  - FISH_AUDIO_API_KEY (Fish Audio)"
  echo "Or import an existing .env file:"
  echo "  ./scripts/import_keys.sh /path/to/your.env"
  echo
else
  echo "Keys found in config/local.env."
fi

echo "Setup complete."
echo "Start the app with: ./scripts/run.sh"
echo "Development mode:  ./scripts/dev.sh"
echo "Backend only:      ./scripts/backend.sh --port 8799"
