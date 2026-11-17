import tomllib
from pathlib import Path

import pytest

from mergeworthy.config import (
    Config,
    ConfigError,
    find_secrets,
    load_config,
    load_secrets,
    parse_config,
)

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "mergeworthy.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_are_safe() -> None:
    config = Config()
    assert config.dry_run is True
    assert config.allowlist == []
    assert config.limits.prs_per_week == 3
    assert config.limits.open_prs_per_repository == 1
    assert config.limits.attempts_per_issue == 1
    assert config.limits.cooldown_days_after_close == 60
    assert config.limits.max_diff_lines == 200


def test_missing_default_file_gives_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_config() == Config()


def test_missing_explicit_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_example_file_matches_defaults() -> None:
    assert load_config(ROOT / "mergeworthy.example.toml") == Config()


def test_loads_values(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "dry_run = false\n"
        'languages = ["Python"]\n'
        'allowlist = ["aakashsiwach/taintmap"]\n'
        'deny_list = ["Some-Org/Repo.js"]\n'
        "[limits]\n"
        "prs_per_week = 1\n"
        "[budget]\n"
        "max_spend_usd_per_attempt = 0.5\n",
    )
    config = load_config(path)
    assert config.dry_run is False
    assert config.languages == ["python"]
    assert config.limits.prs_per_week == 1
    assert config.limits.max_diff_lines == 200
    assert config.budget.max_spend_usd_per_attempt == 0.5
    assert config.is_allowed("AakashSiwach/Taintmap")
    assert not config.is_allowed("aakashsiwach/codeatlas-rag")
    assert config.is_denied("some-org/repo.js")


def test_prices_per_provider_with_check_date() -> None:
    model = {"m": {"input_per_mtok": 3, "output_per_mtok": 15}}
    config = parse_config({"prices": {"anthropic": {"checked": "2027-02-01", "models": model}}})
    prices = config.active_prices
    assert prices.checked is not None and prices.checked.isoformat() == "2027-02-01"
    assert prices.cost("m", 1_000_000, 100_000) == pytest.approx(4.5)
    assert prices.cost("unknown", 1, 1) is None
    defaults = Config().prices
    assert defaults.anthropic.models["claude-opus-5-5"].output_per_mtok == 20.0
    assert defaults.bedrock.checked is None and defaults.bedrock.models == {}  # never invented
    with pytest.raises(ConfigError, match="input_per_mtok"):
        parse_config(
            {"prices": {"anthropic": {"models": {"m": {"input_per_mtok": -1, "output_per_mtok": 1}}}}}
        )


BEDROCK = {
    "name": "bedrock",
    "agent_model": "us.anthropic.claude-opus-5-5",
    "review_model": "global.anthropic.claude-opus-5-5",
}


def test_provider_defaults_to_anthropic() -> None:
    provider = Config().provider
    assert (provider.name, provider.agent, provider.review) == (
        "anthropic",
        "claude-opus-5-5",
        "claude-opus-5-5",
    )


def test_bedrock_provider_uses_bedrock_prices() -> None:
    bedrock_prices = {
        "checked": "2026-11-17",
        "models": {"us.anthropic.claude-opus-5-5": {"input_per_mtok": 1, "output_per_mtok": 2}},
    }
    config = parse_config(
        {"provider": {**BEDROCK, "aws_region": "us-east-1"}, "prices": {"bedrock": bedrock_prices}}
    )
    assert config.provider.agent == "us.anthropic.claude-opus-5-5"
    assert config.provider.review == "global.anthropic.claude-opus-5-5"
    assert config.active_prices.cost("us.anthropic.claude-opus-5-5", 1_000_000, 0) == 1.0
    arn = "arn:aws:bedrock:us-east-2:123456789012:application-inference-profile/abc123"
    assert parse_config({"provider": {**BEDROCK, "agent_model": arn}}).provider.agent == arn


@pytest.mark.parametrize(
    ("provider", "message"),
    [
        ({"name": "bedrock"}, "needs agent_model"),
        ({**BEDROCK, "agent_model": "claude-opus-5-5"}, "not a Bedrock inference profile ID"),
        ({**BEDROCK, "review_model": "anthropic.claude-opus-5-5"}, "not a Bedrock inference profile ID"),
        ({**BEDROCK, "aws_region": "us east 1"}, "not an AWS region"),
        ({"name": "anthropic", "aws_region": "us-east-1"}, "only to provider bedrock"),
        ({"name": "openai"}, "name"),
    ],
)
def test_invalid_provider(provider: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config({"provider": provider})


def test_deny_list_wins_over_allowlist() -> None:
    config = parse_config({"allowlist": ["a/b"], "deny_list": ["a/b"]})
    assert not config.is_allowed("a/b")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"languages": ["rust"]}, "unsupported languages"),
        ({"languages": []}, "languages"),
        ({"labels": [" "]}, "labels must not be empty"),
        ({"allowlist": ["not-a-repo"]}, "owner/name"),
        ({"allowlist": ["a/b", "A/B"]}, "duplicate"),
        ({"limits": {"prs_per_week": -1}}, "prs_per_week"),
        ({"limits": {"max_diff_lines": 0}}, "max_diff_lines"),
        ({"budget": {"max_spend_usd_per_attempt": 0}}, "max_spend_usd_per_attempt"),
        ({"dry_run": "maybe"}, "dry_run"),
        ({"unknown_field": 1}, "unknown_field"),
        ({"limits": {"typo": 1}}, "typo"),
    ],
)
def test_invalid_values_are_rejected(data: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_invalid_toml_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(write(tmp_path, "dry_run = = true"))


@pytest.mark.parametrize(
    "text",
    [
        'github_token = "FAKE_TOKEN_FOR_TESTS"',
        'api_key = "FAKE_TOKEN_FOR_TESTS"',
        '[secrets]\nANTHROPIC_API_KEY = "FAKE_TOKEN_FOR_TESTS"',
        'labels = ["ghp_FAKE_TOKEN_FOR_TESTS"]',
        'ledger_path = "sk-ant-FAKE_TOKEN_FOR_TESTS"',
        '[limits]\npassword = "x"',
    ],
)
def test_secrets_in_file_are_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError, match="secrets must not be stored") as info:
        load_config(write(tmp_path, text))
    assert "FAKE_TOKEN_FOR_TESTS" not in str(info.value)


def test_find_secrets_reports_paths() -> None:
    data = tomllib.loads('[a]\ntoken = "x"\n[b]\nvalue = "github_pat_FAKE"')
    assert find_secrets(data) == ["a.token", "b.value"]


def test_secrets_come_from_environment() -> None:
    secrets = load_secrets({"GITHUB_TOKEN": "FAKE_TOKEN_FOR_TESTS", "ANTHROPIC_API_KEY": " "})
    assert secrets.github_token is not None
    assert secrets.github_token.get_secret_value() == "FAKE_TOKEN_FOR_TESTS"
    assert "FAKE_TOKEN_FOR_TESTS" not in repr(secrets)
    assert secrets.model_api_key is None
