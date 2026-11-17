from pathlib import Path

import pytest
from fakes import FakeGitHub, load_fixture, make_issue
from test_work import FIX, factory
from typer.testing import CliRunner

from mergeworthy import cli
from mergeworthy.agent.fake import FakeAgent
from mergeworthy.ledger import AttemptState, Ledger

runner = CliRunner()
REPO = "good/repo"


def github() -> FakeGitHub:
    good = load_fixture("welcoming", full_name=REPO, language="Java")
    good.issues[12] = make_issue(REPO, 12, title="Calculator.add returns the wrong sum")
    good.issues[13] = make_issue(REPO, 13, assignees=["someone"])
    unclear = load_fixture("ai_unclear", full_name="unclear/repo", language="Java")
    unclear.issues[1] = make_issue("unclear/repo", 1)
    claim = load_fixture("claim_first", full_name="claim/repo", language="Java")
    claim.issues[1] = make_issue("claim/repo", 1)
    return FakeGitHub(good, unclear, claim)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    ledger_path = (tmp_path / "ledger.sqlite3").as_posix()
    work_root = (tmp_path / "work").as_posix()
    config = tmp_path / "mergeworthy.toml"
    config.write_text(
        f'languages = ["java"]\nledger_path = "{ledger_path}"\n[sandbox]\nwork_root = "{work_root}"\n',
        encoding="utf-8",
    )
    agent = FakeAgent(files=dict(FIX), commands=["mvn -o test"], spend_usd=0.3)
    make = factory()
    monkeypatch.setattr(cli, "make_client", github)
    monkeypatch.setattr(cli, "make_agent", lambda config: agent)
    monkeypatch.setattr(cli, "make_sandbox_factory", lambda: make)
    monkeypatch.setattr(cli, "sandbox_ready", lambda: True)
    return {"config": str(config), "ledger": ledger_path, "agent": agent, "make": make}


def invoke(env: dict[str, object], *args: str) -> object:
    return runner.invoke(cli.app, ["work", *args, "-c", str(env["config"])])


def test_dry_run_is_the_default_and_changes_nothing(env: dict[str, object]) -> None:
    result = invoke(env, "https://github.com/good/repo/issues/12")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    out = result.output  # type: ignore[attr-defined]
    assert "dry run: good/repo#12" in out
    assert "clone good/repo at main c0ffee" in out
    assert "nothing was started" in out
    assert env["agent"].seen == []  # type: ignore[attr-defined]
    assert env["make"].boxes == []  # type: ignore[attr-defined]
    with Ledger(str(env["ledger"])) as ledger:
        assert ledger.get_repository(REPO) is None
        assert ledger.attempts_for_subject(REPO) == []


def test_run_produces_a_diff(env: dict[str, object]) -> None:
    result = invoke(env, "good/repo#12", "--no-dry-run")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "working (diff ready for verification)" in result.output  # type: ignore[attr-defined]
    assert "spend: $0.30" in result.output  # type: ignore[attr-defined]
    with Ledger(str(env["ledger"])) as ledger:
        [attempt] = ledger.attempts_for_subject(f"{REPO}#12")
        assert attempt.state is AttemptState.WORKING
        assert ledger.attempt_details(attempt.id)["diff_stats"]["files"] == sorted(FIX)


def test_run_needs_docker(env: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "sandbox_ready", lambda: False)
    result = invoke(env, "good/repo#12", "--no-dry-run")
    assert result.exit_code == 1
    assert "Docker is not available" in result.output  # type: ignore[attr-defined]
    assert env["agent"].seen == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("target", "code", "text"),
    [
        ("good/repo#13", 1, "not eligible"),
        ("unclear/repo#1", 3, "needs a human decision"),
        ("claim/repo#1", 1, "claim issues first"),
        ("good/repo", 1, "work needs an issue"),
    ],
)
def test_refusals(env: dict[str, object], target: str, code: int, text: str) -> None:
    for flag in ("--dry-run", "--no-dry-run"):
        result = invoke(env, target, flag)
        assert result.exit_code == code, result.output  # type: ignore[attr-defined]
        assert text in result.output  # type: ignore[attr-defined]
    assert env["agent"].seen == []  # type: ignore[attr-defined]
