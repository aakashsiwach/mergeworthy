"""End to end from an eligible issue to a diff, with the fake agent and an in-memory sandbox (no Docker)."""

import difflib
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import NOW, FakeGitHub, load_fixture, make_issue

from mergeworthy.agent import AgentSandbox, AttemptResult, AttemptStatus, Budget, Task
from mergeworthy.agent.fake import FakeAgent
from mergeworthy.config import Config
from mergeworthy.discover import check_and_record
from mergeworthy.ledger import AttemptState, Ledger
from mergeworthy.recheck import NoLongerAvailable
from mergeworthy.review import ReviewRequest, ReviewResult, Verdict
from mergeworthy.sandbox import BuildPlan, RunResult, SandboxSettings, detect_build
from mergeworthy.work import diff_stats, run_attempt

REPO = "good/repo"
CALCULATOR = "src/main/java/example/Calculator.java"
FIXTURES = Path(__file__).parent / "fixtures"


class MemorySandbox:
    """Behaves like ``Sandbox`` for the pipeline, but copies files on the host and diffs with difflib."""

    def __init__(
        self,
        attempt_id: int,
        settings: SandboxSettings,
        source: Path,
        install_ok: bool = True,
        baseline: str = "pass",
        tests_pass_always: bool = False,
    ) -> None:
        self._workdir = settings.work_root / str(attempt_id) / "repo"
        self.settings = settings
        self.source = source
        self.install_ok = install_ok
        self.baseline = baseline
        self.baseline_done = False
        self.ignored: list[str] = []
        self.tests_pass_always = tests_pass_always
        self.cloned: tuple[str, str] | None = None
        self.commands: list[str] = []
        self.snapshot: dict[str, str] = {}

    @property
    def workdir(self) -> Path:
        return self._workdir

    def prepare(self) -> None:
        self._workdir.mkdir(parents=True)

    def clone(self, full_name: str, commit: str) -> RunResult:
        self.cloned = (full_name, commit)
        shutil.copytree(self.source, self._workdir, dirs_exist_ok=True)
        self.snapshot = self._files()
        return RunResult(0, "", False, 0.0)

    def detect(self, test_command: str | None = None) -> BuildPlan:
        names = {p.name for p in self._workdir.iterdir()}
        plan = detect_build(names, lambda _: None, self.settings, test_command=test_command)
        assert plan is not None
        return plan

    def install(self, plan: BuildPlan) -> RunResult:
        return RunResult(
            0 if self.install_ok else 1, "" if self.install_ok else "resolution failed", False, 0.0
        )

    def run_tests(self, plan: BuildPlan, timeout_minutes: float) -> RunResult:
        if not self.baseline_done:
            self.baseline_done = True
            self.commands.append(f"baseline:{timeout_minutes}")
            if self.baseline == "timeout":
                return RunResult(-1, "", True, timeout_minutes * 60)
            return RunResult(0 if self.baseline == "pass" else 1, "Tests run: 1, Failures: 1", False, 3.0)
        files = self._files()
        fixed = "addExact" in files.get(CALCULATOR, "")
        passed = self.tests_pass_always or "src/test/java/example/OverflowTest.java" not in files or fixed
        self.commands.append("tests:pass" if passed else "tests:fail")
        return RunResult(0 if passed else 1, "Tests run: 2", False, 3.0)

    def root_files(self) -> set[str]:
        return {p.name for p in self._workdir.iterdir()}

    def read_text(self, relative: str, limit: int = 1_000_000) -> str | None:
        path = self._workdir / relative
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def read_bytes(self, relative: str) -> bytes | None:
        path = self._workdir / relative
        return path.read_bytes() if path.is_file() else None

    def write_bytes(self, relative: str, content: bytes | None) -> None:
        path = self._workdir / relative
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)

    def ignored_files(self) -> list[str]:
        return list(self.ignored)

    def restore_from_head(self, restore: list[str], remove: list[str]) -> RunResult:
        for path in restore:
            (self._workdir / path).write_bytes(self.snapshot[path].encode())
        for path in remove:
            (self._workdir / path).unlink(missing_ok=True)
        return RunResult(0, "", False, 0.0)

    def run(self, plan: BuildPlan, command: str, *, timeout_minutes: float | None = None) -> RunResult:
        self.commands.append(command)
        return RunResult(0, "Tests run: 1, Failures: 0", False, 0.0)

    def diff(self) -> str:
        after = self._files()
        chunks = []
        for path in sorted(set(self.snapshot) | set(after)):
            old, new = self.snapshot.get(path, ""), after.get(path, "")
            if old != new:
                chunks.append(f"diff --git a/{path} b/{path}\n")
                if path not in self.snapshot:
                    chunks.append("new file mode 100644\n")
                chunks.extend(
                    difflib.unified_diff(
                        old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}"
                    )
                )
        return "".join(chunks)

    def _files(self) -> dict[str, str]:
        return {
            p.relative_to(self._workdir).as_posix(): p.read_text(encoding="utf-8")
            for p in self._workdir.rglob("*")
            if p.is_file()
        }


