"""Settings from ``mergeworthy.toml`` and environment variables.

Defaults follow docs/policy.md. Secrets are read only from environment variables; a configuration file that
contains anything that looks like a secret is rejected.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from mergeworthy.sandbox import SandboxSettings

DEFAULT_CONFIG_PATH = Path("mergeworthy.toml")

GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
MODEL_API_KEY_ENV = "ANTHROPIC_API_KEY"

SUPPORTED_LANGUAGES = frozenset({"java", "python"})

_REPO_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")

# Key names that suggest a secret. Matched against each key in the file, case-insensitively.
_SECRET_KEY_PATTERN = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|private[_-]?key|credential)", re.I
)

# Value prefixes of well-known credential formats.
_SECRET_VALUE_PATTERN = re.compile(
    r"^(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|sk-ant-|sk-|xox[abpr]-|AKIA[0-9A-Z]{16})",
)


class ConfigError(Exception):
    """The configuration file is missing required structure, is invalid, or contains a secret."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Limits(_Model):
    """docs/policy.md section 3."""

    prs_per_week: int = Field(default=3, ge=0)
    open_prs_per_repository: int = Field(default=1, ge=0)
    attempts_per_issue: int = Field(default=1, ge=0)
    cooldown_days_after_close: int = Field(default=60, ge=0)
    max_diff_lines: int = Field(default=200, ge=1)


class Eligibility(_Model):
    """Thresholds from docs/policy.md sections 1 and 2."""

    max_days_since_commit: int = Field(default=30, ge=1)
    max_days_since_outside_merge: int = Field(default=90, ge=1)
    max_test_minutes: int = Field(default=15, ge=1)
    claim_comment_days: int = Field(default=30, ge=1)


class AgentSettings(_Model):
    """The coding agent (Claude Agent SDK). The model is set in ``[provider]``."""

    max_turns: int = Field(default=80, ge=1)


ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
# Amazon Bedrock inference profile IDs (``us.anthropic.claude-opus-5-5``) or inference profile ARNs.
_BEDROCK_PROFILE = re.compile(
    r"^(?:(?:global|us|us-gov|eu|jp|au|apac)\.anthropic\.[a-z0-9.:-]+"
    r"|arn:aws(?:-us-gov)?:bedrock:[a-z0-9-]+:\d{12}:(?:application-)?inference-profile/[A-Za-z0-9.:/_-]+)$"
)
_AWS_REGION = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-\d$")


class Provider(_Model):
    """Where the models run: the Claude API (``anthropic``) or Amazon Bedrock (``bedrock``).

    Credentials never go here: ``ANTHROPIC_API_KEY`` for the Claude API; for Bedrock the AWS credential chain
    (``AWS_PROFILE``, ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY``/``AWS_SESSION_TOKEN``) or
    ``AWS_BEARER_TOKEN_BEDROCK``, all from the environment.
    """

    name: Literal["anthropic", "bedrock", "openai"] = "anthropic"
    base_url: str | None = None
    aws_region: str | None = None
    agent_model: str | None = None
    review_model: str | None = None

    @field_validator("aws_region")
    @classmethod
    def _region(cls, value: str | None) -> str | None:
        if value is not None and not _AWS_REGION.fullmatch(value):
            raise ValueError(f"not an AWS region name: {value!r}")
        return value

    @model_validator(mode="after")
    def _models(self) -> Provider:
        if self.name == "bedrock":
            for field_name in ("agent_model", "review_model"):
                value = getattr(self, field_name)
                if value is None:
                    raise ValueError(
                        f"provider bedrock needs {field_name} set to a Bedrock inference profile ID"
                    )
                if not _BEDROCK_PROFILE.fullmatch(value):
                    raise ValueError(
                        f"{field_name} {value!r} is not a Bedrock inference profile ID (for example "
                        "us.anthropic.claude-opus-5-5) or inference profile ARN"
                    )
        elif self.aws_region is not None:
            raise ValueError("aws_region applies only to provider bedrock")
        if self.name == "openai":
            from urllib.parse import urlparse

            parsed = urlparse(self.base_url or "")
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("provider openai base_url must be an HTTPS URL without embedded credentials")
            if parsed.query or parsed.fragment:
                raise ValueError("provider openai base_url must not contain a query or fragment")
        elif self.base_url is not None:
            raise ValueError("base_url applies only to provider openai")
        return self

    @property
    def agent(self) -> str:
        return self.agent_model or ANTHROPIC_DEFAULT_MODEL

    @property
    def review(self) -> str:
        return self.review_model or ANTHROPIC_DEFAULT_MODEL


class ModelPrice(_Model):
    """List price in US dollars per million tokens."""

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


def _default_prices() -> dict[str, ModelPrice]:
    return {
        "claude-opus-5-5": ModelPrice(input_per_mtok=4.0, output_per_mtok=20.0),
        "claude-sonnet-5-5": ModelPrice(input_per_mtok=2.0, output_per_mtok=10.0),
        "claude-haiku-5-5": ModelPrice(input_per_mtok=0.10, output_per_mtok=0.50),
    }


