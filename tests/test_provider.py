"""Provider credentials reach the coding agent process only: never the sandbox, the ledger or output."""

from collections.abc import Sequence
from pathlib import Path

import pytest
from fakes import NOW, FakeGitHub, load_fixture, make_issue
from test_work import ACCEPT, FIX, factory
from typer.testing import CliRunner

from mergeworthy import cli
from mergeworthy.agent import Budget, Task
from mergeworthy.agent.claude import ClaudeCodingAgent, CommandLog
from mergeworthy.agent.fake import FakeAgent
from mergeworthy.config import Config, parse_config
from mergeworthy.discover import check_and_record
from mergeworthy.ledger import Ledger
from mergeworthy.provider import AWS_CREDENTIAL_VARS, agent_environment, redact
from mergeworthy.sandbox import BuildPlan, Sandbox, SandboxSettings
from mergeworthy.work import run_attempt

SECRETS = {
    "AWS_ACCESS_KEY_ID": "AKIAFAKETOKENFORTESTS",
    "AWS_SECRET_ACCESS_KEY": "FAKE_TOKEN_FOR_TESTS_secret_access_key",
    "AWS_SESSION_TOKEN": "FAKE_TOKEN_FOR_TESTS_session_token",
    "AWS_BEARER_TOKEN_BEDROCK": "FAKE_TOKEN_FOR_TESTS_bedrock_api_key",
}
BEDROCK = {
    "name": "bedrock",
    "aws_region": "us-east-1",
    "agent_model": "us.anthropic.claude-opus-5-5",
    "review_model": "us.anthropic.claude-opus-5-5",
}


def bedrock_config(**extra: object) -> Config:
    return parse_config({"provider": BEDROCK, **extra})


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for name, value in {**SECRETS, "AWS_PROFILE": "mergeworthy-test"}.items():
        monkeypatch.setenv(name, value)
    return {**SECRETS, "AWS_PROFILE": "mergeworthy-test"}


def test_bedrock_environment_for_the_agent() -> None:
    env = agent_environment(bedrock_config(), {**SECRETS, "AWS_PROFILE": "dev", "UNRELATED": "x"})
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["ANTHROPIC_API_KEY"] == ""
    for name, value in SECRETS.items():
        assert env[name] == value
    assert env["AWS_PROFILE"] == "dev"
    assert "UNRELATED" not in env


def test_bedrock_passes_only_credentials_that_are_set() -> None:
    env = agent_environment(bedrock_config(), {"AWS_PROFILE": "dev"})
    assert env == {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_API_KEY": "",
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "dev",
    }
    no_region = agent_environment(parse_config({"provider": {**BEDROCK, "aws_region": None}}), {})
    assert "AWS_REGION" not in no_region  # the AWS profile's region applies


def test_claude_api_environment_blanks_aws_and_other_providers() -> None:
    env = agent_environment(Config(), {**SECRETS, "CLAUDE_CODE_USE_BEDROCK": "1"})
    for name in (
        *AWS_CREDENTIAL_VARS,
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_VERTEX",
    ):
        assert env[name] == ""


def test_agent_options_carry_bedrock_settings_but_never_a_github_token(tmp_path: Path) -> None:
    env = agent_environment(bedrock_config(), SECRETS)
    hostile = {**env, "GITHUB_TOKEN": "FAKE_TOKEN_FOR_TESTS_github"}
    agent = ClaudeCodingAgent(model="us.anthropic.claude-opus-5-5", provider_env=hostile)
    task = Task("o/r", 1, "https://github.com/o/r/issues/1", "t", "b", "pytest")

    class Box:
        workdir = tmp_path

        def run(self, command: str) -> object: ...
        def diff(self) -> str:
            return ""

    options = agent.build_options(task, tmp_path, Box(), Budget(1, 1, 1), CommandLog())  # type: ignore[arg-type]
    assert options.model == "us.anthropic.claude-opus-5-5"
    assert options.env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert options.env["AWS_SECRET_ACCESS_KEY"] == SECRETS["AWS_SECRET_ACCESS_KEY"]
    assert options.env["GITHUB_TOKEN"] == ""  # blanking always wins
    assert "FAKE_TOKEN" not in repr(agent)


class RecordingDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout: float) -> tuple[int, str, bool]:
        self.calls.append(list(args))
        return 0, "", False


def test_sandbox_containers_never_receive_provider_credentials(
    tmp_path: Path, aws_env: dict[str, str]
) -> None:
    docker = RecordingDocker()
    box = Sandbox(3, SandboxSettings(work_root=tmp_path), runner=docker)
    box.prepare()
    plan = BuildPlan("maven", "maven:img", "mvn install", "mvn -o test")
    box.clone("owner/repo", "abcdef1")
    box.install(plan)
    box.run_tests(plan, 1)
    box.run(plan, "env")
    box.diff()
    flat = " ".join(" ".join(call) for call in docker.calls)
    for value in aws_env.values():
        assert value not in flat
    for name in (*AWS_CREDENTIAL_VARS, "CLAUDE_CODE_USE_BEDROCK", "AWS_REGION"):
        assert name not in flat


def test_pipeline_never_writes_provider_credentials_to_the_ledger(
    tmp_path: Path, aws_env: dict[str, str]
) -> None:
    fake = load_fixture("welcoming", full_name="good/repo", language="Java")
    fake.issues[12] = make_issue("good/repo", 12)
    gh = FakeGitHub(fake)
    config = bedrock_config(languages=["java"], sandbox={"work_root": str(tmp_path / "work")})
    db = tmp_path / "ledger.sqlite3"
    with Ledger(db) as ledger:
        attempt = check_and_record(gh, ledger, config, "good/repo#12", now=NOW).attempt_id
        assert attempt is not None
        # An agent that crashes with a message containing a secret: the reason is redacted before storage.
        leaky = FakeAgent(files=dict(FIX))

        def crash(*args: object, **kwargs: object) -> object:
            raise RuntimeError(f"auth failed for {aws_env['AWS_SECRET_ACCESS_KEY']}")

        leaky.attempt = crash  # type: ignore[method-assign]
        outcome = run_attempt(gh, ledger, config, attempt, leaky, reviewer=ACCEPT, sandbox_factory=factory())
        assert "***" in outcome.reason
    raw = db.read_bytes()
    for value in aws_env.values():
        assert value.encode() not in raw


def test_cli_output_never_shows_provider_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aws_env: dict[str, str]
) -> None:
    fake = load_fixture("welcoming", full_name="good/repo", language="Java")
    fake.issues[12] = make_issue("good/repo", 12)
    cfg = tmp_path / "mergeworthy.toml"
    cfg.write_text(
        'languages = ["java"]\n'
        f'ledger_path = "{(tmp_path / "l.sqlite3").as_posix()}"\n'
        '[provider]\nname = "bedrock"\naws_region = "us-east-1"\n'
        'agent_model = "us.anthropic.claude-opus-5-5"\nreview_model = "us.anthropic.claude-opus-5-5"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "make_client", lambda: FakeGitHub(fake))
    result = CliRunner().invoke(cli.app, ["work", "good/repo#12", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "us.anthropic.claude-opus-5-5 on bedrock" in result.output
    for value in aws_env.values():
        assert value not in result.output


def test_redact() -> None:
    text = f"error: {SECRETS['AWS_SECRET_ACCESS_KEY']} rejected"
    assert redact(text, SECRETS) == "error: *** rejected"
    assert redact("short ok", {"AWS_SESSION_TOKEN": "ok"}) == "short ok"  # too short to redact safely
