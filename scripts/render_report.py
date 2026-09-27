"""Stage 6 — HTML Report Renderer.

Reads the structured JSON report and renders it as a self-contained HTML file
using output/template.html as the template.

Usage:
    python scripts/render_report.py \
        --report  output/reports/breaking-change-report.json \
        --template output/template.html \
        --output  output/reports/breaking-change-report.html
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path


_VERDICT_BADGE = {
    "BREAKS":          ('<span class="badge badge-breaks">BREAKS</span>',          "#c0392b"),
    "BREAKS_SILENTLY": ('<span class="badge badge-silent">BREAKS SILENTLY</span>', "#e67e22"),
    "LATENT":          ('<span class="badge badge-latent">LATENT</span>',          "#8e44ad"),
    "INVESTIGATE":     ('<span class="badge badge-investigate">INVESTIGATE</span>',"#2980b9"),
    "UNAFFECTED":      ('<span class="badge badge-ok">UNAFFECTED</span>',          "#27ae60"),
    "PENDING_LLM":     ('<span class="badge badge-pending">PENDING</span>',        "#7f8c8d"),
}

_VERDICT_ORDER = ["BREAKS", "BREAKS_SILENTLY", "LATENT", "INVESTIGATE", "UNAFFECTED"]


def _badge(verdict: str) -> str:
    return _VERDICT_BADGE.get(verdict, _VERDICT_BADGE["INVESTIGATE"])[0]


def _render_findings(findings: list[dict]) -> str:
    rows = []
    # Sort: actionable verdicts first
    order = {v: i for i, v in enumerate(_VERDICT_ORDER)}
    sorted_findings = sorted(findings, key=lambda f: order.get(f.get("verdict", ""), 99))

    for f in sorted_findings:
        verdict   = f.get("verdict", "")
        consumer  = html.escape(f.get("consumer", ""))
        file_     = html.escape(f.get("file") or "")
        line      = f.get("line")
        file_ref  = f"{file_}:{line}" if line else file_
        reasoning = html.escape(f.get("reasoning", ""))
        coverage  = html.escape(f.get("coverage", ""))
        loud      = html.escape(f.get("loud_or_silent", "n/a"))
        badge     = _badge(verdict)
        extra     = ""
        if f.get("independently_surfaced"):
            extra = '<span class="tag-surfaced">independently surfaced</span>'

        rows.append(f"""
      <tr class="verdict-{verdict.lower().replace('_','-')}">
        <td>{badge}{extra}</td>
        <td class="consumer-name"><code>{consumer}</code></td>
        <td class="file-ref"><code>{file_ref}</code></td>
        <td class="coverage-cell">{coverage}</td>
        <td class="loud-cell">{loud}</td>
        <td class="reasoning-cell">{reasoning}</td>
      </tr>""")
    return "\n".join(rows)


def _render_summary_bars(summary: dict) -> str:
    parts = []
    colors = {
        "BREAKS": "#c0392b", "BREAKS_SILENTLY": "#e67e22",
        "LATENT": "#8e44ad", "INVESTIGATE": "#2980b9", "UNAFFECTED": "#27ae60",
    }
    labels = {
        "BREAKS": "Breaks", "BREAKS_SILENTLY": "Breaks Silently",
        "LATENT": "Latent", "INVESTIGATE": "Investigate", "UNAFFECTED": "Unaffected",
    }
    for k in _VERDICT_ORDER:
        val = summary.get(k, [])
        count = len(val) if isinstance(val, list) else val
        if count == 0:
            continue
        color = colors[k]
        label = labels[k]
        items_html = ""
        if isinstance(val, list):
            items_html = "".join(f'<li>{html.escape(v)}</li>' for v in val)
            items_html = f'<ul class="summary-list">{items_html}</ul>'
        parts.append(f"""
    <div class="summary-card" style="border-left:4px solid {color}">
      <div class="summary-count" style="color:{color}">{count}</div>
      <div class="summary-label">{label}</div>
      {items_html}
    </div>""")
    return "\n".join(parts)


def render(report: dict, template: str) -> str:
    pr     = report.get("pipeline_run", {})
    stages = pr.get("stages", {})
    cd     = report.get("contract_delta", {})
    summary = report.get("summary", {})
    findings = report.get("findings", [])

    substitutions = {
        "{{GENERATED_AT}}":      html.escape(report.get("generated_at", "")),
        "{{DIFF_REF}}":          html.escape(pr.get("diff", "")),
        "{{REPO}}":              html.escape(pr.get("repo", "")),
        "{{STAGE1_FUNCTIONS}}":  str(stages.get("stage1_changed_functions", 0)),
        "{{STAGE2_RAW}}":        str(stages.get("stage2_raw_candidates", 0)),
        "{{STAGE2_FILTERED}}":   str(stages.get("stage2_after_prefilter", 0)),
        "{{STAGE3_UNITS}}":      str(stages.get("stage3_logical_consumer_units", 0)),
        "{{STAGE3_SUBAGENTS}}":  str(stages.get("stage3_parallel_subagent_calls", 0)),
        "{{CHANGED_FUNCTION}}":  html.escape(cd.get("function", "")),
        "{{CHANGED_ENDPOINT}}":  html.escape(cd.get("endpoint", "")),
        "{{CHANGED_FILE}}":      html.escape(cd.get("file", "")),
        "{{CONTRACT_SUMMARY}}":  html.escape(cd.get("summary", "")),
        "{{SUMMARY_CARDS}}":     _render_summary_bars(summary),
        "{{FINDINGS_ROWS}}":     _render_findings(findings),
        "{{TOTAL_FINDINGS}}":    str(len(findings)),
    }

    out = template
    for placeholder, value in substitutions.items():
        out = out.replace(placeholder, value)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a breaking-change report as HTML.")
    ap.add_argument("--report",   required=True, help="Structured JSON report")
    ap.add_argument("--template", required=True, help="HTML template file")
    ap.add_argument("--output",   default="-",   help="Output path (default: stdout)")
    args = ap.parse_args()

    report   = json.loads(Path(args.report).read_text(encoding="utf-8"))
    template = Path(args.template).read_text(encoding="utf-8")

    html_out = render(report, template)

    if args.output == "-":
        print(html_out)
    else:
        Path(args.output).write_text(html_out, encoding="utf-8")
        print(f"[render_report] Written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
