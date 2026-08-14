from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    workspace = Path(os.environ["AEL_WORKSPACE"])
    targeted = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_paginator.py", "-k", "targeted"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    print("targeted verification:")
    print(targeted.stdout, end="")
    if targeted.returncode != 0:
        print(targeted.stderr, file=sys.stderr, end="")
        return targeted.returncode

    full = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    print("visible full suite:")
    print(full.stdout, end="")
    if full.returncode != 0:
        print(full.stderr, file=sys.stderr, end="")
        return full.returncode

    sys.path.insert(0, str(workspace))
    from paginator import Page, collect

    pages = {
        None: Page(["first"], True, ""),
        "": Page(["second"], False),
    }
    if collect(lambda cursor, _size: pages[cursor]) != ["first", "second"]:
        raise AssertionError("an empty string can be a valid next cursor")

    print("hidden boundary verification: valid empty cursor passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
