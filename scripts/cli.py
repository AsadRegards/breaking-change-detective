"""Breaking Change Detective — minimal CLI entry point.

Runs stages 1-2 (diff parsing + consumer discovery) in-process, then shells
out to the user's own local ``bob -p`` installation for stage-3 reasoning.

Usage:
    bcd analyze --repo <path> --base <ref> --head <ref> --path <file>

``bob`` must be on the user's PATH.  If it is not found the command exits with
a clear error message explaining how to install it.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.parse_diff import parse_diff
from scripts.find_consumers import find_consumers


# ---------------------------------------------------------------------------
# Stage 0 — git diff
# ---------------------------------------------------------------------------

def _get_diff(repo: str, base: str, head: str, path_filter: str | None) -> str:
    """Run ``git diff`` and return the raw unified diff string."""
    cmd = ["git", "-C", repo, "diff", f"{base}..{head}"]
    if path_filter:
        cmd += ["--", path_filter]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"error: git diff failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


# ---------------------------------------------------------------------------
# Stage 3 — shell out to bob -p
# ---------------------------------------------------------------------------

_BOB_PROMPT_TEMPLATE = """\
You are a breaking-change analysis agent. The following consumers were
discovered by static analysis.  For each one reason about whether it is
broken by the contract change described below, then return a short verdict
(BREAKS / BREAKS_SILENTLY / LATENT / INVESTIGATE / UNAFFECTED) with one or
two sentences of reasoning.

## Contract delta (stage 1)

{delta_json}

## Discovered consumers (stage 2)

{consumers_json}
"""


def _filter_consumers(consumers_list: list[dict]) -> list[dict]:
    """Cheap pre-filter: drop non-code files before spending a call on each one."""
    CODE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".java"}
    return [c for c in consumers_list if Path(c.get("file", "")).suffix in CODE_EXTENSIONS]


_CONSUMER_PROMPT_TEMPLATE = """\
You are a breaking-change analysis agent. Return ONLY valid JSON - no prose, no markdown fences.

## Contract change

{delta_json}

## Consumer to analyze

{consumer_json}

## Your task

Reason step by step about whether this consumer's behavior changes given the
contract change above, whether it is covered by an existing test, and whether
any break would be loud (an existing check/test fires) or silent.