class Prices(_Model):
    """Model list prices for one provider, used to estimate self-review spend. Check them against the
    provider's published price list and update ``checked`` when you do; the date is shown next to every spend
    figure. ``checked`` is empty until someone has checked."""

    checked: date | None = None
    models: dict[str, ModelPrice] = Field(default_factory=dict)

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        price = self.models.get(model)
        if price is None:
            return None
        return (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / 1_000_000


def _anthropic_prices() -> Prices:
    return Prices(checked=date(2026, 11, 3), models=_default_prices())


class ProviderPrices(_Model):
    """``[prices.anthropic]`` and ``[prices.bedrock]``. Bedrock prices are set by AWS and are not filled in by
    default: copy them from the Amazon Bedrock pricing page (keyed by the inference profile ID you use)."""

    anthropic: Prices = Field(default_factory=_anthropic_prices)
    bedrock: Prices = Field(default_factory=Prices)
    openai: Prices = Field(default_factory=Prices)


class Budget(_Model):
    """Hard caps for one attempt."""

    max_spend_usd_per_attempt: float = Field(default=2.0, gt=0)
    max_minutes_per_attempt: int = Field(default=30, ge=1)


def _check_repo_names(values: list[str]) -> list[str]:
    cleaned = []
    for value in values:
        name = value.strip()
        if not _REPO_NAME.match(name):
            raise ValueError(f"not a repository name in owner/name form: {value!r}")
        cleaned.append(name.lower())
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("duplicate repository names")
    return cleaned


class Config(_Model):
    """Everything read from ``mergeworthy.toml``. Contains no secrets."""

    dry_run: bool = True
    languages: list[str] = Field(default_factory=lambda: ["java", "python"], min_length=1)
    labels: list[str] = Field(default_factory=lambda: ["good first issue", "help wanted"], min_length=1)
    allowlist: list[str] = Field(default_factory=list)
    deny_list: list[str] = Field(default_factory=list)
    ledger_path: Path = Path("mergeworthy.sqlite3")
    github_login: str | None = None
    limits: Limits = Field(default_factory=Limits)
    eligibility: Eligibility = Field(default_factory=Eligibility)
    budget: Budget = Field(default_factory=Budget)
    provider: Provider = Field(default_factory=Provider)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    prices: ProviderPrices = Field(default_factory=ProviderPrices)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)

    @field_validator("languages")
    @classmethod
    def _languages(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip().lower() for v in values]
        unknown = sorted(set(cleaned) - SUPPORTED_LANGUAGES)
        if unknown:
            supported = ", ".join(sorted(SUPPORTED_LANGUAGES))
            raise ValueError(f"unsupported languages {unknown}; supported: {supported}")
        return list(dict.fromkeys(cleaned))

    @field_validator("github_login")
    @classmethod
    def _login(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", cleaned):
            raise ValueError(f"not a GitHub login: {value!r}")
        return cleaned

    @field_validator("labels")
    @classmethod
    def _labels(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values]
        if any(not v for v in cleaned):
            raise ValueError("labels must not be empty")
        return cleaned

    @field_validator("allowlist", "deny_list")
    @classmethod
    def _repositories(cls, values: list[str]) -> list[str]:
        return _check_repo_names(values)

    def is_allowed(self, full_name: str) -> bool:
        """True only if the repository is on the allowlist and not on the deny list."""
        name = full_name.lower()
        return name in self.allowlist and name not in self.deny_list

    @property
    def active_prices(self) -> Prices:
        """The price list of the configured provider."""
        if self.provider.name == "bedrock":
            return self.prices.bedrock
        if self.provider.name == "openai":
            return self.prices.openai
        return self.prices.anthropic

    def is_denied(self, full_name: str) -> bool:
        return full_name.lower() in self.deny_list


class Secrets(_Model):
    """Credentials, read from the environment only."""

    github_token: SecretStr | None = None
    model_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None


def _walk(data: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, str, Any]]:
    for key, value in data.items():
        path = f"{prefix}{key}"
        yield path, key, value
        if isinstance(value, Mapping):
            yield from _walk(value, f"{path}.")


def _iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def find_secrets(data: Mapping[str, Any]) -> list[str]:
    """Return the dotted paths of entries that look like secrets. Values are never included."""

    def looks_secret(key: str, value: Any) -> bool:
        if _SECRET_KEY_PATTERN.search(key):
            return True
        return any(_SECRET_VALUE_PATTERN.match(s.strip()) for s in _iter_strings(value))

    return [path for path, key, value in _walk(data) if looks_secret(key, value)]


def parse_config(data: Mapping[str, Any]) -> Config:
    secrets = find_secrets(data)
    if secrets:
        raise ConfigError(
            "secrets must not be stored in the configuration file; set them as environment variables "
            f"({GITHUB_TOKEN_ENV}, {MODEL_API_KEY_ENV}). Offending entries: {', '.join(secrets)}"
        )
    try:
        return Config.model_validate(dict(data))
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc


def load_config(path: Path | None = None) -> Config:
    """Load the configuration file. Without a file, safe defaults apply (dry-run, empty allowlist).

    An explicitly given path must exist.
    """
    explicit = path is not None
    target = path if path is not None else DEFAULT_CONFIG_PATH
    if not target.exists():
        if explicit:
            raise ConfigError(f"configuration file not found: {target}")
        return Config()
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{target} is not valid TOML: {exc}") from exc
    return parse_config(data)


def load_secrets(environ: Mapping[str, str] | None = None) -> Secrets:
    env = os.environ if environ is None else environ

    def get(name: str) -> SecretStr | None:
        value = env.get(name, "").strip()
        return SecretStr(value) if value else None

    return Secrets(
        github_token=get(GITHUB_TOKEN_ENV),
        model_api_key=get(MODEL_API_KEY_ENV),
        openai_api_key=get("NARAROUTER_API_KEY"),
    )
