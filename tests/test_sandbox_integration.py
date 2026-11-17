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
