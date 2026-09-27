"""Stage 5 — Report Parser / Merger.

Takes the filtered candidate list (from prefilter_consumers.py) and the
raw LLM verdicts (from reason_consumers.py) and merges them into a single
structured findings report — the canonical output/reports/breaking-change-report.json.

Usage:
    python scripts/parse_report.py \
        --delta    <delta.json>      \
        --verdicts <verdicts.json>   \
        --pipeline-stats <stats.json> \
        [--output  <report.json>]

Output schema matches output/reports/breaking-change-report.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Verdict ordering for the summary section
# ---------------------------------------------------------------------------

_VERDICT_ORDER = ["BREAKS", "BREAKS_SILENTLY", "LATENT", "INVESTIGATE", "UNAFFECTED"]


def merge(delta: dict, verdicts: list[dict], stats: dict) -> dict:
    cf_list = delta.get("changed_functions", [])
    contract_delta = cf_list[0] if cf_list else {}

    findings = []
    summary: dict[str, list | int] = {v: [] for v in _VERDICT_ORDER}

    for v in verdicts:
        verdict_key = v.get("verdict", "INVESTIGATE")
        entry = {
            "consumer":        v.get("consumer", ""),
            "file":            v.get("file"),
            "line":            v.get("line"),
            "verdict":         verdict_key,
            "coverage":        v.get("coverage", "uncovered"),
            "loud_or_silent":  v.get("loud_or_silent", "n/a"),
            "in_ground_truth": v.get("in_ground_truth", False),
            "reasoning":       v.get("reasoning", ""),
        }
        if v.get("independently_surfaced"):
            entry["independently_surfaced"] = True
            entry["note"] = v.get("note", "")
        findings.append(entry)

        bucket = summary.get(verdict_key)
        if isinstance(bucket, list):
            label = v.get("consumer", "")
            if v.get("independently_surfaced"):
                label += " — independently surfaced, not in ground truth"
            bucket.append(label)

    # Collapse UNAFFECTED list to a count
    unaffected = summary.pop("UNAFFECTED", [])
    summary_out: dict = {}
    for k in _VERDICT_ORDER[:-1]:          # BREAKS … INVESTIGATE
        if summary[k]:
            summary_out[k] = summary[k]
    summary_out["UNAFFECTED"] = len(unaffected)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_run":  stats,
        "contract_delta": {
            "file":     contract_delta.get("file", ""),
            "function": contract_delta.get("function", ""),
            "endpoint": contract_delta.get("endpoint", ""),
            "summary":  contract_delta.get("contract_delta", {}).get("summary", ""),
        },
        "findings": findings,
        "summary":  summary_out,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Merge delta + verdicts into a structured report.")
    ap.add_argument("--delta",          required=True,  help="Contract delta JSON (parse_diff output)")
    ap.add_argument("--verdicts",       required=True,  help="Verdicts JSON (reason_consumers output)")
    ap.add_argument("--pipeline-stats", required=True,  help="Pipeline stats JSON")
    ap.add_argument("--output",         default="-",    help="Output path (default: stdout)")
    ap.add_argument("--indent",         type=int, default=2)
    args = ap.parse_args()

    delta    = json.loads(Path(args.delta).read_text(encoding="utf-8-sig"))
    verdicts = json.loads(Path(args.verdicts).read_text(encoding="utf-8-sig"))
    stats    = json.loads(Path(args.pipeline_stats).read_text(encoding="utf-8-sig"))

    # verdicts file may be {"verdicts": [...]} or a bare list
    if isinstance(verdicts, dict):
        verdicts = verdicts.get("verdicts", [])

    report = merge(delta, verdicts, stats)

    text = json.dumps(report, indent=args.indent)
    if args.output == "-":
        print(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"[parse_report] Written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
