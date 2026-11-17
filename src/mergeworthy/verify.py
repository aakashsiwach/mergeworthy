"""Objective verification of an attempt (docs/architecture.md, "Verification rules").

1. ``baseline``: the suite passed on the unmodified repository.
2. ``tests_pass``: the suite passes with the change.
3. ``tests_fail_without_fix``: the change adds or changes tests, and the suite with those tests but without
   the non-test changes fails. Applied to every change, not only bug fixes, because docs/policy.md section 4
   requires tests that fail before the fix and pass after it whenever the project has a test suite. The
   working copy must be byte-for-byte the same afterwards.
4. ``scope``: the diff is a safe patch (``patch.validate_patch``), within the size limit (excluding tests),
   touches few files and none of the files a contribution should not change (CI, lockfiles, licences).
5. ``linters``: the linters and formatters the project configures pass on the changed files.

Every rule returns a result with a reason. Verification passes only if all of them pass.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from mergeworthy.config import Config
from mergeworthy.patch import file_changes, validate_patch
from mergeworthy.sandbox import BuildPlan, RunResult

MAX_FILES = 15
NOT_FOUND = 127

# Paths a contribution for an issue should not touch. Matched against the full relative path.
FORBIDDEN_PATHS = (
    (re.compile(r"^\.github/(workflows|actions)/"), "CI configuration"),
    (re.compile(r"(^|/)\.(gitlab-ci|travis|circleci|drone)\b"), "CI configuration"),
    (re.compile(r"(^|/)Jenkinsfile$"), "CI configuration"),
    (re.compile(r"(^|/)(LICENSE|LICENCE|COPYING|NOTICE)(\.\w+)?$", re.I), "licence file"),
    (
        re.compile(
            r"(^|/)(poetry\.lock|uv\.lock|Pipfile\.lock|package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$"
        ),
        "lockfile",
    ),
    (re.compile(r"(^|/)(gradle/wrapper/|\.mvn/wrapper/|mvnw(\.cmd)?$|gradlew(\.bat)?$)"), "build wrapper"),
    (re.compile(r"(^|/)\.mvn/extensions\.xml$"), "build extension"),
    (
        re.compile(r"(^|/)(CODEOWNERS|SECURITY\.md|CODE_OF_CONDUCT\.md|CONTRIBUTING\.md)$"),
        "project governance file",
    ),
)


@dataclass(frozen=True)
class RuleResult:
    rule: str
    passed: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "passed": self.passed, "reason": self.reason, "details": self.details}


@dataclass(frozen=True)
class VerificationReport:
    rules: list[RuleResult]

    @property
    def passed(self) -> bool:
        return bool(self.rules) and all(r.passed for r in self.rules)

    @property
    def failures(self) -> list[RuleResult]:
        return [r for r in self.rules if not r.passed]

    def summary(self) -> str:
        if self.passed:
            return "all verification rules passed"
        return "; ".join(f"{r.rule}: {r.reason}" for r in self.failures)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.rules]


class VerifySandbox(Protocol):
    @property
    def workdir(self) -> Path: ...

    def run_tests(self, plan: BuildPlan, timeout_minutes: float) -> RunResult: ...
    def run(self, plan: BuildPlan, command: str, *, timeout_minutes: float | None = None) -> RunResult: ...
    def diff(self) -> str: ...
    def read_text(self, relative: str, limit: int = 1_000_000) -> str | None: ...
    def read_bytes(self, relative: str) -> bytes | None: ...
    def write_bytes(self, relative: str, content: bytes | None) -> None: ...
    def restore_from_head(self, restore: list[str], remove: list[str]) -> RunResult: ...


# --- rules ------------------------------------------------------------------------------------------------


def rule_baseline(baseline: dict[str, Any] | None) -> RuleResult:
    if not baseline:
        return RuleResult("baseline", False, "no baseline test run was recorded")
    if baseline.get("timed_out"):
        return RuleResult("baseline", False, "baseline tests timed out", baseline)
    if baseline.get("exit_code") != 0:
        return RuleResult("baseline", False, "baseline tests failed before any change", baseline)
    return RuleResult("baseline", True, "baseline tests passed", baseline)


def rule_tests_pass(result: RunResult) -> RuleResult:
    details = {
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "seconds": round(result.seconds, 1),
    }
    if result.timed_out:
        return RuleResult("tests_pass", False, "tests timed out with the change", details)
    if not result.ok:
        return RuleResult(
            "tests_pass", False, "tests fail with the change", {**details, "output": result.output[-2000:]}
        )
    return RuleResult("tests_pass", True, "tests pass with the change", details)


def rule_scope(diff: str, stats: dict[str, Any], config: Config) -> RuleResult:
    problems = list(validate_patch(diff).problems)
    files: list[str] = stats.get("files", [])
    for path in files:
        for pattern, label in FORBIDDEN_PATHS:
            if pattern.search(path):
                problems.append(f"{path}: {label} should not be changed")
    if len(files) > MAX_FILES:
        problems.append(f"{len(files)} files changed (limit {MAX_FILES})")
    lines = int(stats.get("lines_changed_excluding_tests", 0))
    limit = config.limits.max_diff_lines
    if lines > limit:
        problems.append(f"{lines} lines changed excluding tests (limit {limit})")
    details = {"files": len(files), "lines_changed_excluding_tests": lines, "limit": limit}
    if problems:
        return RuleResult("scope", False, "; ".join(problems), details)
    return RuleResult("scope", True, f"{len(files)} files, {lines} lines excluding tests", details)


def rule_tests_fail_without_fix(
    sandbox: VerifySandbox, plan: BuildPlan, diff: str, stats: dict[str, Any], timeout_minutes: float
) -> RuleResult:
    name = "tests_fail_without_fix"
    test_files = set(stats.get("test_files", []))
    if not test_files:
        return RuleResult(name, False, "the change adds or changes no tests")
    changes = {path: kind for path, kind in file_changes(diff).items() if path not in test_files}
    if not changes:
        return RuleResult(
            name, False, "the change contains only tests; there is no fix to check them against"
        )

    saved = {path: sandbox.read_bytes(path) for path in changes}
    restore = [path for path, kind in changes.items() if kind != "added"]
    remove = [path for path, kind in changes.items() if kind == "added"]
    reverted = sandbox.restore_from_head(restore, remove)
    if not reverted.ok:
        result = RuleResult(
            name, False, f"could not remove the fix to test without it: {reverted.output[-300:]}"
        )
    else:
        without = sandbox.run_tests(plan, timeout_minutes)
        if without.timed_out:
            result = RuleResult(name, False, "tests timed out without the fix; inconclusive")
        elif without.ok:
            result = RuleResult(name, False, "tests still pass without the fix, so they do not test it")
        else:
            result = RuleResult(name, True, "the new or changed tests fail without the fix")
    for path, content in saved.items():
        sandbox.write_bytes(path, content)
    if sandbox.diff() != diff:
        return RuleResult(name, False, "the working copy changed during verification")
    return result


@dataclass(frozen=True)
class LintCheck:
    tool: str
    command: str


def _python_files(files: list[str]) -> list[str]:
    return [f for f in files if f.endswith(".py")]


def detect_linters(
    root_files: set[str], read: Callable[[str], str | None], changed: list[str], ecosystem: str
) -> list[LintCheck]:
    """Linters and formatters the project configures, on the changed files where the tool allows it."""
    checks: list[LintCheck] = []
    if ecosystem == "python":
        py = " ".join(shlex.quote(f) for f in _python_files(changed))
        if not py:
            return []
        pyproject: dict[str, Any] = {}
        text = read("pyproject.toml")
        if text:
            try:
                pyproject = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                pyproject = {}
        tools = pyproject.get("tool", {})
        if "ruff" in tools or {"ruff.toml", ".ruff.toml"} & root_files:
            checks.append(LintCheck("ruff", f"ruff check {py}"))
            if "format" in tools.get("ruff", {}):
                checks.append(LintCheck("ruff format", f"ruff format --check {py}"))
        if "black" in tools:
            checks.append(LintCheck("black", f"black --check {py}"))
        if "isort" in tools or ".isort.cfg" in root_files:
            checks.append(LintCheck("isort", f"isort --check-only {py}"))
        setup_cfg = read("setup.cfg") or ""
        if ".flake8" in root_files or "[flake8]" in setup_cfg:
            checks.append(LintCheck("flake8", f"flake8 {py}"))
    elif ecosystem == "maven":
        pom = read("pom.xml") or ""
        mvn = "./mvnw" if "mvnw" in root_files else "mvn"
        flags = "-B -q -o -Dmaven.repo.local=/work/deps/m2"
        if "spotless-maven-plugin" in pom:
            checks.append(LintCheck("spotless", f"{mvn} {flags} spotless:check"))
        if "maven-checkstyle-plugin" in pom:
            checks.append(LintCheck("checkstyle", f"{mvn} {flags} checkstyle:check"))
    elif ecosystem == "gradle":
        build = (read("build.gradle") or "") + (read("build.gradle.kts") or "")
        gradle = "./gradlew" if "gradlew" in root_files else "gradle"
        flags = "--no-daemon --offline --gradle-user-home /work/deps/gradle"
        if "spotless" in build:
            checks.append(LintCheck("spotless", f"{gradle} {flags} spotlessCheck"))
        if "checkstyle" in build:
            checks.append(LintCheck("checkstyle", f"{gradle} {flags} checkstyleMain checkstyleTest"))
    return checks


DOC_SUFFIXES = (".md", ".rst")


def is_documentation_only(files: list[str]) -> bool:
    """True when every changed file is documentation: ``*.md``, ``*.rst`` or anything under ``docs/``."""
    return bool(files) and all(f.lower().endswith(DOC_SUFFIXES) or f.startswith("docs/") for f in files)


def detect_docs_build(root_files: set[str], read: Callable[[str], str | None]) -> LintCheck | None:
    """The project's documentation build, if one is configured (MkDocs or Sphinx), in strict mode."""
    for name in ("mkdocs.yml", "mkdocs.yaml"):
        if name in root_files:
            return LintCheck(
                "mkdocs", f"mkdocs build --strict --config-file {name} --site-dir /tmp/mergeworthy-site"
            )
    for conf in ("docs/conf.py", "doc/conf.py", "docs/source/conf.py"):
        if read(conf) is not None:
            source = conf.rsplit("/", 1)[0]
            return LintCheck("sphinx", f"python -m sphinx -W -b html {source} /tmp/mergeworthy-docs")
    return None


