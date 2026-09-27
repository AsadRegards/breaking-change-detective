"""Pre-filter between stage 2 (consumer discovery) and stage 3 (LLM reasoning).

Drops candidates that are definitely not actionable code consumers so we don't
waste subagent calls on documentation noise.

Two filtering passes:

Pass 1 — File-type filter:
    Keep only candidates whose file extension is one of:
        .py  .ts  .tsx  .js  .jsx  .java
    Drops: .md, .yml, .yaml, .sh, .json, .txt, and anything else.

Pass 2 — Comment/docstring filter:
    Within kept files, drop matches that fall on a line that is purely a
    comment or inside a block docstring/comment.

    Python:   lines whose stripped content starts with #  or  \"\"\"  or  '''
    TS/JS:    lines whose stripped content starts with //  or  /*  or  *
    Java:     lines whose stripped content starts with //  or  /*  or  *

Usage (pipe from find_consumers.py):
    ... | python scripts/find_consumers.py | python scripts/prefilter_consumers.py

Or with files:
    python scripts/prefilter_consumers.py --input consumers.json [--output filtered.json]

Prints a before/after summary to stderr, JSON result to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CODE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".java"}

# Prefixes that mark a line as a pure comment in each language family.
# We only check the stripped line — blank lines and code lines won't match.
_COMMENT_PREFIXES: dict[str, list[str]] = {
    ".py":   ["#", '"""', "'''"],
    ".ts":   ["//", "/*", "*"],
    ".tsx":  ["//", "/*", "*"],
    ".js":   ["//", "/*", "*"],
    ".jsx":  ["//", "/*", "*"],
    ".java": ["//", "/*", "*"],
}


# ---------------------------------------------------------------------------
# Filter logic
# ---------------------------------------------------------------------------

def _is_comment_line(stripped: str, ext: str) -> bool:
    """Return True if the stripped line looks like a pure comment/docstring line."""
    prefixes = _COMMENT_PREFIXES.get(ext, [])
    return any(stripped.startswith(p) for p in prefixes)


def _read_line(file_path: str, lineno: int, repo_root: str) -> str:
    """Read a single line (1-based) from a file relative to repo_root."""
    try:
        p = Path(repo_root) / file_path
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if 1 <= lineno <= len(lines):
            return lines[lineno - 1].strip()
    except OSError:
        pass
    return ""


def prefilter(consumers: list[dict], repo_root: str) -> tuple[list[dict], dict]:
    """
    Return (filtered_list, stats_dict).
    stats_dict contains counts for the summary.
    """
    total = len(consumers)
    after_ext: list[dict] = []
    after_comment: list[dict] = []

    dropped_ext = 0
    dropped_comment = 0

    for c in consumers:
        ext = Path(c["file"]).suffix.lower()

        # Pass 1: extension
        if ext not in _CODE_EXTENSIONS:
            dropped_ext += 1
            continue
        after_ext.append(c)

    for c in after_ext:
        ext = Path(c["file"]).suffix.lower()

        # Pass 2: comment/docstring — re-read the actual line from disk
        # (the snippet in the consumer record is already stripped, but we
        # re-read to be safe against truncation in the snippet field)
        line_text = _read_line(c["file"], c["line"], repo_root)
        if not line_text:
            # Fallback to snippet if we can't read the file
            line_text = c.get("snippet", "")

        if _is_comment_line(line_text, ext):
            dropped_comment += 1
            continue
        after_comment.append(c)

    stats = {
        "before": total,
        "after_extension_filter": len(after_ext),
        "after_comment_filter": len(after_comment),
        "dropped_non_code_files": dropped_ext,
        "dropped_comment_lines": dropped_comment,
        "final": len(after_comment),
    }
    return after_comment, stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-filter stage-2 consumer candidates before stage-3 reasoning."
    )
    parser.add_argument("--input",  help="Read consumers JSON from file instead of stdin")
    parser.add_argument("--output", help="Write filtered JSON to file instead of stdout")
    parser.add_argument(
        "--repo",
        default="target-repo/galaxium-travels",
        help="Repo root used to read source lines for comment detection",
    )
    parser.add_argument("--indent", type=int, default=2)
    args = parser.parse_args()

    if args.input:
        with open(args.input, encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = json.load(sys.stdin)

    consumers = data.get("consumers", [])
    filtered, stats = prefilter(consumers, args.repo)

    # Always print the summary to stderr so it's visible even when stdout is piped
    print(
        f"[prefilter] {stats['before']} candidates in  →  "
        f"{stats['after_extension_filter']} after extension filter  →  "
        f"{stats['final']} after comment filter  "
        f"(dropped {stats['dropped_non_code_files']} non-code, "
        f"{stats['dropped_comment_lines']} comment lines)",
        file=sys.stderr,
    )

    result = {"consumers": filtered, "prefilter_stats": stats}

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=args.indent)
        print(f"[prefilter] Written to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(result, indent=args.indent))


if __name__ == "__main__":
    main()
