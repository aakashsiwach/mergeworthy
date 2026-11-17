"""Real Docker runs. Opt in with ``pytest --run-integration``; skipped when Docker is unavailable."""

import itertools
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from mergeworthy.sandbox import Sandbox, SandboxSettings, docker_available

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures"
# Override with MERGEWORTHY_CODEATLAS_COMMIT to test another commit.
CODEATLAS = "aakashsiwach/codeatlas-rag"
_ids = itertools.count(90_000)


@pytest.fixture
def box(tmp_path: Path) -> Iterator[Sandbox]:
    if not docker_available():
        pytest.skip("Docker is not available")
    sandbox = Sandbox(next(_ids), SandboxSettings(work_root=tmp_path / "work"))
    sandbox.prepare()
    try:
        yield sandbox
    finally:
        sandbox.destroy()


def test_maven_fixture_tests_pass_offline(box: Sandbox) -> None:
    box.load_local(FIXTURES / "maven-sample")
    plan = box.detect()
    assert plan.ecosystem == "maven"
    install = box.install(plan)
    assert install.ok, install.output
    result = box.run_tests(plan, timeout_minutes=15)
    assert result.ok, result.output


def test_container_has_no_network_and_no_host_secrets(box: Sandbox) -> None:
    box.load_local(FIXTURES / "maven-sample")
    plan = box.detect()
    result = box.run(plan, "env; (getent hosts github.com && echo NETWORK) || echo NO_NETWORK")
    assert "NO_NETWORK" in result.output
    assert "GITHUB_TOKEN" not in result.output
    assert "ANTHROPIC_API_KEY" not in result.output


def test_codeatlas_rag_tests_run(box: Sandbox) -> None:
    commit = os.environ.get("MERGEWORTHY_CODEATLAS_COMMIT")
    if not commit:
        pytest.skip("set MERGEWORTHY_CODEATLAS_COMMIT to the commit SHA to test")
    clone = box.clone(CODEATLAS, commit)
    assert clone.ok, clone.output
    plan = box.detect()
    assert plan.ecosystem == "python"
    install = box.install(plan)
    assert install.ok, install.output
    result = box.run_tests(plan, timeout_minutes=15)
    assert result.ok, result.output
    assert box.diff() == ""


def test_fake_agent_end_to_end_in_docker(box: Sandbox) -> None:
    from mergeworthy.agent import Budget, Task
    from mergeworthy.agent.fake import FakeAgent
    from mergeworthy.work import BoundSandbox

    box.load_local(FIXTURES / "maven-sample")
    plan = box.detect()
    assert box.install(plan).ok
    assert box.run_tests(plan, timeout_minutes=15).ok  # baseline, as work.py does
    source = "src/main/java/example/Calculator.java"
    fixed = (box.workdir / source).read_text(encoding="utf-8").replace("a + b", "Math.addExact(a, b)")
    agent = FakeAgent(files={source: fixed}, commands=["mvn -B -q -Dmaven.repo.local=/work/deps/m2 -o test"])
    task = Task(
        "local/maven-sample", 1, "https://github.com/local/maven-sample/issues/1", "t", "b", plan.test
    )
    result = agent.attempt(task, box.workdir, BoundSandbox(box, plan), Budget(1.0, 10, 5))
    assert [c.exit_code for c in result.commands] == [0]
    assert "-        return a + b;" in result.diff
    assert "+        return Math.addExact(a, b);" in result.diff
