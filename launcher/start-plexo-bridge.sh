#!/bin/sh
# Starts the Plexo bridge in the background and opens its status page (macOS and Linux).
exec python3 "$(dirname "$0")/bridge/plexo_fdm.py" ui