def rule_docs_build(sandbox: VerifySandbox, plan: BuildPlan, check: LintCheck | None) -> RuleResult:
    if check is None:
        return RuleResult("docs_build", True, "no documentation build configured")
    result = sandbox.run(plan, check.command)
    details = {"tool": check.tool, "exit_code": result.exit_code, "timed_out": result.timed_out}
    if result.exit_code == NOT_FOUND:
        return RuleResult("docs_build", False, f"{check.tool} is configured but not installed", details)
    if not result.ok:
        return RuleResult(
            "docs_build", False, f"{check.tool} build fails: {result.output[-300:].strip()}", details
        )
    return RuleResult("docs_build", True, f"{check.tool} build passes", details)


def rule_linters(sandbox: VerifySandbox, plan: BuildPlan, checks: list[LintCheck]) -> RuleResult:
    if not checks:
        return RuleResult("linters", True, "no linters or formatters configured for the changed files")
    failed: list[str] = []
    ran: list[dict[str, Any]] = []
    for check in checks:
        result = sandbox.run(plan, check.command)
        ran.append({"tool": check.tool, "exit_code": result.exit_code, "timed_out": result.timed_out})
        if result.exit_code == NOT_FOUND:
            failed.append(f"{check.tool} is configured but not installed in the sandbox")
        elif not result.ok:
            failed.append(f"{check.tool} reports problems: {result.output[-300:].strip()}")
    if failed:
        return RuleResult("linters", False, "; ".join(failed), {"checks": ran})
    return RuleResult("linters", True, f"{', '.join(c.tool for c in checks)} passed", {"checks": ran})


