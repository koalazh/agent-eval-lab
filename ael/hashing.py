from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any


UNKNOWN = "UNKNOWN"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hash_file_tree(path: Path) -> str:
    if not path.exists():
        return UNKNOWN
    if path.is_file():
        return sha256_bytes(path.read_bytes())
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(str(item.relative_to(path)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def git_sha(cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    value = result.stdout.strip()
    return value or UNKNOWN


def runtime_profile() -> dict[str, str]:
    return {
        "os": platform.system() or UNKNOWN,
        "os_release": platform.release() or UNKNOWN,
        "architecture": platform.machine() or UNKNOWN,
        "python": sys.version.split()[0] or UNKNOWN,
    }


def config_hash(value: Any) -> str:
    return sha256_text(canonical_json(value))

