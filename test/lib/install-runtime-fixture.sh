#!/usr/bin/env bash
set -euo pipefail

ROOT="$1"
PREFIX="$2"
SHARE="$PREFIX/share/harness-launcher"
mkdir -p "$SHARE" "$PREFIX/bin"
for source in "$ROOT"/bin/*; do
  [[ -f "$source" ]] || continue
  cp "$source" "$SHARE/"
done
chmod 755 "$SHARE"/*
for file in harness-auto harness-codex harness-exec harness-headless harness-herdr-web harness-paseo harness-profile harness-preserve; do
  ln -s "../share/harness-launcher/$file" "$PREFIX/bin/$file"
done
