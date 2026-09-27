# Breaking Change Detective

Detect API-contract breakage across a git repository using static analysis
(stages 1–2) and LLM reasoning via IBM Bob (stage 3).

## Install

```bash
pip install git+https://github.com/IBM/breaking-change-detective.git
```

> **Prerequisite:** [IBM Bob](https://www.ibm.com/products/bob) must be
> installed and the `bob` command available on your `PATH`. Stage 3 reasoning
> shells out to `bob -p` — no API key is embedded in this package.

## Usage

```bash
bcd analyze \
  --repo  path/to/target-repo \
  --base  main \
  --head  my-feature-branch \
  --path  booking_system_backend/server.py
```

`--path` is optional; omit it to analyse all changed files.

The command runs stages 1–2 locally (git diff → contract delta → consumer
discovery) and then hands a structured prompt to `bob -p` for stage-3
reasoning.  Bob's analysis streams directly to your terminal.
