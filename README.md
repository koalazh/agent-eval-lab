# Agent Eval Lab

> Agent Eval Lab is not a leaderboard.
> It is a local experiment system for understanding how and why coding agents change.

**Compare cases, not just scores.**

**Evidence before interpretation.**

**Turn failures into the next experiments.**

AEL is a local-first Coding Agent experiment lab. It runs the same Case against Agent Variants and Trials, asks an independent verifier for task truth, preserves workspace/native evidence, and makes failures the input to the next experiment.

## Development

This project uses uv and Python 3.11+.

Run:

    uv sync --extra test
    uv run pytest
    uv run ael doctor

No paid model calls are made by the test suite.

## Local evidence

AEL keeps native output and normalized events separate from verifier truth:

- verifier: whether the task passed;
- workspace: what changed;
- native/telemetry: behavior evidence.

Host-local filesystem copying is workspace isolation, not an OS or security sandbox. An Agent sandbox and approval policy remain part of its recorded variant/run fingerprint.

## Current machine probe

The read-only M1 probe currently reports Codex CLI 0.147.0, Claude Code 2.1.229, and Hermes 0.20.0 on this machine. Pi is not on PATH, so its capability is reported as unavailable until the CLI exists. AEL does not silently install or simulate missing Agents.

The real adapters use current machine help and official machine interfaces: Codex exec JSONL, Claude non-interactive stream JSON, Pi RPC JSONL, and Hermes one-shot with a usage file. Their native output stays in each Run's native evidence directory.

The manual-only live smoke recipe is:

    uv run ael doctor --root .
    uv run ael agents --root .
    uv run ael run examples/experiments/live-smoke.yaml --root .

It never stores credentials in the repository. A missing CLI, unavailable provider, authentication error, or rate limit is recorded as infrastructure/process evidence and is not promoted as an Agent task failure.

## Observation profiles and OTel

Runs default to minimal observation. Minimal and telemetry retain normalized behavior summaries but omit structured prompt, tool-argument, tool-result, and transcript fields; deep is explicit and preserves those fields subject to credential redaction. Every Run records its ObservationProfile in the fingerprint.

Optional telemetry uses per-run OpenTelemetry resource attributes and never edits user-global Agent configuration. The sample Collector at `infra/otel-collector.yaml` binds only to localhost and exports to a local debug exporter. Without a Collector, AEL still runs; the doctor result says `NOT_FOUND`.

For Claude Code, `telemetry` enables its metrics/logs through the per-run environment only when `AEL_OTEL_ENDPOINT` is supplied; `deep` additionally opts into prompt and tool payload fields. Minimal remains the default.

## Example

A future comparison should be readable as:

    Minimal v0.3       PASS
    Minimal v0.4       FAIL

    same:
    model
    task
    runtime

    changed:
    compaction=true

    next experiment:
    compaction off/on on context regression suite

The report should say what is observed and what remains unknown, rather than claiming that a model is a precise percentage responsible.

## Product boundary

AEL focuses on matrix execution, differential comparison, evidence fusion, failure investigation, failure-to-experiment, and failure-to-regression. It is not a distributed runner, agent runtime, generic plugin framework, OTel backend, or cloud service.

## Differential evidence

The deterministic Failure Explorer first checks Case revision, selects the closest PASS reference, and shows SAME/CHANGED/UNKNOWN variables, confidence (`CONTROLLED`, `PARTIAL`, or `DESCRIPTIVE`), verifier/workspace evidence, normalized anchor timelines, and the first meaningful divergence. If the reference or anchor evidence is insufficient it says so; it does not invent a causal root cause.

Diagnosis consumes that compact packet, not an unbounded trace. Without configuration it returns deterministic hypotheses and unknowns. With `AEL_DIAGNOSIS_BASE_URL`, `AEL_DIAGNOSIS_API_KEY`, and `AEL_DIAGNOSIS_MODEL`, it can call one OpenAI-compatible chat-completions endpoint; the key is used only in the request header. A follow-up action creates a user-confirmed `DRAFT` experiment over the same Case revision and records the proposed independent variable.

## Failure Book

Completed processes with verifier FAIL become `OBSERVED` Failure Book entries. A user can promote one to `REGRESSION_GUARDED`; AEL copies the fixture and Python grader, adds a new Case revision to the Regression Suite, and leaves the source Case untouched. Later experiments can be constructed from that persisted Suite and rerun with the same verifier.

## License

Internal greenfield prototype.
## Runnable example

The repository includes a no-cost deterministic matrix:

    uv run ael run examples/experiments/fake-matrix.yaml --root .

Then open the local UI:

    uv run ael ui --root .

The example intentionally produces both stable PASS and stable FAIL rows without calling a model.

For a persisted experiment, use `uv run ael compare <experiment-a> <experiment-b> --root .` and inspect the run-level Failure Explorer from the local UI. AEL writes the database and large evidence under `.ael/`, which is ignored by Git.
