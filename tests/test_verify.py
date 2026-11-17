from pathlib import Path
from typing import Any

import pytest

from mergeworthy.config import Config
from mergeworthy.sandbox import BuildPlan, RunResult
from mergeworthy.verify import (
    LintCheck,
    detect_linters,
    rule_baseline,
    rule_linters,
    rule_scope,
    rule_tests_fail_without_fix,
    rule_tests_pass,
    verify_attempt,
)
from mergeworthy.work import diff_stats

PLAN = BuildPlan("python", "py", "pip install", "pytest")
CONFIG = Config()

SRC_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"
)
TEST_DIFF = (
    "diff --git a/tests/test_app.py b/tests/test_app.py\nnew file mode 100644\n--- /dev/null\n"
    "+++ b/tests/test_app.py\n@@ -0,0 +1 @@\n+def test(): assert app.value == 'new'\n"
)
NEW_SRC_DIFF = (
    "diff --git a/src/helper.py b/src/helper.py\nnew file mode 100644\n--- /dev/null\n+++ b/src/helper.py\n"
    "@@ -0,0 +1 @@\n+x = 1\n"
)


def ok(output: str = "") -> RunResult:
    return RunResult(0, output, False, 1.0)


def fail(output: str = "1 failed", code: int = 1) -> RunResult:
    return RunResult(code, output, False, 1.0)


class FakeSandbox:
    """Files in memory; ``run_tests`` passes only when the fix is present or the new test is absent."""

    def __init__(self, head: dict[str, str], current: dict[str, str], diff: str) -> None:
        self.head = dict(head)
        self.files = dict(current)
        self._diff = diff
        self.runs: list[str] = []
        self.restore_ok = True
        self.tamper = False
        self.test_result: RunResult | None = None
        self.lint_results: dict[str, RunResult] = {}

    @property
    def workdir(self) -> Path:
        return Path("/nonexistent")

    def run_tests(self, plan: BuildPlan, timeout_minutes: float) -> RunResult:
        self.runs.append("tests")
        if self.test_result is not None:
            return self.test_result
        if self.tamper:
            self.files["src/app.py"] = "tampered"
        if "tests/test_app.py" in self.files and self.files.get("src/app.py") != "new":
            return fail()
        return ok()

    def run(self, plan: BuildPlan, command: str, *, timeout_minutes: float | None = None) -> RunResult:
        self.runs.append(command)
        for tool, result in self.lint_results.items():
            if command.startswith(tool):
                return result
        return ok()

    def diff(self) -> str:
        return self._diff if self.files.get("src/app.py") in ("new", None) else "changed"

    def read_text(self, relative: str, limit: int = 1_000_000) -> str | None:
        return self.files.get(relative)

    def read_bytes(self, relative: str) -> bytes | None:
        value = self.files.get(relative)
        return None if value is None else value.encode()

    def write_bytes(self, relative: str, content: bytes | None) -> None:
        if content is None:
            self.files.pop(relative, None)
        else:
            self.files[relative] = content.decode()

    def restore_from_head(self, restore: list[str], remove: list[str]) -> RunResult:
        if not self.restore_ok:
            return fail("fatal: bad revision")
        for path in restore:
            self.files[path] = self.head[path]
        for path in remove:
            self.files.pop(path, None)
        return ok()


def fixed_sandbox(diff: str = SRC_DIFF + TEST_DIFF) -> FakeSandbox:
    return FakeSandbox(
        head={"src/app.py": "old"},
        current={"src/app.py": "new", "tests/test_app.py": "def test(): ..."},
        diff=diff,
    )


# --- rule 1 -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("baseline", "passed", "reason"),
    [
        ({"exit_code": 0, "timed_out": False}, True, "baseline tests passed"),
        ({"exit_code": 1, "timed_out": False}, False, "failed before any change"),
        ({"exit_code": -1, "timed_out": True}, False, "timed out"),
        (None, False, "no baseline"),
    ],
)
def test_rule_baseline(baseline: dict[str, Any] | None, passed: bool, reason: str) -> None:
    result = rule_baseline(baseline)
    assert result.passed is passed
    assert reason in result.reason


