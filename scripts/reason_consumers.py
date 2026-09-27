"""Stage 3 — LLM Consumer Reasoner.

Groups filtered candidates into logical consumer units, builds a rich prompt
for each (including ~30 lines of source context), and calls the LLM in
parallel via concurrent.futures. Results are written as a verdicts JSON file.

LLM endpoint configuration (in priority order):
  1. --api-url / --api-key CLI flags
  2. BOB_API_URL + BOB_API_KEY env vars  (Bob chat completions endpoint)
  3. OPENAI_API_KEY env var              (standard OpenAI endpoint)
  4. .env file in the workspace root     (same keys as above)

If no API key is found, writes PENDING_LLM verdicts for all consumers so the
pipeline can still produce a structurally-complete (but unresolved) report.

Usage:
    python scripts/reason_consumers.py \
        --filtered  output/filtered_consumers.json \
        --delta     output/delta.json \
        --repo      target-repo/galaxium-travels \
        --output    output/verdicts.json \
        [--api-url  https://...] \
        [--api-key  sk-...] \
        [--model    gpt-4o] \
        [--workers  5]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# ---------------------------------------------------------------------------
# .env loader (no external deps)
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


# ---------------------------------------------------------------------------
# Source context reader
# ---------------------------------------------------------------------------

def _source_context(repo: Path, rel_file: str, lineno: int | None, window: int = 30) -> str:
    """Return up to `window` lines centred on lineno from rel_file in the repo."""
    if not lineno:
        return ""
    try:
        lines = (repo / rel_file).read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(0, lineno - window // 2 - 1)
        end   = min(len(lines), lineno + window // 2)
        numbered = [f"{i+1:4d} | {lines[i]}" for i in range(start, end)]
        return "\n".join(numbered)
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Consumer grouping  (same logic used in the validated stage-3 run)
# ---------------------------------------------------------------------------

# Map from (function_context, file) key → group label and representative candidate
def _group_candidates(candidates: list[dict]) -> list[dict]:
    """
    Collapse the 34 filtered candidates into logical consumer units.
    Each unit = one LLM call.
    """
    groups: dict[str, dict] = {}

    for c in candidates:
        file_    = c.get("file", "")
        fn_ctx   = c.get("function_context", "") or "(module)"
        mtype    = c.get("match_type", "")
        ext      = Path(file_).suffix.lower()
        lineno   = c.get("line")
        snippet  = c.get("snippet", "")

        # --- Dedicated groups (one unit each) ---

        # MCP registration — always its own unit
        if mtype == "mcp_registration":
            key = "mcp_registration"
            if key not in groups:
                groups[key] = {"key": key, "candidates": [], "priority": 0}
            groups[key]["candidates"].append(c)
            continue

        # e2e test that directly asserts on error body — own unit
        if file_.endswith("test_holds.py") and fn_ctx == "test_hold_on_unknown_quote_returns_error":
            key = "test_unknown_quote"
            if key not in groups:
                groups[key] = {"key": key, "candidates": [], "priority": 0}
            groups[key]["candidates"].append(c)
            continue

        # helpers.py::create_hold — own unit (latent guard pattern)
        if file_.endswith("helpers.py") and fn_ctx == "create_hold":
            key = "helpers_create_hold"
            if key not in groups:
                groups[key] = {"key": key, "candidates": [], "priority": 0}
            groups[key]["candidates"].append(c)
            continue

        # frontend createHold — own unit
        if file_.endswith("api.ts") and fn_ctx == "createHold":
            key = "frontend_createHold"
            if key not in groups:
                groups[key] = {"key": key, "candidates": [], "priority": 0}
            groups[key]["candidates"].append(c)
            continue

        # --- Batch groups ---

        # frontend other hold functions
        if file_.endswith("api.ts") and fn_ctx in ("getHold", "confirmHold", "releaseHold"):
            key = "frontend_other_holds"
            if key not in groups:
                groups[key] = {"key": key, "candidates": [], "priority": 1}
            groups[key]["candidates"].append(c)
            continue

        # e2e happy-path tests
        if file_.endswith("test_holds.py") and fn_ctx not in ("test_hold_on_unknown_quote_returns_error", "(module)"):
            key = "test_happy_path"
            if key not in groups:
                groups[key] = {"key": key, "candidates": [], "priority": 1}
            groups[key]["candidates"].append(c)
            continue

        # holdStorage.ts
        if "holdStorage" in file_:
            key = "holdStorage"
            if key not in groups:
                groups[key] = {"key": key, "candidates": [], "priority": 1}
            groups[key]["candidates"].append(c)
            continue

        # Everything else (server.py other routes, module-level imports, etc.)
        key = "other"
        if key not in groups:
            groups[key] = {"key": key, "candidates": [], "priority": 1}
        groups[key]["candidates"].append(c)

    return list(groups.values())


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_CONTRACT_PREAMBLE = """\
## Contract change

