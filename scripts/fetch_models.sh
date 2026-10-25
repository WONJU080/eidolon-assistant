#!/bin/sh
# Download the local KWS model used for wake-word detection.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

name="sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
target="models/$name"

if [ -f "$target/tokens.txt" ]; then
  echo "KWS model already present: $target"
  exit 0
fi

tmp=$(mktemp -d "${TMPDIR:-/tmp}/eidolon-kws.XXXXXX")
trap 'rm -rf "$tmp"' EXIT INT TERM

mkdir -p models
direct="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$name.tar.bz2"
proxy="https://gh-proxy.com/$direct"
ok=0

for url in "$direct" "$proxy"; do
  echo "Downloading $url"
  if curl -fL --retry 3 --connect-timeout 20 -o "$tmp/kws.tar.bz2" "$url"; then
    ok=1
    break
  fi
  echo "Download failed; trying next mirror..."
done

if [ "$ok" -ne 1 ]; then
  echo "Could not download the KWS model. Download it manually:" >&2
  echo "  $direct" >&2
  echo "  $proxy" >&2
  echo "then extract it under models/ as $name/" >&2
  exit 1
fi

tar -xjf "$tmp/kws.tar.bz2" -C "$tmp"
if [ ! -d "$tmp/$name" ]; then
  echo "Unexpected archive layout; expected $name/ at the root" >&2
  exit 1
fi

rm -rf "$target"
mv "$tmp/$name" "$target"

if [ ! -f "$target/tokens.txt" ]; then
  echo "Model extracted but tokens.txt is missing" >&2
  exit 1
fi

echo "KWS model ready: $target"
