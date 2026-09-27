# Breaking Change Detective

Finds silent breaking changes to shared internal code — the kind that pass
every existing test and still compile, but quietly change behavior for a
downstream consumer that depended on the old contract.

Most CI catches syntactic breaks. It almost never catches semantic ones:
a shared function's error-handling contract changes, and somewhere three
files away — in a different language, with no test covering that path —
a caller silently misbehaves. This tool finds those.

## How it works

Two-stage architecture, deliberately split between deterministic code and
agentic reasoning:

1. **Deterministic discovery** (`scripts/parse_diff.py`, `scripts/find_consumers.py`)
   — parses a git diff into a structured "contract delta" (what changed about
   status codes, response shape, error signaling), then searches the codebase
   for every plausible consumer of the changed function, filtered from raw
   text matches down to real code call sites.

2. **Agentic reasoning** — each discovered consumer is analyzed independently
   by [IBM Bob](https://www.ibm.com/products/bob)'s own agent reasoning, which
   reads the real contract delta and the consumer's actual source, and returns
   a verdict: `BREAKS` (an existing test will fail), `BREAKS_SILENTLY`
   (behavior changes with no test to catch it), `LATENT` (a safety check is
   defeated but not currently exercised), `INVESTIGATE`, or `UNAFFECTED`.

The core distinction from a generic "find usages" tool: it reasons about
whether a consumer's *own error-detection logic* still fires under the new
contract — not just whether the code path is reachable.

## Status

- **Stages 1–2 are fully implemented and working** — validated against a
  real seeded breaking change with 120 raw candidates correctly filtered to
  34 plausible code consumers.
- **Stage 3's reasoning is architecturally validated but not yet wired
  end-to-end through the `bcd` CLI.** The design (one Bob call per consumer)
  was manually validated and produced four correct verdicts against a real
  codebase, including one (a `LATENT` finding) the tool surfaced independently
  of the original seeded bug — see `demo/ground_truth.md`. The CLI's
  subprocess invocation of `bob -p` currently fails on long prompts passed as
  a command-line argument on Windows (likely an argv length limit); piping
  the prompt via stdin instead of argv is the identified fix, not yet applied.
- **See the live demo** for the validated reasoning in full, including the
  real prompts and real responses.

## Installation

```bash
pip install git+https://github.com/<your-username>/breaking-change-detective.git
```

Requires [IBM Bob](https://www.ibm.com/products/bob) installed and available
on your `PATH` for stage-3 reasoning.

## Usage

```bash
bcd analyze --repo <path-to-target-repo> --base <base-ref> --head <head-ref> --path <changed-file>
```

Example, against the demo target repository (see below for how to clone it):

```bash
bcd analyze --repo target-repo/galaxium-travels --base main --head breaking-change-demo --path booking_system_backend/server.py
```

## Demo

This project was validated against
[IBM's Galaxium Travels](https://github.com/IBM/galaxium-travels) demo app.
It is not vendored into this repository — clone it separately:

```bash
git clone https://github.com/IBM/galaxium-travels target-repo/galaxium-travels
```

Two real breaking changes were seeded into its Python↔Java proxy layer —
exactly the kind of "fix" a well-meaning contributor would submit — and the
tool's reasoning was validated against both:

- **`create_hold`**: correctly separated a loud, CI-caught test failure from
  a genuinely silent break in the untested frontend, correctly reasoned about
  the auto-generated MCP tool layer AI agents call, and independently
  surfaced a fourth risk we hadn't seeded — a test helper whose own
  failure-detection assertion had quietly become dead code.
- **`release_hold`** (genericity check): a second, unrelated breaking change
  analyzed with no prior ground truth, correctly discovering two previously
  unseen UI consumers and correctly reasoning that one was unaffected because
  its error handling never inspects the response content at all.

**Live results viewer:** `<your-netlify-url-here>`
Full details, real prompts, and real reasoning: `demo/ground_truth.md`

## Project structure
scripts/ diff parsing, consumer discovery, CLI entry point
.bob/modes/ custom Bob mode config for the reasoning stage
demo/ seeded bug documentation and validated ground truth
ui/ static results viewer (deployed separately)
target-repo/ external clone target — not committed, see Demo section

## Built with IBM Bob

See `IBM Bob Usage Statement.txt` for the full breakdown of how Bob was used
both as a development partner (scaffolding the deterministic pipeline stages,
diagnosing a real dependency bug in the target demo app) and as the actual
reasoning engine powering the tool's verdicts.