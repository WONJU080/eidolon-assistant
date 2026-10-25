#!/bin/sh
# Merge selected API keys from an existing .env file into config/local.env.
# Values are never printed. The destination is chmod 600.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

if [ "$#" -ne 1 ]; then
  echo "Usage: ./scripts/import_keys.sh /path/to/your.env" >&2
  exit 2
fi

src=$1
if [ ! -f "$src" ]; then
  echo "Not a file: $src" >&2
  exit 1
fi

mkdir -p config
dst=config/local.env
tmp=$(mktemp "${TMPDIR:-/tmp}/eidolon-keys.XXXXXX")
trap 'rm -f "$tmp" "$tmp.next"' EXIT INT TERM

if [ -f "$dst" ]; then
  cp "$dst" "$tmp"
else
  : > "$tmp"
fi

count=0
for key in CHAT_LLM_BASE_URL CHAT_LLM_MODEL CHAT_LLM_API_KEY DASHSCOPE_API_KEY FISH_AUDIO_API_KEY; do
  line=$(grep -E "^${key}=" "$src" | tail -n 1 || true)
  if [ -n "$line" ]; then
    grep -vE "^${key}=" "$tmp" > "$tmp.next" || true
    printf '%s\n' "$line" >> "$tmp.next"
    mv "$tmp.next" "$tmp"
    count=$((count + 1))
  fi
done

if [ "$count" -eq 0 ]; then
  echo "No known keys found in $src" >&2
  echo "Expected lines such as CHAT_LLM_API_KEY=..., DASHSCOPE_API_KEY=..., FISH_AUDIO_API_KEY=..." >&2
  exit 1
fi

mv "$tmp" "$dst"
chmod 600 "$dst"
echo "Imported $count key(s) into config/local.env (values not printed)."
