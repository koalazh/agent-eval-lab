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

## License

Internal greenfield prototype.

