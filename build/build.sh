#!/bin/sh
# Packs the add-on into plexo-multi-network.fda (a zip with manifest.json at its root), which
# FDM installs from its Add-ons menu.
set -e
root="$(cd "$(dirname "$0")/.." && pwd)"
dist="$root/dist/plexo-multi-network.fda"
mkdir -p "$root/dist"
rm -f "$dist"
cd "$root/addon"
zip -r -X -q "$dist" . -x '*__pycache__*' -x '*.pyc' -x '.DS_Store'
echo "Built $dist"