def verify_attempt(
    sandbox: VerifySandbox,
    plan: BuildPlan,
    config: Config,
    *,
    diff: str,
    stats: dict[str, Any],
    baseline: dict[str, Any] | None,
    root_files: set[str],
) -> VerificationReport:
    minutes = config.eligibility.max_test_minutes
    rules = [rule_baseline(baseline)]
    scope = rule_scope(diff, stats, config)
    if not scope.passed:
        # An unsafe or oversized patch is not worth running tests for.
        return VerificationReport([*rules, scope])
    rules.append(rule_tests_pass(sandbox.run_tests(plan, minutes)))
    files: list[str] = stats.get("files", [])
    if is_documentation_only(files):
        rules.append(
            RuleResult(
                "tests_fail_without_fix", True, "documentation-only change: exempt (docs/policy.md section 4)"
            )
        )
        rules.append(rule_docs_build(sandbox, plan, detect_docs_build(root_files, sandbox.read_text)))
    else:
        rules.append(rule_tests_fail_without_fix(sandbox, plan, diff, stats, minutes))
    rules.append(scope)
    checks = detect_linters(root_files, sandbox.read_text, stats.get("files", []), plan.ecosystem)
    rules.append(rule_linters(sandbox, plan, checks))
    return VerificationReport(rules)
