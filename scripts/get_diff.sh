#!/usr/bin/env bash
# get_diff.sh — Stage 0: produce a git diff for the pipeline.
#
# Usage:
#   scripts/get_diff.sh <repo> <base_ref> <head_ref> [-- <path>...]
#
# Examples:
#   scripts/get_diff.sh target-repo/galaxium-travels main breaking-change-demo
#   scripts/get_diff.sh target-repo/galaxium-travels main breaking-change-demo -- booking_system_backend/server.py
#
# Writes the unified diff to stdout. Caller redirects to a file.

set -euo pipefail

REPO="${1:?Usage: get_diff.sh <repo> <base_ref> <head_ref> [-- <path>...]}"
BASE="${2:?missing base_ref}"
HEAD="${3:?missing head_ref}"
shift 3

git -C "$REPO" diff "${BASE}..${HEAD}" "$@"
