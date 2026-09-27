#!/usr/bin/env bash
# run_bob.sh — Single-command entry point for the Breaking Change Detective pipeline.
#
# Usage:
#   scripts/run_bob.sh [options]
#
# Options (all optional — defaults target the demo repo):
#   --repo        PATH           Target repo root             (default: target-repo/galaxium-travels)
#   --base        REF            Base git ref                 (default: main)
#   --head        REF            Head git ref                 (default: breaking-change-demo)
#   --path-filter FILE [FILE...] Restrict diff to these paths (default: all files)
#   --output-dir  PATH           Where to write reports       (default: output/reports)
#   --output-format text|stream-json                          (default: text)
#   --api-key     KEY            LLM API key (or set BOB_API_KEY / OPENAI_API_KEY in env)
#   --api-url     URL            LLM chat completions URL     (default: OpenAI)
#   --model       NAME           LLM model name               (default: gpt-4o)
#   --workers     N              Parallel LLM calls           (default: 5)
#
# Examples:
#   # Full demo run (text output)
#   scripts/run_bob.sh
#
#   # Restrict to one file, stream-json output for CI
#   scripts/run_bob.sh \
#     --path-filter booking_system_backend/server.py \
#     --output-format stream-json
#
#   # Custom refs with explicit API key
#   scripts/run_bob.sh --base v1.2.0 --head v1.3.0 --api-key "$MY_KEY"

set -euo pipefail

# Resolve the directory containing this script so we can run from any CWD
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Find the Python interpreter
PY=""
for candidate in python3 python py; do
    if command -v "$candidate" &>/dev/null; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "ERROR: Python interpreter not found. Install Python 3.9+ and ensure it is on PATH." >&2
    exit 1
fi

# Pass all arguments straight through to run_pipeline.py
exec "$PY" "$SCRIPT_DIR/run_pipeline.py" "$@"