File changed: `{file}`
Function: `{function}`
Endpoint: `{endpoint}`

BEFORE: {before_summary}
AFTER: {after_summary}
"""


def _contract_text(delta: dict) -> str:
    cf = delta.get("changed_functions", [{}])[0]
    cd = cf.get("contract_delta", {})
    before = cd.get("before", {})
    after  = cd.get("after",  {})

    def fmt(side: dict) -> str:
        eh = side.get("error_handling", [])
        rs = side.get("response_shape", [])
        sc = side.get("status_codes", [])
        parts = []
        if eh: parts.append("error handling: " + "; ".join(eh))
        if rs: parts.append("response shape: " + "; ".join(rs))
        if sc: parts.append("status codes: " + "; ".join(sc))
        return " | ".join(parts) if parts else "(unchanged)"

    return _CONTRACT_PREAMBLE.format(
        file=cf.get("file", ""),
        function=cf.get("function", ""),
        endpoint=cf.get("endpoint", ""),
        before_summary=fmt(before),
        after_summary=fmt(after),
    )


def _build_prompt(group: dict, delta: dict, repo: Path) -> str:
    key = group["key"]
    candidates = group["candidates"]
    contract = _contract_text(delta)

    # Gather source contexts for representative candidates (deduplicated by file:line)
    seen_ctx: set[tuple[str, int | None]] = set()
    contexts: list[str] = []
    for c in candidates:
        k = (c.get("file", ""), c.get("line"))
        if k in seen_ctx:
            continue
        seen_ctx.add(k)
        ctx = _source_context(repo, c.get("file", ""), c.get("line"))
        if ctx:
            contexts.append(f"### Source context: `{c['file']}` around line {c['line']}\n```\n{ctx}\n```")

    context_block = "\n\n".join(contexts[:4])  # cap at 4 context blocks

    # Build consumer description per group
    if key == "test_unknown_quote":
        consumer_desc = (
            "Consumer: `e2e/test_holds.py::test_hold_on_unknown_quote_returns_error`\n"
            "This test posts to the changed endpoint with an unknown quote ID and asserts on the response body. "
            "It runs in CI."
        )
        return_schema = _single_verdict_schema("e2e/test_holds.py::test_hold_on_unknown_quote_returns_error")

    elif key == "frontend_createHold":
        consumer_desc = (
            "Consumer: `booking_system_frontend/src/services/api.ts::createHold` "
            "and the `assertNotProxyError` guard it calls.\n"
            "No frontend tests exist."
        )
        return_schema = _single_verdict_schema("booking_system_frontend/src/services/api.ts::createHold+assertNotProxyError")

    elif key == "helpers_create_hold":
        consumer_desc = (
            "Consumer: `e2e/helpers.py::create_hold` — a shared e2e test helper.\n"
            "Consider: what does `r.raise_for_status()` do under the old vs. new contract? "
            "What does `assert 'holdId' in hold` do under each? "
            "Does any existing test call this helper with an invalid quote_id?"
        )
        return_schema = _single_verdict_schema("e2e/helpers.py::create_hold")

    elif key == "mcp_registration":
        consumer_desc = (
            "Consumer: `booking_system_backend/server.py` — `FastApiMCP(app)` auto-registration.\n"
            "`FastApiMCP` wraps every route on `app` as an MCP tool. "
            "The `create_hold` tool previously always returned HTTP 200 with a dict result; "
            "now it returns real 4xx/5xx on errors. "
            "Whether fastapi-mcp 0.4.0 surfaces non-2xx responses as MCP tool errors (isError:true) "
            "or as successful tool results is unknown without inspecting fastapi-mcp source. "
            "No MCP-layer tests exist."
        )
        return_schema = _single_verdict_schema(
            "booking_system_backend/server.py::FastApiMCP(app) — auto-wrapped create_hold tool"
        )

    elif key == "frontend_other_holds":
        consumer_desc = (
            "Consumers: `api.ts::getHold`, `api.ts::confirmHold`, `api.ts::releaseHold`.\n"
            "These call `GET /holds/{holdId}`, `POST /holds/{holdId}/confirm`, "
            "`POST /holds/{holdId}/release` respectively — all DIFFERENT endpoints from the one "
            "that was changed. Check whether those Python handlers were also changed. No frontend tests exist."
        )
        return_schema = _batch_verdict_schema([
            "booking_system_frontend/src/services/api.ts::getHold",
            "booking_system_frontend/src/services/api.ts::confirmHold",
            "booking_system_frontend/src/services/api.ts::releaseHold",
        ])

    elif key == "test_happy_path":
        consumer_desc = (
            "Consumers: happy-path e2e tests (`test_confirm_creates_booking_and_decrements_seat`, "
            "`test_release_does_not_consume_seat`, `test_confirm_is_idempotent`, `test_hold_auto_expiry`). "
            "These call `create_hold(client, quote[\"quoteId\"])` with a VALID quote on the success path."
        )
        return_schema = _batch_verdict_schema([
            "e2e/test_holds.py::happy-path tests (confirm/release/idempotent/auto-expiry)"
        ])

    else:  # "holdStorage" or "other"
        fnames = list({c.get("file", "") for c in candidates})[:3]
        consumer_desc = (
            f"Consumers: misc candidates in {fnames}. "
            "These were matched by URL substring or function-name proximity. "
            "Determine if any actually depend on the changed endpoint's response contract."
        )
        labels = list({c.get("file", "") + "::" + (c.get("function_context") or "(module)") for c in candidates})[:4]
        return_schema = _batch_verdict_schema(labels)

    return f"""\
