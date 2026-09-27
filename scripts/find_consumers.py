"""Stage 2 — Consumer Discovery.

Given a contract delta (JSON from parse_diff.py), finds every call site in the
repo that could be affected by the changed function. Searches for:

  1. The HTTP endpoint path string (in test files, frontend HTTP clients, etc.)
  2. The Python function name (direct Python callers, MCP registrations)
  3. MCP/tool auto-registrations that would expose the function as a tool

Usage:
    python scripts/parse_diff.py --diff-file <diff> | python scripts/find_consumers.py --repo <path>
    python scripts/find_consumers.py --delta-file <json> --repo <path>

Output (JSON to stdout):
    {
      "consumers": [
        {
          "file": "e2e/test_holds.py",
          "line": 81,
          "match_type": "endpoint_path",
          "matched_term": "/quotes/{quote_id}/holds",
          "snippet": "...",
          "function_context": "test_hold_on_unknown_quote_returns_error",
          "changed_function": "create_hold",
          "endpoint": "POST /quotes/{quote_id}/holds"
        },
        ...
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Consumer:
    file: str           # path relative to repo root
    line: int           # 1-based
    match_type: str     # "endpoint_path" | "function_name" | "mcp_registration"
    matched_term: str   # the exact string we searched for
    snippet: str        # the matching line (stripped)
    function_context: str   # enclosing function/method name if detectable
    changed_function: str   # which changed function this relates to
    endpoint: str           # e.g. "POST /quotes/{quote_id}/holds"


# ---------------------------------------------------------------------------
# File-tree helpers
# ---------------------------------------------------------------------------

# Extensions to include in searches
_TEXT_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx",
    ".json", ".yaml", ".yml", ".sh", ".md",
}
# Directories to skip entirely
_SKIP_DIRS = {
    ".git", ".venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", "dist", "build",
}


def _iter_text_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if Path(fname).suffix in _TEXT_EXTENSIONS:
                yield Path(dirpath) / fname


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Context extraction
# ---------------------------------------------------------------------------

_PY_FUNC  = re.compile(r"^(?:async )?def (\w+)\s*\(")
_TS_FUNC  = re.compile(r"(?:^|\s)(?:async\s+)?(?:function\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s+)?\()")


def _enclosing_function(lines: list[str], line_idx: int, path: Path) -> str:
    """Walk backwards from line_idx to find the nearest enclosing function name."""
    suffix = path.suffix
    for i in range(line_idx, -1, -1):
        ln = lines[i]
        if suffix == ".py":
            m = _PY_FUNC.match(ln)
            if m:
                return m.group(1)
        elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
            m = _TS_FUNC.search(ln)
            if m:
                return m.group(1) or m.group(2) or ""
    return ""


# ---------------------------------------------------------------------------
# Endpoint path normalisation
# ---------------------------------------------------------------------------

def _endpoint_variants(endpoint_path: str) -> list[str]:
    """
    Return search variants for an endpoint path.
    e.g. "/quotes/{quote_id}/holds" → [
        "/quotes/{quote_id}/holds",   # literal template
        "/quotes/",                   # prefix (catches dynamic calls)
        "holds",                      # terminal segment
    ]
    Also produce a regex-friendly version replacing {param} with [^/]+.
    """
    variants = [endpoint_path]
    segments = [s for s in endpoint_path.split("/") if s]
    if len(segments) >= 2:
        # prefix up to first path param or last fixed segment before param
        prefix_parts = []
        for seg in segments:
            if seg.startswith("{"):
                break
            prefix_parts.append(seg)
        if prefix_parts:
            variants.append("/" + "/".join(prefix_parts) + "/")
        # terminal segment
        last_fixed = [s for s in segments if not s.startswith("{")]
        if last_fixed:
            variants.append(last_fixed[-1])
    return variants


# ---------------------------------------------------------------------------
# MCP registration detection
# ---------------------------------------------------------------------------

_MCP_PATTERNS = [
    re.compile(r"FastApiMCP\s*\("),
    re.compile(r"FastMCP\s*\("),
    re.compile(r"mcp\s*=\s*FastApiMCP"),
]

# Pattern to detect "mcp.mount(app)" or "app.mount(mcp_router)" style calls.
# Checked separately to avoid false-positives on unrelated .mount() calls.
_MCP_MOUNT_PATTERN = re.compile(r"\.mount\s*\(")

# Detects the FastAPI app object name from lines like:
#   app = FastAPI(...)
_APP_DEF_PATTERN = re.compile(r"^(\w+)\s*=\s*FastAPI\s*\(")


def _app_names_in_file(lines: list[str]) -> set[str]:
    """Return all names bound to FastAPI() instances in this file."""
    names: set[str] = set()
    for ln in lines:
        m = _APP_DEF_PATTERN.match(ln.strip())
        if m:
            names.add(m.group(1))
    return names


def _mcp_wraps_same_app(line: str, app_names: set[str]) -> bool:
    """
    Return True when a line that contains an MCP pattern references one of the
    known app-object names.  This prevents flagging MCP mounts from unrelated
    FastAPI apps in a multi-app repo.

    If we cannot identify any app names (empty set), we fall back to True so
    we don't silently miss a registration in an unusual layout.
    """
    if not app_names:
        return True
    return any(name in line for name in app_names)


def _is_mcp_registration(line: str, app_names: set[str]) -> bool:
    if any(p.search(line) for p in _MCP_PATTERNS):
        return _mcp_wraps_same_app(line, app_names)
    if _MCP_MOUNT_PATTERN.search(line):
        # .mount() is generic; only flag if it also references a known app name
        return _mcp_wraps_same_app(line, app_names)
    return False


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def find_consumers(delta: dict, repo_root: Path) -> list[dict]:
    consumers: list[Consumer] = []
    seen: set[tuple[str, int]] = set()  # (rel_file, line) dedup

    for cf in delta.get("changed_functions", []):
        func_name: str = cf["function"]
        endpoint: str  = cf["endpoint"]       # e.g. "POST /quotes/{quote_id}/holds"
        endpoint_path  = endpoint.split(" ", 1)[1] if " " in endpoint else endpoint

        ep_variants    = _endpoint_variants(endpoint_path)

        for fpath in _iter_text_files(repo_root):
            rel = str(fpath.relative_to(repo_root)).replace("\\", "/")
            lines = _read_lines(fpath)
            # Identify FastAPI app names once per file so MCP check is app-scoped
            app_names = _app_names_in_file(lines) if fpath.suffix == ".py" else set()

            for idx, raw_line in enumerate(lines):
                lineno = idx + 1
                stripped = raw_line.strip()

                # --- MCP registration ---
                if _is_mcp_registration(stripped, app_names):
                    key = (rel, lineno)
                    if key not in seen:
                        seen.add(key)
                        consumers.append(Consumer(
                            file=rel,
                            line=lineno,
                            match_type="mcp_registration",
                            matched_term="FastApiMCP / FastMCP",
                            snippet=stripped[:200],
                            function_context=_enclosing_function(lines, idx, fpath),
                            changed_function=func_name,
                            endpoint=endpoint,
                        ))
                    continue

                # --- Endpoint path variants ---
                for variant in ep_variants:
                    if variant in stripped:
                        key = (rel, lineno)
                        if key not in seen:
                            seen.add(key)
                            consumers.append(Consumer(
                                file=rel,
                                line=lineno,
                                match_type="endpoint_path",
                                matched_term=variant,
                                snippet=stripped[:200],
                                function_context=_enclosing_function(lines, idx, fpath),
                                changed_function=func_name,
                                endpoint=endpoint,
                            ))
                        break  # only report once per line

                if (rel, lineno) in seen:
                    continue  # already matched above

                # --- Python function name (only in .py files, skip the definition itself) ---
                if fpath.suffix == ".py" and func_name in stripped:
                    # Skip the definition line itself
                    if re.match(rf"(?:async )?def {re.escape(func_name)}\s*\(", stripped):
                        continue
                    # Skip decorator lines above the definition
                    if stripped.startswith("@"):
                        continue
                    key = (rel, lineno)
                    if key not in seen:
                        seen.add(key)
                        consumers.append(Consumer(
                            file=rel,
                            line=lineno,
                            match_type="function_name",
                            matched_term=func_name,
                            snippet=stripped[:200],
                            function_context=_enclosing_function(lines, idx, fpath),
                            changed_function=func_name,
                            endpoint=endpoint,
                        ))

    return [asdict(c) for c in consumers]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Discover consumers of changed functions.")
    parser.add_argument("--delta-file", help="Read contract delta JSON from file instead of stdin")
    parser.add_argument(
        "--repo",
        default="target-repo/galaxium-travels",
        help="Path to the repository root to search (default: target-repo/galaxium-travels)",
    )
    parser.add_argument("--indent", type=int, default=2)
    args = parser.parse_args()

    if args.delta_file:
        with open(args.delta_file, encoding="utf-8") as fh:
            delta = json.load(fh)
    else:
        delta = json.load(sys.stdin)

    repo_root = Path(args.repo)
    if not repo_root.exists():
        print(f"ERROR: repo path does not exist: {repo_root}", file=sys.stderr)
        sys.exit(1)

    result = {"consumers": find_consumers(delta, repo_root)}
    print(json.dumps(result, indent=args.indent))


if __name__ == "__main__":
    main()
