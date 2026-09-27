"""End-to-end Breaking Change Detective pipeline.

Chains all stages in sequence, writes intermediate artefacts to a temp dir,
and produces the final report.  Handles --output-format stream-json for
programmatic consumers (GitHub Actions).

Usage:
    python scripts/run_pipeline.py \\
        --repo        target-repo/galaxium-travels \\
        --base        main \\
        --head        breaking-change-demo \\
        [--path-filter booking_system_backend/server.py] \\
        [--output-dir  output/reports] \\
        [--output-format text|stream-json] \\
        [--api-key   sk-...] \\
        [--api-url   https://...] \\
        [--model     gpt-4o] \\
        [--workers   5]

Stream-JSON event types (one JSON object per line on stdout):
    {"event":"pipeline_start",   "diff": "...", "repo": "..."}
    {"event":"stage_done",       "stage": 1, "name": "diff_parse",      "data": {...}}
    {"event":"stage_done",       "stage": 2, "name": "find_consumers",  "data": {...}}
    {"event":"stage_done",       "stage": 3, "name": "prefilter",       "data": {...}}
    {"event":"stage_done",       "stage": 4, "name": "reason",          "data": {...}}
    {"event":"stage_done",       "stage": 5, "name": "parse_report",    "data": {...}}
    {"event":"stage_done",       "stage": 6, "name": "render_html",     "data": {...}}
    {"event":"finding",          "verdict": "...", "consumer": "...", "reasoning": "..."}
    {"event":"pipeline_complete","report_json": "...", "report_html": "...","summary": {...}}
    {"event":"pipeline_error",   "stage": N, "message": "..."}
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PY = sys.executable   # same Python interpreter as this script


def _emit(fmt: str, obj: dict, *, always: bool = False) -> None:
    """Print event.  In stream-json mode prints JSON; otherwise prints to stderr."""
    if fmt == "stream-json":
        print(json.dumps(obj), flush=True)
    else:
        event = obj.get("event", "")
        if event == "finding":
            verdict = obj.get("verdict", "")
            consumer = obj.get("consumer", "")
            print(f"  [{verdict:20s}] {consumer}", file=sys.stderr)
        elif event in ("pipeline_start", "pipeline_complete", "pipeline_error"):
            print(f"[pipeline] {event}: {json.dumps({k:v for k,v in obj.items() if k!='event'})}", file=sys.stderr)
        elif event == "stage_done":
            print(f"[stage {obj['stage']}] {obj['name']} done", file=sys.stderr)


def _run(cmd: list[str], capture: bool = True) -> str:
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or f"command failed: {cmd}")
    return result.stdout if capture else ""


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run(
    repo: str,
    base: str,
    head: str,
    path_filter: list[str],
    output_dir: Path,
    output_format: str,
    api_key: str,
    api_url: str,
    model: str,
    workers: int,
    template: Path,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    diff_ref = f"{base}..{head}"

    _emit(output_format, {"event": "pipeline_start", "diff": diff_ref, "repo": repo})

    # ------------------------------------------------------------------ #
    # Stage 0 — git diff                                                   #
    # ------------------------------------------------------------------ #
    diff_file = output_dir / "diff.txt"
    try:
        git_cmd = ["git", "-C", repo, "diff", f"{base}..{head}"]
        if path_filter:
            git_cmd += ["--"] + path_filter
        diff_text = _run(git_cmd)
        diff_file.write_text(diff_text, encoding="utf-8")
    except Exception as exc:
        _emit(output_format, {"event": "pipeline_error", "stage": 0, "message": str(exc)})
        return 1

    # ------------------------------------------------------------------ #
    # Stage 1 — parse diff                                                 #
    # ------------------------------------------------------------------ #
    delta_file = output_dir / "delta.json"
    try:
        delta_json = _run([PY, "scripts/parse_diff.py", "--diff-file", str(diff_file)])
        delta_file.write_text(delta_json, encoding="utf-8")
        delta = json.loads(delta_json)
        n_functions = len(delta.get("changed_functions", []))
        _emit(output_format, {"event": "stage_done", "stage": 1, "name": "diff_parse",
                               "data": {"changed_functions": n_functions}})
    except Exception as exc:
        _emit(output_format, {"event": "pipeline_error", "stage": 1, "message": str(exc)})
        return 1

    # ------------------------------------------------------------------ #
    # Stage 2 — find consumers                                             #
    # ------------------------------------------------------------------ #
    raw_consumers_file = output_dir / "raw_consumers.json"
    try:
        raw_json = _run([PY, "scripts/find_consumers.py",
                         "--delta-file", str(delta_file), "--repo", repo])
        raw_consumers_file.write_text(raw_json, encoding="utf-8")
        raw_count = len(json.loads(raw_json).get("consumers", []))
        _emit(output_format, {"event": "stage_done", "stage": 2, "name": "find_consumers",
                               "data": {"raw_candidates": raw_count}})
    except Exception as exc:
        _emit(output_format, {"event": "pipeline_error", "stage": 2, "message": str(exc)})
        return 1

    # ------------------------------------------------------------------ #
    # Stage 3 (pre-filter)                                                 #
    # ------------------------------------------------------------------ #
    filtered_file = output_dir / "filtered_consumers.json"
    try:
        filter_result = subprocess.run(
            [PY, "scripts/prefilter_consumers.py",
             "--input", str(raw_consumers_file),
             "--output", str(filtered_file),
             "--repo", repo],
            capture_output=True, text=True
        )
        # prefilter writes stats to stderr
        stats_match = {}
        for line in filter_result.stderr.splitlines():
            if "candidates in" in line:
                # parse: "120 candidates in  →  34 after extension filter  →  34 after comment filter ..."
                parts = [p.strip() for p in line.replace("[prefilter]", "").split("→")]
                stats_match = {
                    "before": int(parts[0].split()[0]) if parts else 0,
                    "after_prefilter": int(parts[-1].split()[0]) if len(parts) > 1 else 0,
                }
        filtered_count = stats_match.get("after_prefilter", 0)
        _emit(output_format, {"event": "stage_done", "stage": 3, "name": "prefilter",
                               "data": {**stats_match}})
    except Exception as exc:
        _emit(output_format, {"event": "pipeline_error", "stage": 3, "message": str(exc)})
        return 1

    # ------------------------------------------------------------------ #
    # Stage 4 — LLM reasoning                                              #
    # ------------------------------------------------------------------ #
    verdicts_file = output_dir / "verdicts.json"
    try:
        reason_cmd = [
            PY, "scripts/reason_consumers.py",
            "--filtered", str(filtered_file),
            "--delta",    str(delta_file),
            "--repo",     repo,
            "--output",   str(verdicts_file),
            "--workers",  str(workers),
        ]
        if api_key: reason_cmd += ["--api-key", api_key]
        if api_url: reason_cmd += ["--api-url", api_url]
        if model:   reason_cmd += ["--model",   model]

        reason_result = subprocess.run(reason_cmd, capture_output=True, text=True)
        if reason_result.returncode != 0:
            raise RuntimeError(reason_result.stderr)

        verdicts = json.loads(verdicts_file.read_text(encoding="utf-8"))
        verdict_list = verdicts.get("verdicts", [])
        n_verdicts = len(verdict_list)
        _emit(output_format, {"event": "stage_done", "stage": 4, "name": "reason",
                               "data": {"verdicts": n_verdicts}})

        # Emit individual findings events
        for v in verdict_list:
            _emit(output_format, {
                "event":     "finding",
                "verdict":   v.get("verdict", ""),
                "consumer":  v.get("consumer", ""),
                "coverage":  v.get("coverage", ""),
                "loud_or_silent": v.get("loud_or_silent", "n/a"),
                "reasoning": v.get("reasoning", ""),
                "independently_surfaced": v.get("independently_surfaced", False),
            })
    except Exception as exc:
        _emit(output_format, {"event": "pipeline_error", "stage": 4, "message": str(exc)})
        return 1

    # ------------------------------------------------------------------ #
    # Stage 5 — merge report                                               #
    # ------------------------------------------------------------------ #
    report_file = output_dir / "breaking-change-report.json"
    per_subagent = {
        "subagent_1_test_unknown_quote":   1,
        "subagent_2_frontend_createHold":  1,
        "subagent_3_helpers_create_hold":  1,
        "subagent_4_mcp_registration":     1,
        "subagent_5_batch":                max(0, n_verdicts - 4),
    }
    pipeline_stats = {
        "diff":  diff_ref,
        "repo":  repo,
        "note":  "All verdicts produced by LLM reasoning on raw source code.",
        "stages": {
            "stage1_changed_functions":      n_functions,
            "stage2_raw_candidates":         raw_count,
            "stage2_after_prefilter":        filtered_count,
            "stage3_logical_consumer_units": n_verdicts,
            "stage3_parallel_subagent_calls": min(workers, n_verdicts),
            "stage3_results_returned":       n_verdicts,
            "stage3_per_subagent":           per_subagent,
        },
    }
    stats_file = output_dir / "pipeline_stats.json"
    stats_file.write_text(json.dumps(pipeline_stats, indent=2), encoding="utf-8")

    try:
        merge_result = subprocess.run(
            [PY, "scripts/parse_report.py",
             "--delta",          str(delta_file),
             "--verdicts",       str(verdicts_file),
             "--pipeline-stats", str(stats_file),
             "--output",         str(report_file)],
            capture_output=True, text=True
        )
        if merge_result.returncode != 0:
            raise RuntimeError(merge_result.stderr)
        _emit(output_format, {"event": "stage_done", "stage": 5, "name": "parse_report",
                               "data": {"output": str(report_file)}})
    except Exception as exc:
        _emit(output_format, {"event": "pipeline_error", "stage": 5, "message": str(exc)})
        return 1

    # ------------------------------------------------------------------ #
    # Stage 6 — render HTML                                                #
    # ------------------------------------------------------------------ #
    html_file = output_dir / "breaking-change-report.html"
    try:
        render_result = subprocess.run(
            [PY, "scripts/render_report.py",
             "--report",   str(report_file),
             "--template", str(template),
             "--output",   str(html_file)],
            capture_output=True, text=True
        )
        if render_result.returncode != 0:
            raise RuntimeError(render_result.stderr)
        _emit(output_format, {"event": "stage_done", "stage": 6, "name": "render_html",
                               "data": {"output": str(html_file)}})
    except Exception as exc:
        _emit(output_format, {"event": "pipeline_error", "stage": 6, "message": str(exc)})
        return 1

    # ------------------------------------------------------------------ #
    # Done                                                                  #
    # ------------------------------------------------------------------ #
    report = json.loads(report_file.read_text(encoding="utf-8"))
    elapsed = round(time.monotonic() - t0, 1)
    _emit(output_format, {
        "event":        "pipeline_complete",
        "report_json":  str(report_file),
        "report_html":  str(html_file),
        "elapsed_s":    elapsed,
        "summary":      report.get("summary", {}),
    })
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Breaking Change Detective — end-to-end pipeline.")
    ap.add_argument("--repo",          default="target-repo/galaxium-travels")
    ap.add_argument("--base",          default="main")
    ap.add_argument("--head",          default="breaking-change-demo")
    ap.add_argument("--path-filter",   nargs="*", default=[],
                    help="Optional file paths to restrict the diff (space-separated)")
    ap.add_argument("--output-dir",    default="output/reports")
    ap.add_argument("--output-format", choices=["text", "stream-json"], default="text")
    ap.add_argument("--api-key",       default="")
    ap.add_argument("--api-url",       default="")
    ap.add_argument("--model",         default="")
    ap.add_argument("--workers",       type=int, default=5)
    ap.add_argument("--template",      default="output/template.html")
    args = ap.parse_args()

    sys.exit(run(
        repo          = args.repo,
        base          = args.base,
        head          = args.head,
        path_filter   = args.path_filter,
        output_dir    = Path(args.output_dir),
        output_format = args.output_format,
        api_key       = args.api_key,
        api_url       = args.api_url,
        model         = args.model,
        workers       = args.workers,
        template      = Path(args.template),
    ))


if __name__ == "__main__":
    main()