# --- rule 2 -----------------------------------------------------------------------------------------------


def test_rule_tests_pass() -> None:
    assert rule_tests_pass(ok()).passed
    failed = rule_tests_pass(fail("AssertionError"))
    assert not failed.passed
    assert "AssertionError" in failed.details["output"]
    assert "timed out" in rule_tests_pass(RunResult(-1, "", True, 900)).reason


# --- rule 3 -----------------------------------------------------------------------------------------------


def run_rule3(box: FakeSandbox, diff: str) -> Any:
    return rule_tests_fail_without_fix(box, PLAN, diff, diff_stats(diff), 15)


def test_new_test_fails_without_fix_and_working_copy_is_restored() -> None:
    box = fixed_sandbox()
    result = run_rule3(box, SRC_DIFF + TEST_DIFF)
    assert result.passed, result.reason
    assert box.files == {"src/app.py": "new", "tests/test_app.py": "def test(): ..."}


def test_tests_that_pass_without_fix_do_not_count() -> None:
    box = fixed_sandbox()
    box.test_result = ok()
    result = run_rule3(box, SRC_DIFF + TEST_DIFF)
    assert not result.passed
    assert "still pass without the fix" in result.reason
    assert box.files["src/app.py"] == "new"


def test_no_tests_in_change() -> None:
    assert "no tests" in run_rule3(fixed_sandbox(SRC_DIFF), SRC_DIFF).reason


def test_only_tests_in_change() -> None:
    assert "only tests" in run_rule3(fixed_sandbox(TEST_DIFF), TEST_DIFF).reason


def test_added_source_file_is_removed_then_restored() -> None:
    diff = SRC_DIFF + NEW_SRC_DIFF + TEST_DIFF
    box = fixed_sandbox(diff)
    box.files["src/helper.py"] = "x = 1\n"
    result = run_rule3(box, diff)
    assert result.passed, result.reason
    assert box.files["src/helper.py"] == "x = 1\n"


def test_timeout_without_fix_is_inconclusive() -> None:
    box = fixed_sandbox()
    box.test_result = RunResult(-1, "", True, 900)
    assert "inconclusive" in run_rule3(box, SRC_DIFF + TEST_DIFF).reason


def test_revert_failure_fails_rule() -> None:
    box = fixed_sandbox()
    box.restore_ok = False
    result = run_rule3(box, SRC_DIFF + TEST_DIFF)
    assert not result.passed
    assert "could not remove the fix" in result.reason


def test_tampering_during_verification_is_detected() -> None:
    box = fixed_sandbox()
    box.tamper = True

    original_write = box.write_bytes

    def write_but_keep_tamper(relative: str, content: bytes | None) -> None:
        original_write(relative, content)
        box.files["src/app.py"] = "tampered"

    box.write_bytes = write_but_keep_tamper  # type: ignore[method-assign]
    result = run_rule3(box, SRC_DIFF + TEST_DIFF)
    assert not result.passed
    assert "changed during verification" in result.reason


# --- rule 4 -----------------------------------------------------------------------------------------------


def stats_for(files: list[str], lines: int = 2) -> dict[str, Any]:
    return {"files": files, "test_files": [], "lines_changed_excluding_tests": lines, "test_lines_changed": 0}


def test_scope_passes_for_small_safe_change() -> None:
    assert rule_scope(SRC_DIFF, diff_stats(SRC_DIFF), CONFIG).passed


@pytest.mark.parametrize(
    ("path", "label"),
    [
        (".github/workflows/ci.yml", "CI configuration"),
        ("LICENSE", "licence file"),
        ("poetry.lock", "lockfile"),
        ("mvnw", "build wrapper"),
        (".mvn/extensions.xml", "build extension"),
        ("CONTRIBUTING.md", "governance"),
    ],
)
def test_scope_rejects_files_a_fix_should_not_touch(path: str, label: str) -> None:
    diff = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-a\n+b\n"
    result = rule_scope(diff, diff_stats(diff), CONFIG)
    assert not result.passed
    assert label in result.reason