You are a breaking-change analysis agent. Return ONLY valid JSON — no prose, no markdown fences.

{contract}

## Your task

{consumer_desc}

For each consumer, reason about:
1. Does it depend on the specific error response shape or status code that changed?
2. Does the breakage surface loudly (assertion/exception in CI) or silently (missed check, degraded UX)?
3. Is the error path covered by any existing test?

Verdicts: "BREAKS" | "BREAKS_SILENTLY" | "LATENT" | "INVESTIGATE" | "UNAFFECTED"

{context_block}

{return_schema}
"""


def _single_verdict_schema(consumer_label: str) -> str:
    return f"""\
Return exactly this JSON object:
{{
  "consumer": "{consumer_label}",
  "file": "<relative file path or null>",
  "line": <line number or null>,
  "verdict": "BREAKS|BREAKS_SILENTLY|LATENT|INVESTIGATE|UNAFFECTED",
  "coverage": "covered|uncovered",
  "loud_or_silent": "loud|silent|n/a",
  "reasoning": "two or three sentences"
}}"""


def _batch_verdict_schema(labels: list[str]) -> str:
    items = "\n".join(
        f'  {{"consumer": "{lbl}", "file": null, "line": null, '
        f'"verdict": "...", "coverage": "...", "loud_or_silent": "...", "reasoning": "..."}}'
        for lbl in labels
    )
    return f"Return a JSON array:\n[\n{items}\n]"


# ---------------------------------------------------------------------------
# LLM caller
# ---------------------------------------------------------------------------

def _call_llm(prompt: str, api_url: str, api_key: str, model: str, retries: int = 2) -> str:
    import httpx
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    for attempt in range(retries + 1):
        try:
            r = httpx.post(api_url, json=payload, headers=headers, timeout=120.0)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            if attempt == retries:
                raise
            time.sleep(2 ** attempt)
    return ""  # unreachable


def _parse_llm_response(text: str) -> list[dict]:
    """Extract JSON object or array from LLM response text."""
    # Strip markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text.strip(), flags=re.MULTILINE)
    text = text.strip()
    parsed = json.loads(text)
    if isinstance(parsed, list):
        return parsed
    return [parsed]


_PENDING_LABELS: dict[str, str] = {
    "test_unknown_quote":   "e2e/test_holds.py::test_hold_on_unknown_quote_returns_error",
    "frontend_createHold":  "booking_system_frontend/src/services/api.ts::createHold+assertNotProxyError",
    "helpers_create_hold":  "e2e/helpers.py::create_hold",
    "mcp_registration":     "booking_system_backend/server.py::FastApiMCP(app) — auto-wrapped create_hold tool",
    "frontend_other_holds": "booking_system_frontend/src/services/api.ts::getHold+confirmHold+releaseHold",
    "test_happy_path":      "e2e/test_holds.py::happy-path tests",
    "holdStorage":          "booking_system_frontend/src/utils/holdStorage.ts",
    "other":                "other candidates",
}


def _pending_verdicts(group: dict) -> list[dict]:
    key = group["key"]
    candidates = group["candidates"]
    label = _PENDING_LABELS.get(key) or (candidates[0].get("function_context") or candidates[0].get("file", ""))
    return [{"consumer": label, "verdict": "PENDING_LLM", "coverage": "uncovered",
             "loud_or_silent": "n/a", "reasoning": "LLM not available — set BOB_API_KEY or OPENAI_API_KEY"}]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 3: reason about consumer impact using LLM.")
    ap.add_argument("--filtered",  required=True, help="Filtered candidates JSON")
    ap.add_argument("--delta",     required=True, help="Contract delta JSON")
    ap.add_argument("--repo",      default="target-repo/galaxium-travels")
    ap.add_argument("--output",    default="output/verdicts.json")
    ap.add_argument("--api-url",   default="")
    ap.add_argument("--api-key",   default="")
    ap.add_argument("--model",     default="")
    ap.add_argument("--workers",   type=int, default=5)
    args = ap.parse_args()

    # Resolve API config
    dotenv = _load_dotenv(Path(".env"))
    api_key = args.api_key or os.environ.get("BOB_API_KEY") or dotenv.get("BOB_API_KEY") \
              or os.environ.get("OPENAI_API_KEY") or dotenv.get("OPENAI_API_KEY") or ""
    api_url = args.api_url or os.environ.get("BOB_API_URL") or dotenv.get("BOB_API_URL") \
              or "https://api.openai.com/v1/chat/completions"
    model   = args.model or os.environ.get("LLM_MODEL") or dotenv.get("LLM_MODEL") or "gpt-4o"

    filtered = json.loads(Path(args.filtered).read_text(encoding="utf-8"))
    candidates = filtered.get("consumers", filtered) if isinstance(filtered, dict) else filtered
    delta    = json.loads(Path(args.delta).read_text(encoding="utf-8"))
    repo     = Path(args.repo)

    groups = _group_candidates(candidates)
    print(f"[reason_consumers] {len(candidates)} candidates → {len(groups)} consumer groups", file=sys.stderr)

    verdicts: list[dict] = []

    if not api_key:
        print("[reason_consumers] WARNING: no API key found — writing PENDING_LLM verdicts", file=sys.stderr)
        for group in groups:
            verdicts.extend(_pending_verdicts(group))
    else:
        def _reason(group: dict) -> tuple[str, list[dict]]:
            prompt = _build_prompt(group, delta, repo)
            raw = _call_llm(prompt, api_url, api_key, model)
            results = _parse_llm_response(raw)
            print(f"[reason_consumers] group={group['key']} → {len(results)} verdict(s)", file=sys.stderr)
            return group["key"], results

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_reason, g): g for g in groups}
            for fut in as_completed(futures):
                try:
                    _, results = fut.result()
                    verdicts.extend(results)
                except Exception as exc:
                    group = futures[fut]
                    print(f"[reason_consumers] ERROR in group {group['key']}: {exc}", file=sys.stderr)
                    verdicts.extend(_pending_verdicts(group))

    output = {"verdicts": verdicts}
    Path(args.output).write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"[reason_consumers] {len(verdicts)} verdicts written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