Return exactly this JSON object and nothing else:
{{
  "consumer": "...", "file": "...", "line": ...,
  "verdict": "BREAKS|BREAKS_SILENTLY|LATENT|INVESTIGATE|UNAFFECTED",
  "coverage": "covered|uncovered", "loud_or_silent": "loud|silent|n/a",
  "reasoning": "two or three sentences"
}}
"""


# Matches the outermost {...} block in bob's stdout, which may also contain
# thinking steps, cost summaries, and tool-call headers as surrounding text.
_JSON_BLOCK_RE = re.compile(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}', re.DOTALL)

_VERDICT_KEYS = {"verdict", "consumer", "reasoning"}




def _extract_verdict(stdout: str) -> dict | None:
    """Pull a verdict JSON object out of bob's stdout, which may include
    transcript formatting, tool-call logs, and narration around the answer."""

    # If bob's output uses "Assistant (N)" turn markers, only look inside the
    # LAST assistant turn — avoids matching JSON-shaped text from earlier
    # tool-call arguments rather than the actual final answer.
    turns = re.split(r'\n─+\nAssistant \(\d+\)[^\n]*\n', stdout)
    search_text = turns[-1] if len(turns) > 1 else stdout

    # Strip a trailing "Task Summary" block if present
    search_text = re.split(r'\nTask Summary\n', search_text)[0]

    # Strip markdown fences
    stripped = search_text.strip()
    stripped = re.sub(r'^```[a-zA-Z]*\n?', '', stripped)
    stripped = re.sub(r'\n?```$', '', stripped.strip())

    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict) and "verdict" in obj:
            return obj
    except json.JSONDecodeError:
        pass

    # Fall back: scan the FULL original stdout for any {...} block (up to
    # 2 levels of nesting), preferring the LAST match — bob's real answer
    # typically comes after any tool-call JSON earlier in the transcript.
    matches = list(re.finditer(
        r'\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}', stdout, re.DOTALL
    ))
    for m in reversed(matches):
        try:
            obj = json.loads(m.group())
            if isinstance(obj, dict) and "verdict" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None

def _build_consumer_prompt(delta_json: str, consumer_json: str) -> str:
    return (
        "You are a breaking-change analysis agent. Return ONLY valid JSON - no prose, no markdown fences.\n\n"
        "## Contract change\n\n"
        f"{delta_json}\n\n"
        "## Consumer to analyze\n\n"
        f"{consumer_json}\n\n"
        "## Your task\n\n"
        "Reason step by step about whether this consumer's behavior changes given the\n"
        "contract change above, whether it is covered by an existing test, and whether\n"
        "any break would be loud (an existing check/test fires) or silent.\n\n"
        "Return exactly this JSON object and nothing else:\n"
        '{"consumer": "...", "file": "...", "line": 0, '
        '"verdict": "BREAKS|BREAKS_SILENTLY|LATENT|INVESTIGATE|UNAFFECTED", '
        '"coverage": "covered|uncovered", "loud_or_silent": "loud|silent|n/a", '
        '"reasoning": "two or three sentences"}'
    )

def _run_bob_reasoning(delta: dict, consumers_list: list[dict]) -> list[dict]:
    """One bob -p call per consumer, prompt text passed directly — no temp
    file, no workspace-sandbox issue."""
    bob_bin = shutil.which("bob")
    if bob_bin is None:
        print(
            "error: 'bob' was not found on your PATH.\n"
            "Install it and make sure 'bob' is on PATH, then re-run bcd.",
            file=sys.stderr,
        )
        sys.exit(1)

    filtered = _filter_consumers(consumers_list)
    print(f"[bcd] stage 3 - {len(filtered)} code consumer(s) after filtering "
          f"(from {len(consumers_list)} raw candidates)", file=sys.stderr)

    delta_json = json.dumps(delta, indent=2)
    results = []
    for i, consumer in enumerate(filtered, start=1):
        label = f"{consumer.get('file', '?')}:{consumer.get('line', '?')}"
        print(f"[bcd] stage 3 - analyzing {i}/{len(filtered)}: {label}", file=sys.stderr)

        prompt = _build_consumer_prompt(delta_json, json.dumps(consumer, indent=2))
        if i == 1:
            print(f"[bcd] DEBUG prompt length: {len(prompt)} chars", file=sys.stderr)
        result = subprocess.run([bob_bin, "-p", prompt], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[bcd]   warning: bob exited {result.returncode} for {label}", file=sys.stderr)
            continue

        verdict = _extract_verdict(result.stdout)
        if verdict is None:
            print(f"[bcd]   warning: could not parse verdict JSON for {label}; "
                  f"storing raw output", file=sys.stderr)
            results.append({"consumer": label, "raw_output": result.stdout})
        else:
            results.append(verdict)

    return results


# ---------------------------------------------------------------------------
# analyze sub-command
# ---------------------------------------------------------------------------

def _cmd_analyze(args: argparse.Namespace) -> None:
    repo = args.repo
    base = args.base
    head = args.head
    path_filter = args.path  # may be None

    # ------------------------------------------------------------------
    # Stage 0: raw diff
    # ------------------------------------------------------------------
    print("[bcd] stage 0 — git diff …", file=sys.stderr)
    raw_diff = _get_diff(repo, base, head, path_filter)
    if not raw_diff.strip():
        print(f"[bcd] no diff found between {base} and {head}", file=sys.stderr)
        sys.exit(0)

    # ------------------------------------------------------------------
    # Stage 1: parse diff → contract delta
    # ------------------------------------------------------------------
    print("[bcd] stage 1 — parsing diff …", file=sys.stderr)
    delta = parse_diff(raw_diff)
    n_functions = len(delta.get("changed_functions", []))
    print(f"[bcd] stage 1 done — {n_functions} changed function(s)", file=sys.stderr)

    if n_functions == 0:
        print("[bcd] no contract-relevant changes detected; nothing to analyse.", file=sys.stderr)
        sys.exit(0)

    # ------------------------------------------------------------------
    # Stage 2: find consumers
    # ------------------------------------------------------------------
    print("[bcd] stage 2 — discovering consumers …", file=sys.stderr)
    repo_root = Path(repo)
    consumers_list = find_consumers(delta, repo_root)
    consumers = {"consumers": consumers_list}
    print(
        f"[bcd] stage 2 done — {len(consumers_list)} candidate consumer(s) found",
        file=sys.stderr,
    )

    # ------------------------------------------------------------------
    # Stage 3: bob -p reasoning
    # ------------------------------------------------------------------
    print("[bcd] stage 3 - handing off to bob for reasoning...", file=sys.stderr)
    results = _run_bob_reasoning(delta, consumers_list)

    # ------------------------------------------------------------------
    # Summary table — printed to stdout for the human reading the terminal
    # ------------------------------------------------------------------
    _VERDICT_COLORS = {
        "BREAKS":           "\033[31m",   # red
        "BREAKS_SILENTLY":  "\033[33m",   # yellow/orange
        "LATENT":           "\033[35m",   # purple
        "INVESTIGATE":      "\033[34m",   # blue
        "UNAFFECTED":       "\033[32m",   # green
    }
    _RESET = "\033[0m"

    col_w = 42  # width of the file:line column
    print()
    print(f"{'FILE:LINE':<{col_w}}  {'VERDICT':<18}  REASONING")
    print("-" * (col_w + 2 + 18 + 2 + 72))
    for r in results:
        if "verdict" not in r:
            # unparsed fallback row
            loc = r.get("consumer", "?")
            print(f"{loc:<{col_w}}  {'(parse error)':<18}  (see raw_output in JSON)")
            continue
        loc = f"{r.get('file', '?')}:{r.get('line', '')}"
        verdict = r.get("verdict", "?")
        reasoning = r.get("reasoning", "")
        # Truncate reasoning to one line ~72 chars
        first_sentence = reasoning.split(". ")[0].rstrip(".")
        if len(first_sentence) > 72:
            first_sentence = first_sentence[:69] + "..."
        color = _VERDICT_COLORS.get(verdict, "")
        print(f"{loc:<{col_w}}  {color}{verdict:<18}{_RESET}  {first_sentence}")
    print()

    output_path = Path("output") / "bcd_report.json"
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"[bcd] full report → {output_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bcd",
        description="Breaking Change Detective — detect API-contract breakage across a repo.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser(
        "analyze",
        help="Run stages 1-2 (diff + consumer discovery) then hand off to bob for reasoning.",
    )
    analyze.add_argument("--repo", required=True, help="Path to the target git repository.")
    analyze.add_argument("--base", required=True, help="Base git ref (e.g. main, v1.0.0).")
    analyze.add_argument("--head", required=True, help="Head git ref (e.g. feature-branch, v1.1.0).")
    analyze.add_argument(
        "--path",
        default=None,
        metavar="FILE",
        help="Restrict the diff to this file path (relative to repo root).",
    )

    args = parser.parse_args()
    if args.command == "analyze":
        _cmd_analyze(args)


if __name__ == "__main__":
    main()
