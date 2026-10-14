#!/usr/bin/env bash
set -eu
submission_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$submission_dir/agent.py" "$@"