class FakeReviewer:
    def __init__(self, verdict: Verdict = Verdict.ACCEPT) -> None:
        self.verdict = verdict
        self.requests: list[ReviewRequest] = []

    def review(self, request: ReviewRequest) -> ReviewResult:
        self.requests.append(request)
        reasons = [] if self.verdict is Verdict.ACCEPT else ["likely_merged: the change is too clever"]
        return ReviewResult(self.verdict, {}, reasons, "1", "fake-model", {}, 0.05)


ACCEPT = FakeReviewer()


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "ledger.sqlite3") as led:
        yield led


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config.model_validate({"sandbox": {"work_root": str(tmp_path / "work")}})


def github() -> FakeGitHub:
    fake = load_fixture("welcoming", full_name=REPO, language="Java")
    fake.issues[12] = make_issue(REPO, 12, title="Calculator.add returns the wrong sum")
    return FakeGitHub(fake)


def eligible(gh: FakeGitHub, ledger: Ledger) -> int:
    outcome = check_and_record(gh, ledger, Config(languages=["java"]), f"{REPO}#12", now=NOW)
    assert outcome.attempt_state is AttemptState.ELIGIBLE, outcome.repository.summary()
    assert outcome.attempt_id is not None
    return outcome.attempt_id


FIX = {
    "src/main/java/example/Calculator.java": (
        "package example;\n\npublic final class Calculator {\n    private Calculator() {}\n\n"
        "    public static int add(int a, int b) {\n        return Math.addExact(a, b);\n    }\n}\n"
    ),
    "src/test/java/example/OverflowTest.java": "package example;\n\nclass OverflowTest {}\n",
}


def factory(source: Path = FIXTURES / "maven-sample", **kwargs: object) -> object:
    boxes: list[MemorySandbox] = []

    def make(attempt_id: int, settings: SandboxSettings) -> MemorySandbox:
        box = MemorySandbox(attempt_id, settings, source, **kwargs)
        boxes.append(box)
        return box

    make.boxes = boxes  # type: ignore[attr-defined]
    return make


