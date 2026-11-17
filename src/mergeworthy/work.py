"""Run one attempt from an eligible issue to a diff.

Steps: re-check the issue (``recheck.ensure_still_available``), move the attempt to ``working``, clone the
default branch at its current commit into a fresh sandbox, install dependencies, run the baseline test suite,
hand the task to the coding agent, and record the diff and spend. Verification and self-review (T6) take it
from there; any failure here moves the attempt to ``failed`` with the reason.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mergeworthy.agent import AttemptResult, AttemptStatus, Budget, CodingAgent, Task
from mergeworthy.config import Config
from mergeworthy.ledger import AttemptState, Ledger
from mergeworthy.recheck import IssueReader, ensure_still_available
from mergeworthy.sandbox import BuildPlan, RunResult, Sandbox, SandboxError, SandboxSettings

STARTABLE = frozenset({AttemptState.ELIGIBLE, AttemptState.CLAIMED})
_TEST_PATH = re.compile(
    r"(^|/)(tests?|testing|spec|__tests__)(/|$)|(^|/)test_[^/]*$|_test\.[a-z]+$|Tests?\.java$"
)


class WorkSandbox(Protocol):
    """The parts of ``Sandbox`` the pipeline uses; tests provide an in-memory version."""

    @property
    def workdir(self) -> Path: ...

    def prepare(self) -> None: ...
    def clone(self, full_name: str, commit: str) -> RunResult: ...
    def detect(self, test_command: str | None = None) -> BuildPlan: ...
    def install(self, plan: BuildPlan) -> RunResult: ...
    def run_tests(self, plan: BuildPlan, timeout_minutes: float) -> RunResult: ...
    def run(self, plan: BuildPlan, command: str, *, timeout_minutes: float | None = None) -> RunResult: ...
    def diff(self) -> str: ...


SandboxFactory = Callable[[int, SandboxSettings], WorkSandbox]


def default_sandbox(attempt_id: int, settings: SandboxSettings) -> WorkSandbox:
    return Sandbox(attempt_id, settings)


@dataclass(frozen=True)
class BoundSandbox:
    """A sandbox bound to its build plan: what the agent gets."""

    sandbox: WorkSandbox
    plan: BuildPlan

    @property
    def workdir(self) -> Path:
        return self.sandbox.workdir

    def run(self, command: str) -> RunResult:
        return self.sandbox.run(self.plan, command)

    def diff(self) -> str:
        return self.sandbox.diff()


@dataclass(frozen=True)
class WorkOutcome:
    attempt_id: int
    state: AttemptState
    result: AttemptResult | None
    reason: str


def diff_stats(diff: str) -> dict[str, Any]:
    """Files and changed lines in a unified diff, with test files counted separately."""
    files: dict[str, dict[str, int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            current = match.group(2) if match else line[len("diff --git ") :]
            files.setdefault(current, {"added": 0, "removed": 0})
        elif current is None or line.startswith(("+++", "---")):
            continue
        elif line.startswith("+"):
            files[current]["added"] += 1
        elif line.startswith("-"):
            files[current]["removed"] += 1
    tests = {path for path in files if _TEST_PATH.search(path)}
    changed = sum(v["added"] + v["removed"] for path, v in files.items() if path not in tests)
    test_changed = sum(v["added"] + v["removed"] for path, v in files.items() if path in tests)
    return {
        "files": sorted(files),
        "test_files": sorted(tests),
        "lines_changed_excluding_tests": changed,
        "test_lines_changed": test_changed,
    }


def _fail(ledger: Ledger, attempt_id: int, reason: str, result: AttemptResult | None = None) -> WorkOutcome:
    ledger.transition(attempt_id, AttemptState.FAILED, reason)
    return WorkOutcome(attempt_id, AttemptState.FAILED, result, reason)


def run_attempt(
    gh: IssueReader,
    ledger: Ledger,
    config: Config,
    attempt_id: int,
    agent: CodingAgent,
    *,
    sandbox_factory: SandboxFactory = default_sandbox,
) -> WorkOutcome:
    attempt = ledger.get_attempt(attempt_id)
    if attempt.state not in STARTABLE:
        raise ValueError(
            f"attempt {attempt_id} is {attempt.state}; work starts only from eligible or claimed"
        )
    # Raises NoLongerAvailable (attempt abandoned) or AwaitingHumanDecision (attempt unchanged).
    ensure_still_available(gh, ledger, config, attempt_id)

    issue_record = ledger.get_issue(attempt.issue_id)
    assert issue_record is not None  # ensure_still_available checked it
    parts = issue_record["url"].rstrip("/").split("/")
    full_name, number = f"{parts[-4]}/{parts[-3]}", int(parts[-1])
    issue = gh.get_issue(full_name, number)
    repo = gh.get_repository(full_name)
    commits = gh.list_commits(full_name, branch=repo.default_branch, limit=1)
    if not commits:
        raise ValueError(f"{full_name} has no commits on {repo.default_branch}")
    commit = commits[0].sha
    ledger.transition(attempt_id, AttemptState.WORKING, f"agent started on {repo.default_branch} at {commit}")
    ledger.update_attempt(attempt_id, branch=f"mergeworthy/issue-{number}")

    box = sandbox_factory(attempt_id, config.sandbox)
    try:
        box.prepare()
        cloned = box.clone(full_name, commit)
        if not cloned.ok:
            return _fail(ledger, attempt_id, f"clone failed: {cloned.output[-500:]}")
        plan = box.detect()
        installed = box.install(plan)
        if not installed.ok:
            reason = "timed out" if installed.timed_out else installed.output[-500:]
            return _fail(ledger, attempt_id, f"dependency installation failed: {reason}")
        # Baseline: the suite must pass before any change (verification rule 1) and within the time limit
        # (policy.md section 1). For Maven this also fetches artifacts only needed when tests run, so the
        # agent's offline test runs work.
        minutes = config.eligibility.max_test_minutes
        baseline = box.run_tests(plan, minutes)
        ledger.update_attempt(
            attempt_id,
            test_results={
                "baseline": {
                    "exit_code": baseline.exit_code,
                    "timed_out": baseline.timed_out,
                    "seconds": baseline.seconds,
                }
            },
        )
        if baseline.timed_out:
            return _fail(ledger, attempt_id, f"baseline tests took longer than {minutes} minutes")
        if not baseline.ok:
            return _fail(
                ledger, attempt_id, f"baseline tests fail before any change: {baseline.output[-500:]}"
            )
    except SandboxError as exc:
        return _fail(ledger, attempt_id, f"sandbox error: {exc}")

    task = Task(
        repository=full_name,
        issue_number=number,
        issue_url=issue.html_url,
        issue_title=issue.title,
        issue_body=issue.body,
        test_command=plan.test.splitlines()[-1],
        max_diff_lines=config.limits.max_diff_lines,
    )
    budget = Budget(
        max_spend_usd=config.budget.max_spend_usd_per_attempt,
        max_minutes=config.budget.max_minutes_per_attempt,
        max_turns=config.agent.max_turns,
    )
    try:
        result = agent.attempt(task, box.workdir, BoundSandbox(box, plan), budget)
    except Exception as exc:  # the agent is a boundary: record the failure instead of crashing the run
        return _fail(ledger, attempt_id, f"agent error: {type(exc).__name__}: {exc}")

    stats = diff_stats(result.diff)
    ledger.update_attempt(attempt_id, spend_usd=result.spend_usd, diff_stats=stats)
    if result.status is not AttemptStatus.COMPLETED:
        return _fail(ledger, attempt_id, f"agent {result.status.value}: {result.error or ''}".strip(), result)
    if not result.diff.strip():
        return _fail(ledger, attempt_id, "the agent made no changes", result)
    return WorkOutcome(attempt_id, AttemptState.WORKING, result, "diff ready for verification")