def test_scope_rejects_oversized_and_wide_changes() -> None:
    assert (
        "lines changed excluding tests (limit 200)"
        in rule_scope(SRC_DIFF, stats_for(["src/app.py"], 201), CONFIG).reason
    )
    many = [f"src/m{i}.py" for i in range(16)]
    assert "16 files changed" in rule_scope(SRC_DIFF, stats_for(many), CONFIG).reason


def test_scope_rejects_unsafe_patch() -> None:
    diff = "diff --git a/link b/link\nnew file mode 120000\n"
    assert "mode 120000" in rule_scope(diff, diff_stats(diff), CONFIG).reason


# --- rule 5 -----------------------------------------------------------------------------------------------


def test_detect_python_linters() -> None:
    pyproject = "[tool.ruff]\nline-length = 100\n[tool.ruff.format]\n[tool.black]\n"
    checks = detect_linters(
        {"pyproject.toml", ".flake8"},
        lambda p: pyproject if p == "pyproject.toml" else None,
        ["src/app.py", "README.md", "src/my file.py"],
        "python",
    )
    commands = [c.command for c in checks]
    assert commands == [
        "ruff check src/app.py 'src/my file.py'",
        "ruff format --check src/app.py 'src/my file.py'",
        "black --check src/app.py 'src/my file.py'",
        "flake8 src/app.py 'src/my file.py'",
    ]
    assert detect_linters({"pyproject.toml"}, lambda p: pyproject, ["README.md"], "python") == []


def test_detect_java_linters() -> None:
    pom = (
        "<plugin><artifactId>spotless-maven-plugin</artifactId></plugin>"
        "<plugin><artifactId>maven-checkstyle-plugin</artifactId></plugin>"
    )
    tools = [c.tool for c in detect_linters({"pom.xml"}, lambda p: pom, ["A.java"], "maven")]
    assert tools == ["spotless", "checkstyle"]
    gradle = detect_linters(
        {"build.gradle", "gradlew"}, lambda p: "plugins { id 'com.diffplug.spotless' }", [], "gradle"
    )
    assert [c.command.split()[0] for c in gradle] == ["./gradlew"]
    assert detect_linters({"pom.xml"}, lambda p: "<project/>", ["A.java"], "maven") == []


def test_rule_linters() -> None:
    box = fixed_sandbox()
    assert rule_linters(box, PLAN, []).passed
    checks = [LintCheck("ruff", "ruff check a.py"), LintCheck("black", "black --check a.py")]
    assert rule_linters(box, PLAN, checks).passed
    box.lint_results = {"black": fail("would reformat a.py")}
    result = rule_linters(box, PLAN, checks)
    assert not result.passed
    assert "black reports problems: would reformat a.py" in result.reason
    box.lint_results = {"ruff": fail("sh: ruff: not found", code=127)}
    assert "configured but not installed" in rule_linters(box, PLAN, checks).reason


# --- all rules --------------------------------------------------------------------------------------------


def test_verify_attempt_runs_all_rules() -> None:
    box = fixed_sandbox()
    diff = SRC_DIFF + TEST_DIFF
    report = verify_attempt(
        box, PLAN, CONFIG, diff=diff, stats=diff_stats(diff), baseline={"exit_code": 0}, root_files=set()
    )
    assert report.passed, report.summary()
    assert [r.rule for r in report.rules] == [
        "baseline",
        "tests_pass",
        "tests_fail_without_fix",
        "scope",
        "linters",
    ]
    assert report.summary() == "all verification rules passed"


def test_verify_attempt_stops_before_tests_for_unsafe_scope() -> None:
    box = fixed_sandbox()
    diff = "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-a\n+b\n"
    report = verify_attempt(
        box, PLAN, CONFIG, diff=diff, stats=diff_stats(diff), baseline={"exit_code": 0}, root_files=set()
    )
    assert not report.passed
    assert [r.rule for r in report.rules] == ["baseline", "scope"]
    assert box.runs == []
