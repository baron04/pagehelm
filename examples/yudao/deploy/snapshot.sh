#!/bin/sh
# Explicit project/socket, no implicit current-instance target.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$HERE/snapshot.py" "$@"
