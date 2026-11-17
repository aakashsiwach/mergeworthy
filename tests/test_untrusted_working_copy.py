"""The working copy, including .git, is untrusted. Host code never runs git or any other program in it."""

import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from mergeworthy.agent import Budget, Task
from mergeworthy.agent.claude import check_tool_call
from mergeworthy.agent.fake import FakeAgent
from mergeworthy.patch import PatchError, require_valid, validate_patch
from mergeworthy.sandbox import BuildPlan, Sandbox, SandboxError, SandboxSettings
from mergeworthy.work import BoundSandbox, diff_stats

PLAN = BuildPlan("python", "py", "pip install", "pytest")


class RecordingDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout: float) -> tuple[int, str, bool]:
        self.calls.append(list(args))
        return 0, "", False


def plant(workdir: Path, marker: Path) -> None:
    """A hostile .git: fsmonitor and hooks that would create ``marker`` if any host git ran here."""
    payload = f"\"{sys.executable}\" -c \"open(r'{marker}', 'w').write('pwned')\""
    git = workdir / ".git"
    (git / "hooks").mkdir(parents=True)
    (git / "config").write_text(
        f"[core]\n\tfsmonitor = {payload}\n\tpager = {payload}\n[alias]\n\tst = !{payload}\n",
        encoding="utf-8",
    )
    for hook in ("post-checkout", "pre-commit", "post-index-change"):
        (git / "hooks" / hook).write_text(f"#!/bin/sh\n{payload}\n", encoding="utf-8")
    (workdir / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (workdir / "app.py").write_bytes(b"x = 1\n")


@pytest.fixture
def no_host_processes(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Fail on any process the host would start; record argv instead."""
    started: list[list[str]] = []

    def refuse(args: object, *a: object, **k: object) -> None:
        started.append([str(x) for x in args] if isinstance(args, list | tuple) else [str(args)])
        raise AssertionError(f"host code started a process: {args}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    return started


def test_host_code_never_executes_a_planted_git_config(
    tmp_path: Path, no_host_processes: list[list[str]]
) -> None:
    docker = RecordingDocker()
    box = Sandbox(5, SandboxSettings(work_root=tmp_path / "work"), runner=docker)
    box.workdir.mkdir(parents=True)
    marker = tmp_path / "PWNED"
    plant(box.workdir, marker)

    # Every host-side operation on the working copy.
    assert "pyproject.toml" in box.root_files()
    assert box.read_text("app.py") == "x = 1\n"
    plan = box.detect()
    box.remove_symlinks()
    box.install(plan)
    box.run_tests(plan, 1)
    box.run(plan, "pytest -q")
    box.diff()
    agent = FakeAgent(files={"app.py": "x = 2\n"}, commands=["pytest -q"])
    agent.attempt(
        Task("o/r", 1, "https://github.com/o/r/issues/1", "t", "b", "pytest"),
        box.workdir,
        BoundSandbox(box, plan),
        Budget(1, 1, 1),
    )
    diff = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    diff_stats(diff)
    require_valid(diff, ["app.py"])

    assert not marker.exists()
    assert no_host_processes == []
    assert all(call[0] == "docker" for call in docker.calls)


def test_project_containers_see_git_read_only(tmp_path: Path) -> None:
    docker = RecordingDocker()
    box = Sandbox(5, SandboxSettings(work_root=tmp_path), runner=docker)
    (box.workdir / ".git").mkdir(parents=True)
    box.install(PLAN)
    box.run_tests(PLAN, 1)
    box.run(PLAN, "ls")
    git_ro = f"type=bind,source={box.workdir / '.git'},target=/work/repo/.git,readonly"
    for call in docker.calls:
        assert git_ro in call, call
    box.diff()  # mergeworthy's own fixed git command may write the index
    assert git_ro not in docker.calls[-1]


def test_sandbox_refuses_non_docker_commands(tmp_path: Path) -> None:
    box = Sandbox(5, SandboxSettings(work_root=tmp_path), runner=RecordingDocker())
    with pytest.raises(SandboxError, match="non-docker"):
        box._exec(["git", "status"], 10)


@pytest.mark.parametrize(
    "path", [".git/config", ".git/hooks/pre-commit", "sub/.git/config", "./.git/HEAD", ".GIT/config"]
)
def test_agent_cannot_touch_git_metadata(tmp_path: Path, path: str) -> None:
    for tool in ("Read", "Edit", "Write"):
        reason = check_tool_call(tool, {"file_path": path}, tmp_path)
        if path == ".GIT/config" and reason is None:  # case-sensitive file systems treat it as another dir
            continue
        assert reason is not None, (tool, path)
    assert check_tool_call("Grep", {"pattern": "x", "glob": ".git/**"}, tmp_path) is not None


def test_fake_agent_cannot_write_git_metadata(tmp_path: Path) -> None:
    box = Sandbox(5, SandboxSettings(work_root=tmp_path), runner=RecordingDocker())
    box.workdir.mkdir(parents=True)
    with pytest.raises(ValueError, match="escapes"):
        FakeAgent(files={".git/config": "[core]"}).attempt(
            Task("o/r", 1, "u", "t", "b", "p"), box.workdir, BoundSandbox(box, PLAN), Budget(1, 1, 1)
        )


# --- patch validation (used by verification and by the clean-clone submission) -----------------------------

GOOD = (
    "diff --git a/src/app.py b/src/app.py\nindex 1111111..2222222 100644\n"
    "--- a/src/app.py\n+++ b/src/app.py\n"
    "@@ -1 +1 @@\n-old\n+new\n"
    "diff --git a/tests/test_app.py b/tests/test_app.py\nnew file mode 100644\nindex 0000000..3333333\n"
    "--- /dev/null\n+++ b/tests/test_app.py\n@@ -0,0 +1 @@\n+def test(): pass\n"
)


def test_valid_patch() -> None:
    report = validate_patch(GOOD, ["src/app.py", "tests/test_app.py"])
    assert report.ok, report.problems
    assert report.files == ["src/app.py", "tests/test_app.py"]


@pytest.mark.parametrize(
    ("diff", "problem"),
    [
        ("diff --git a/.git/config b/.git/config\n--- a/.git/config\n+++ b/.git/config\n", "touches .git"),
        ("diff --git a/x/.git/hooks/a b/x/.git/hooks/a\n", "touches .git"),
        ("diff --git a/.gitmodules b/.gitmodules\n", ".gitmodules"),
        ("diff --git a/../etc/passwd b/../etc/passwd\n", "contains . or .."),
        ("diff --git a/link b/link\nnew file mode 120000\n", "mode 120000"),
        ("diff --git a/vendor/lib b/vendor/lib\nnew file mode 160000\n", "mode 160000"),
        ("diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n", None),
        ("diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n", "binary change"),
        ("diff --git a/a.py b/a.py\nrename from a.py\nrename to .git/hooks/post-merge\n", "touches .git"),
        ("garbage before\ndiff --git a/a b/a\n", "text before the first file header"),
        ("not a diff at all\n", "text before the first file header"),
    ],
)
def test_unsafe_patches_are_rejected(diff: str, problem: str | None) -> None:
    report = validate_patch(diff)
    if problem is None:
        assert report.ok, report.problems
    else:
        assert any(problem in p for p in report.problems), report.problems


def test_patch_must_match_the_approved_files() -> None:
    with pytest.raises(PatchError, match=re.escape("unexpected: ['tests/test_app.py']")):
        require_valid(GOOD, ["src/app.py"])
    with pytest.raises(PatchError, match=re.escape("missing: ['README.md']")):
        require_valid(GOOD, ["src/app.py", "tests/test_app.py", "README.md"])
