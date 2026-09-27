"""Stage 1 — Diff Parser.

Reads a unified git diff (stdin or file) and extracts a structured
ContractDelta describing what changed about each modified function's
observable behaviour: error handling, response shape, status codes.

Usage:
    git -C <repo> diff <base>..<head> -- <file> | python scripts/parse_diff.py
    python scripts/parse_diff.py --diff-file <path>

Output (JSON to stdout):
    {
      "changed_functions": [
        {
          "file": "booking_system_backend/server.py",
          "function": "create_hold",
          "endpoint": "POST /quotes/{quote_id}/holds",
          "contract_delta": {
            "before": { "error_handling": [...], "response_shape": [...], "status_codes": [...] },
            "after":  { "error_handling": [...], "response_shape": [...], "status_codes": [...] },
            "summary": "..."
          },
          "raw_hunk": "..."
        }
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Iterator


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ContractSide:
    error_handling: list[str] = field(default_factory=list)
    response_shape: list[str] = field(default_factory=list)
    status_codes: list[str] = field(default_factory=list)


@dataclass
class ContractDelta:
    before: ContractSide = field(default_factory=ContractSide)
    after: ContractSide = field(default_factory=ContractSide)
    summary: str = ""


@dataclass
class ChangedFunction:
    file: str
    function: str
    endpoint: str
    contract_delta: ContractDelta
    raw_hunk: str


# ---------------------------------------------------------------------------
# Diff parsing helpers
# ---------------------------------------------------------------------------

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_DIFF_FILE   = re.compile(r"^diff --git a/(.+) b/\1")
_FUNC_DEF    = re.compile(r"^[ \-\+](?:async )?def (\w+)\s*\(")
_DECORATOR   = re.compile(r"^[ \-\+]@app\.(get|post|put|patch|delete|head)\(['\"]([^'\"]+)['\"]")

# Patterns we care about for contract extraction
_PATTERNS = {
    "raises_http_exception": re.compile(r"raise HTTPException"),
    "returns_error_dict":    re.compile(r'return\s*\{[^}]*["\']error["\']'),
    "returns_detail_dict":   re.compile(r'return\s*\{[^}]*["\']detail["\']'),
    "except_http_error":     re.compile(r"except httpx\.HTTPError"),
    "except_http_status":    re.compile(r"except httpx\.HTTPStatusError"),
    "raise_for_status":      re.compile(r"\.raise_for_status\(\)"),
    "return_json":           re.compile(r"return response\.json\(\)"),
    "status_code_ref":       re.compile(r"status_code=(\S+)"),
}


def _iter_hunks(lines: list[str]) -> Iterator[tuple[str, list[str]]]:
    """Yield (hunk_header, hunk_lines) for every hunk in the diff."""
    current_header = ""
    current_lines: list[str] = []
    for line in lines:
        if _HUNK_HEADER.match(line):
            if current_lines:
                yield current_header, current_lines
            current_header = line.rstrip()
            current_lines = []
        elif current_header:
            current_lines.append(line)
    if current_lines:
        yield current_header, current_lines


def _extract_contract_side(lines: list[str]) -> ContractSide:
    side = ContractSide()
    for ln in lines:
        body = ln[1:]  # strip leading +/-/ 
        if _PATTERNS["raises_http_exception"].search(body):
            m = _PATTERNS["status_code_ref"].search(body)
            code = m.group(1).rstrip(",)") if m else "?"
            side.error_handling.append(f"raise HTTPException(status_code={code})")
            side.status_codes.append(code)
        if _PATTERNS["returns_error_dict"].search(body):
            side.error_handling.append('return {"error": ...}')
            side.response_shape.append('{"error": "..."}')
            side.status_codes.append("200 (implicit)")
        if _PATTERNS["returns_detail_dict"].search(body):
            side.response_shape.append('{"detail": "..."}')
        if _PATTERNS["except_http_error"].search(body):
            side.error_handling.append("except httpx.HTTPError")
        if _PATTERNS["except_http_status"].search(body):
            side.error_handling.append("except httpx.HTTPStatusError")
        if _PATTERNS["raise_for_status"].search(body):
            side.error_handling.append("response.raise_for_status()")
        if _PATTERNS["return_json"].search(body):
            side.response_shape.append("response.json() on success")
    return side


def _build_summary(before: ContractSide, after: ContractSide) -> str:
    parts = []
    before_errors = set(before.error_handling)
    after_errors  = set(after.error_handling)
    removed = before_errors - after_errors
    added   = after_errors  - before_errors

    if 'return {"error": ...}' in removed and any("HTTPException" in e for e in added):
        parts.append(
            "Error path changed: previously returned HTTP 200 with body key "
            "'error'; now raises HTTPException with body key 'detail' and a "
            "real status code."
        )
    if removed - {'return {"error": ...}'}:
        parts.append(f"Removed error handling: {', '.join(sorted(removed - {'return {\"error\": ...}'}))}.")
    if added - {e for e in added if "HTTPException" in e}:
        parts.append(f"Added error handling: {', '.join(sorted(added - {e for e in added if 'HTTPException' in e}))}.")

    before_shapes = set(before.response_shape)
    after_shapes  = set(after.response_shape)
    if before_shapes != after_shapes:
        parts.append(
            f"Response shape on error: was {before_shapes or '{}'}, "
            f"now {after_shapes or '{}'}."
        )

    if not parts:
        parts.append("Implementation change detected; contract impact unclear — review manually.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Main parsing logic
# ---------------------------------------------------------------------------

def parse_diff(raw: str) -> dict:
    lines = raw.splitlines(keepends=True)
    results: list[dict] = []

    current_file = ""
    current_endpoint = ""
    current_method_prefix = ""

    for i, line in enumerate(lines):
        m = _DIFF_FILE.match(line)
        if m:
            current_file = m.group(1)
            current_endpoint = ""
            current_method_prefix = ""
            continue

        dm = _DECORATOR.match(line.lstrip("+-"))
        if dm:
            current_method_prefix = dm.group(1).upper()
            current_endpoint = f"{current_method_prefix} {dm.group(2)}"
            continue

    # Second pass: process by hunk
    current_file = ""
    current_endpoint = ""
    current_function = ""

    i = 0
    while i < len(lines):
        line = lines[i]

        m = _DIFF_FILE.match(line)
        if m:
            current_file = m.group(1)
            current_endpoint = ""
            current_function = ""
            i += 1
            continue

        # Track decorators in context lines
        dm = _DECORATOR.match(line.lstrip("+-").rstrip())
        if dm:
            method = dm.group(1).upper()
            current_endpoint = f"{method} {dm.group(2)}"
            i += 1
            continue

        if _HUNK_HEADER.match(line):
            hunk_header = line.rstrip()
            hunk_lines = []
            i += 1
            while i < len(lines) and not _HUNK_HEADER.match(lines[i]) and not _DIFF_FILE.match(lines[i]):
                hunk_lines.append(lines[i])
                i += 1

            # Scan hunk: pair each decorator with the def that immediately follows it.
            # A decorator seen *after* the function body belongs to the *next* function.
            pending_endpoint = current_endpoint
            for hl in hunk_lines:
                dm2 = _DECORATOR.match(hl.lstrip("+-").rstrip())
                if dm2:
                    method = dm2.group(1).upper()
                    pending_endpoint = f"{method} {dm2.group(2)}"

                fm = _FUNC_DEF.match(hl)
                if fm:
                    current_function = fm.group(1)
                    current_endpoint = pending_endpoint  # lock in the decorator seen just before this def

            removed_lines = [l for l in hunk_lines if l.startswith("-")]
            added_lines   = [l for l in hunk_lines if l.startswith("+")]

            # Only emit if there are actual changes
            if not removed_lines and not added_lines:
                continue

            before = _extract_contract_side(removed_lines)
            after  = _extract_contract_side(added_lines)

            # Skip hunks with no contract-relevant changes
            all_before = before.error_handling + before.response_shape
            all_after  = after.error_handling  + after.response_shape
            if not all_before and not all_after:
                continue

            summary = _build_summary(before, after)
            delta = ContractDelta(before=before, after=after, summary=summary)

            results.append(asdict(ChangedFunction(
                file=current_file,
                function=current_function,
                endpoint=current_endpoint,
                contract_delta=delta,
                raw_hunk=hunk_header + "\n" + "".join(hunk_lines),
            )))
            continue

        i += 1

    return {"changed_functions": results}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Parse a git diff into a contract delta.")
    parser.add_argument("--diff-file", help="Read diff from file instead of stdin")
    parser.add_argument("--indent", type=int, default=2, help="JSON indent width")
    args = parser.parse_args()

    if args.diff_file:
        with open(args.diff_file, encoding="utf-8") as fh:
            raw = fh.read()
    else:
        raw = sys.stdin.read()

    result = parse_diff(raw)
    print(json.dumps(result, indent=args.indent))


if __name__ == "__main__":
    main()
