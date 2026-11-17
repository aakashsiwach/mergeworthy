"""Validate a diff produced in the untrusted working copy before it is used anywhere else.

The working copy, including its ``.git`` directory, is untrusted: tests and the agent ran there. The only
thing that leaves it is the diff, as text. Submission (T8) makes a fresh clone on the host, checks the diff
with ``validate_patch`` against the files recorded at approval, applies it, and commits from that clone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
ALLOWED_MODES = frozenset({"100644", "100755"})
_MODE_LINE = re.compile(
    r"^(new file mode|deleted file mode|old mode|new mode|index [0-9a-f]+\.\.[0-9a-f]+) ?(\d+)?$"
)


class PatchError(ValueError):
    pass


@dataclass(frozen=True)
class PatchReport:
    files: list[str]
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _path_problem(path: str) -> str | None:
    if "\\" in path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return f"{path}: not a relative POSIX path"
    parts = PurePosixPath(path).parts
    if ".." in parts or "." in parts:
        return f"{path}: contains . or .."
    if any(part.lower() == ".git" for part in parts):
        return f"{path}: touches .git"
    if any(part.lower() in {".gitmodules"} for part in parts):
        return f"{path}: touches .gitmodules"
    return None


def validate_patch(diff: str, expected_files: list[str] | None = None) -> PatchReport:
    """Check a ``git diff`` text: safe relative paths, regular files only (no symlinks or submodules), no
    binary patches, and, when given, exactly the expected set of files."""
    files: list[str] = []
    problems: list[str] = []
    current: str | None = None
    for line in diff.splitlines():
        header = _HEADER.match(line)
        if header:
            old, new = header.groups()
            for path in {old, new}:
                problem = _path_problem(path)
                if problem:
                    problems.append(problem)
            current = new
            if new not in files:
                files.append(new)
            if old != new and old not in files:
                files.append(old)
            continue
        if line.startswith("diff --git"):
            problems.append(f"unparseable file header: {line[:120]}")
            continue
        if current is None:
            if line.strip():
                problems.append("text before the first file header")
            continue
        mode = _MODE_LINE.match(line)
        if mode and mode.group(2) and mode.group(2) not in ALLOWED_MODES:
            problems.append(f"{current}: mode {mode.group(2)} (only regular files are allowed)")
        if line.startswith(("Binary files ", "GIT binary patch")):
            problems.append(f"{current}: binary change")
        if line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            path = line.split(" ", 2)[2]
            problem = _path_problem(path)
            if problem:
                problems.append(problem)
    if not files and diff.strip():
        problems.append("no file headers found")
    if expected_files is not None and sorted(files) != sorted(set(expected_files)):
        unexpected = sorted(set(files) - set(expected_files))
        missing = sorted(set(expected_files) - set(files))
        problems.append(f"files differ from the approved set (unexpected: {unexpected}, missing: {missing})")
    return PatchReport(files=sorted(files), problems=problems)


def require_valid(diff: str, expected_files: list[str] | None = None) -> PatchReport:
    report = validate_patch(diff, expected_files)
    if not report.ok:
        raise PatchError("; ".join(report.problems))
    return report


def file_changes(diff: str) -> dict[str, str]:
    """Map each file in a validated diff to ``added``, ``deleted`` or ``modified``."""
    changes: dict[str, str] = {}
    current: str | None = None
    for line in diff.splitlines():
        header = _HEADER.match(line)
        if header:
            current = header.group(2)
            changes[current] = "modified"
        elif current is not None and line.startswith("new file mode"):
            changes[current] = "added"
        elif current is not None and line.startswith("deleted file mode"):
            changes[current] = "deleted"
    return changes