def test_issue_to_diff_with_fake_agent(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    agent = FakeAgent(files=dict(FIX), commands=["mvn -o test"], spend_usd=0.25)
    make = factory()
    outcome = run_attempt(gh, ledger, config, attempt_id, agent, reviewer=ACCEPT, sandbox_factory=make)

    assert outcome.state is AttemptState.AWAITING_APPROVAL, outcome.reason
    assert outcome.result is not None
    diff = outcome.result.diff
    assert "+        return Math.addExact(a, b);" in diff
    assert "-        return a + b;" in diff
    assert "src/test/java/example/OverflowTest.java" in diff

    [box] = make.boxes  # type: ignore[attr-defined]
    assert box.cloned == (REPO, "c0ffee")
    assert box.commands == ["baseline:15", "mvn -o test", "tests:pass", "tests:fail"]
    [seen] = agent.seen
    assert seen.issue_title == "Calculator.add returns the wrong sum"
    assert seen.test_command.endswith("-o test")

    details = ledger.attempt_details(attempt_id)
    assert details["state"] == "awaiting_approval"
    assert details["diff"] == diff
    assert details["explanation"] == "Applied the predefined patch."
    assert details["commands"] == [{"command": "mvn -o test", "exit_code": 0, "timed_out": False}]
    assert [r["rule"] for r in details["verification"]] == [
        "baseline", "tests_pass", "tests_fail_without_fix", "scope", "linters",
        "untracked_build_output",
    ]  # fmt: skip
    assert all(r["passed"] for r in details["verification"])
    assert details["review_verdict"] == "accept"
    assert details["review_reasons"]["rubric_version"] == "1"
    states = [t.to_state for t in ledger.transitions(attempt_id)]
    assert states[-3:] == [AttemptState.WORKING, AttemptState.SELF_REVIEWED, AttemptState.AWAITING_APPROVAL]
    assert details["spend_usd"] == pytest.approx(0.30)  # agent 0.25 + review 0.05
    assert details["branch"] == "mergeworthy/12-calculator-add-returns-the-wrong-sum"
    assert details["base_commit"] == "c0ffee"
    assert details["diff_stats"]["files"] == sorted(FIX)
    assert details["test_results"]["baseline"]["exit_code"] == 0
    assert details["diff_stats"]["test_files"] == ["src/test/java/example/OverflowTest.java"]
    assert details["diff_stats"]["lines_changed_excluding_tests"] == 2
    assert any("agent started on main at c0ffee" in t.reason for t in ledger.transitions(attempt_id))


def test_issue_taken_before_start_abandons_attempt(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    gh.repos[REPO].issues[12] = make_issue(REPO, 12, assignees=["someone"])
    make = factory()
    with pytest.raises(NoLongerAvailable):
        run_attempt(
            gh, ledger, config, attempt_id, FakeAgent(files=dict(FIX)), reviewer=ACCEPT, sandbox_factory=make
        )
    assert ledger.get_attempt(attempt_id).state is AttemptState.ABANDONED
    assert make.boxes == []  # type: ignore[attr-defined]  # no sandbox was created


def test_only_eligible_or_claimed_attempts_start(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    ledger.transition(attempt_id, AttemptState.SKIPPED, "changed our mind")
    with pytest.raises(ValueError, match="starts only from eligible or claimed"):
        run_attempt(
            gh, ledger, config, attempt_id, FakeAgent(files={}), reviewer=ACCEPT, sandbox_factory=factory()
        )


def test_install_failure_fails_attempt(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    agent = FakeAgent(files=dict(FIX))
    outcome = run_attempt(
        gh, ledger, config, attempt_id, agent, reviewer=ACCEPT, sandbox_factory=factory(install_ok=False)
    )
    assert outcome.state is AttemptState.FAILED
    assert "dependency installation failed" in outcome.reason
    assert agent.seen == []


def test_no_changes_fails_attempt(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    outcome = run_attempt(
        gh, ledger, config, attempt_id, FakeAgent(files={}), reviewer=ACCEPT, sandbox_factory=factory()
    )
    assert outcome.state is AttemptState.FAILED
    assert outcome.reason == "the agent made no changes"


class CappedAgent:
    def attempt(self, task: Task, workdir: Path, sandbox: AgentSandbox, budget: Budget) -> AttemptResult:
        assert budget.max_spend_usd == 2.0
        assert budget.max_minutes == 30
        return AttemptResult(AttemptStatus.BUDGET_EXCEEDED, spend_usd=2.05, error="spend cap reached")


class CrashingAgent:
    def attempt(self, task: Task, workdir: Path, sandbox: AgentSandbox, budget: Budget) -> AttemptResult:
        raise RuntimeError("CLI not found")


def test_budget_exceeded_fails_attempt_and_records_spend(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    outcome = run_attempt(
        gh, ledger, config, attempt_id, CappedAgent(), reviewer=ACCEPT, sandbox_factory=factory()
    )
    assert outcome.state is AttemptState.FAILED
    assert "budget_exceeded" in outcome.reason
    assert ledger.attempt_details(attempt_id)["spend_usd"] == 2.05


def test_agent_crash_fails_attempt(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    outcome = run_attempt(
        gh, ledger, config, attempt_id, CrashingAgent(), reviewer=ACCEPT, sandbox_factory=factory()
    )
    assert outcome.state is AttemptState.FAILED
    assert "RuntimeError: CLI not found" in outcome.reason


def test_fake_agent_refuses_paths_outside_working_copy(tmp_path: Path) -> None:
    agent = FakeAgent(files={"../escape.txt": "x"})
    box = MemorySandbox(1, SandboxSettings(work_root=tmp_path), FIXTURES / "maven-sample")
    box.prepare()
    plan = BuildPlan("maven", "m", "i", "t")
    from mergeworthy.work import BoundSandbox

    with pytest.raises(ValueError, match="escapes"):
        agent.attempt(_task(), box.workdir, BoundSandbox(box, plan), Budget(1, 1, 1))


def _task() -> Task:
    return Task("o/r", 1, "https://github.com/o/r/issues/1", "t", "b", "pytest")


def test_diff_stats_separates_tests() -> None:
    diff = (
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/tests/test_app.py b/tests/test_app.py\n--- /dev/null\n+++ b/tests/test_app.py\n"
        "@@ -0,0 +1,2 @@\n+def test_x():\n+    assert True\n"
    )
    stats = diff_stats(diff)
    assert stats["files"] == ["src/app.py", "tests/test_app.py"]
    assert stats["test_files"] == ["tests/test_app.py"]
    assert stats["lines_changed_excluding_tests"] == 2
    assert stats["test_lines_changed"] == 2
    assert diff_stats("")["files"] == []


@pytest.mark.parametrize(
    ("baseline", "reason"), [("fail", "baseline tests fail"), ("timeout", "longer than 15")]
)
def test_baseline_must_pass_in_time(ledger: Ledger, config: Config, baseline: str, reason: str) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    agent = FakeAgent(files=dict(FIX))
    outcome = run_attempt(
        gh, ledger, config, attempt_id, agent, reviewer=ACCEPT, sandbox_factory=factory(baseline=baseline)
    )
    assert outcome.state is AttemptState.FAILED
    assert reason in outcome.reason
    assert agent.seen == []


def test_review_rejection_rejects_attempt(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    reviewer = FakeReviewer(Verdict.REJECT)
    agent = FakeAgent(files=dict(FIX))
    outcome = run_attempt(gh, ledger, config, attempt_id, agent, reviewer=reviewer, sandbox_factory=factory())
    assert outcome.state is AttemptState.REJECTED
    assert "too clever" in outcome.reason
    [request] = reviewer.requests
    assert request.diff == outcome.result.diff  # type: ignore[union-attr]
    assert request.issue_title == "Calculator.add returns the wrong sum"
    assert not hasattr(request, "explanation")  # the reviewer never sees the agent's reasoning
    assert ledger.attempt_details(attempt_id)["review_verdict"] == "reject"


def test_verification_failure_fails_attempt_before_review(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    reviewer = FakeReviewer()
    make = factory(tests_pass_always=True)  # the new test passes even without the fix
    outcome = run_attempt(
        gh, ledger, config, attempt_id, FakeAgent(files=dict(FIX)), reviewer=reviewer, sandbox_factory=make
    )
    assert outcome.state is AttemptState.FAILED
    assert "tests_fail_without_fix: tests still pass without the fix" in outcome.reason
    assert reviewer.requests == []
    stored = ledger.attempt_details(attempt_id)["verification"]
    assert {r["rule"]: r["passed"] for r in stored}["tests_fail_without_fix"] is False
