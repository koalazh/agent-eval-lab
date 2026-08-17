#!/usr/bin/env python3
"""Small real subprocess harness used by the AEL Generic CLI acceptance path."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    prompt = sys.stdin.read()
    result = "pass" if "--result=pass" in sys.argv[1:] else "wrong"
    if "--no-write" not in sys.argv[1:]:
        Path.cwd().joinpath("answer.txt").write_text(f"{result}\n", encoding="utf-8")
    print(json.dumps({"type": "file_change", "name": "answer.txt", "summary": result}))
    print(json.dumps({"type": "complete", "summary": f"received {len(prompt)} prompt chars"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
