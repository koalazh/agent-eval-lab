from __future__ import annotations

import difflib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .hashing import hash_file_tree
from .redaction import redact


class WorkspaceManager:
    def __init__(self, evidence_root: Path):
        self.evidence_root = evidence_root
        self.workspace_root = evidence_root / "workspaces"
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def create(self, fixture: Path, run_id: str) -> tuple[Path, dict[str, str]]:
        if not fixture.exists() or not fixture.is_dir():
            raise ValueError(f"fixture directory does not exist: {fixture}")
        workspace = Path(tempfile.mkdtemp(prefix=f"{run_id}-", dir=self.workspace_root))
        shutil.copytree(fixture, workspace, dirs_exist_ok=True)
        return workspace, self.snapshot(workspace)

    def snapshot(self, root: Path) -> dict[str, str]:
        return {
            str(item.relative_to(root)): hash_file_tree(item)
            for item in sorted(p for p in root.rglob("*") if p.is_file())
        }

    def capture(self, root: Path, before: dict[str, str], evidence_dir: Path) -> dict[str, Any]:
        after = self.snapshot(root)
        changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
        deleted = sorted(path for path in set(before) - set(after))
        added = sorted(path for path in set(after) - set(before))
        diff_lines: list[str] = []
        for relative in changed:
            target = root / relative
            before_text = ""
            if relative in before and target.exists():
                try:
                    before_text = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    before_text = "<binary or non-UTF8 file>"
            after_text = ""
            if relative in after and target.exists():
                try:
                    after_text = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    after_text = "<binary or non-UTF8 file>"
            diff_lines.extend(
                difflib.unified_diff(
                    before_text.splitlines(keepends=True),
                    after_text.splitlines(keepends=True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "diff.patch").write_text("".join(diff_lines), encoding="utf-8")
        changes = {
            "changed_files": changed,
            "added_files": added,
            "deleted_files": deleted,
            "workspace_isolation": "host-local filesystem copy; not an OS/security sandbox",
        }
        (evidence_dir / "changes.json").write_text(
            json.dumps(redact(changes), indent=2, sort_keys=True), encoding="utf-8"
        )
        (evidence_dir / "untracked.json").write_text(
            json.dumps(redact({"files": added}), indent=2, sort_keys=True), encoding="utf-8"
        )
        return changes

    @staticmethod
    def cleanup(root: Path) -> None:
        shutil.rmtree(root, ignore_errors=True)

