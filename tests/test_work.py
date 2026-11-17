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
from mergeworthy.sandbox import BuildPlan, RunResult, SandboxSettings, detect_build
from mergeworthy.work import diff_stats, run_attempt

REPO = "good/repo"
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
    ) -> None:
        self._workdir = settings.work_root / str(attempt_id) / "repo"
        self.settings = settings
        self.source = source
        self.install_ok = install_ok
        self.baseline = baseline
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
        self.commands.append(f"baseline:{timeout_minutes}")
        if self.baseline == "timeout":
            return RunResult(-1, "", True, timeout_minutes * 60)
        return RunResult(0 if self.baseline == "pass" else 1, "Tests run: 1, Failures: 1", False, 3.0)

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
        box = MemorySandbox(attempt_id, settings, source, **kwargs)  # type: ignore[arg-type]
        boxes.append(box)
        return box

    make.boxes = boxes  # type: ignore[attr-defined]
    return make


def test_issue_to_diff_with_fake_agent(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    agent = FakeAgent(files=dict(FIX), commands=["mvn -o test"], spend_usd=0.25)
    make = factory()
    outcome = run_attempt(gh, ledger, config, attempt_id, agent, sandbox_factory=make)  # type: ignore[arg-type]

    assert outcome.state is AttemptState.WORKING
    assert outcome.result is not None
    diff = outcome.result.diff
    assert "+        return Math.addExact(a, b);" in diff
    assert "-        return a + b;" in diff
    assert "src/test/java/example/OverflowTest.java" in diff

    [box] = make.boxes  # type: ignore[attr-defined]
    assert box.cloned == (REPO, "c0ffee")
    assert box.commands == ["baseline:15", "mvn -o test"]
    [seen] = agent.seen
    assert seen.issue_title == "Calculator.add returns the wrong sum"
    assert seen.test_command.endswith("-o test")

    details = ledger.attempt_details(attempt_id)
    assert details["state"] == "working"
    assert details["spend_usd"] == 0.25
    assert details["branch"] == "mergeworthy/issue-12"
    assert details["diff_stats"]["files"] == sorted(FIX)
    assert details["test_results"]["baseline"]["exit_code"] == 0
    assert details["diff_stats"]["test_files"] == ["src/test/java/example/OverflowTest.java"]
    assert details["diff_stats"]["lines_changed_excluding_tests"] == 2
    assert "agent started on main at c0ffee" in ledger.transitions(attempt_id)[-1].reason


def test_issue_taken_before_start_abandons_attempt(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    gh.repos[REPO].issues[12] = make_issue(REPO, 12, assignees=["someone"])
    make = factory()
    with pytest.raises(NoLongerAvailable):
        run_attempt(gh, ledger, config, attempt_id, FakeAgent(files=dict(FIX)), sandbox_factory=make)  # type: ignore[arg-type]
    assert ledger.get_attempt(attempt_id).state is AttemptState.ABANDONED
    assert make.boxes == []  # type: ignore[attr-defined]  # no sandbox was created


def test_only_eligible_or_claimed_attempts_start(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    ledger.transition(attempt_id, AttemptState.SKIPPED, "changed our mind")
    with pytest.raises(ValueError, match="starts only from eligible or claimed"):
        run_attempt(gh, ledger, config, attempt_id, FakeAgent(files={}), sandbox_factory=factory())  # type: ignore[arg-type]


def test_install_failure_fails_attempt(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    agent = FakeAgent(files=dict(FIX))
    outcome = run_attempt(gh, ledger, config, attempt_id, agent, sandbox_factory=factory(install_ok=False))  # type: ignore[arg-type]
    assert outcome.state is AttemptState.FAILED
    assert "dependency installation failed" in outcome.reason
    assert agent.seen == []


def test_no_changes_fails_attempt(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    outcome = run_attempt(gh, ledger, config, attempt_id, FakeAgent(files={}), sandbox_factory=factory())  # type: ignore[arg-type]
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
    outcome = run_attempt(gh, ledger, config, attempt_id, CappedAgent(), sandbox_factory=factory())  # type: ignore[arg-type]
    assert outcome.state is AttemptState.FAILED
    assert "budget_exceeded" in outcome.reason
    assert ledger.attempt_details(attempt_id)["spend_usd"] == 2.05


def test_agent_crash_fails_attempt(ledger: Ledger, config: Config) -> None:
    gh = github()
    attempt_id = eligible(gh, ledger)
    outcome = run_attempt(gh, ledger, config, attempt_id, CrashingAgent(), sandbox_factory=factory())  # type: ignore[arg-type]
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
    outcome = run_attempt(gh, ledger, config, attempt_id, agent, sandbox_factory=factory(baseline=baseline))  # type: ignore[arg-type]
    assert outcome.state is AttemptState.FAILED
    assert reason in outcome.reason
    assert agent.seen == []
