"""Model provider settings for the processes that call models: the Claude API or Amazon Bedrock.

Only the coding agent process (Claude Agent SDK, which runs Claude Code) and the self-review client receive
provider credentials. They never reach a sandbox container (``sandbox.py`` passes only ``HOME``), the
ledger or logs: they are read from the environment when needed and never stored or printed.

Names follow the Claude Code documentation for Amazon Bedrock: ``CLAUDE_CODE_USE_BEDROCK=1``, ``AWS_REGION``,
and the AWS credential chain (``AWS_PROFILE``, ``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``,
``AWS_SESSION_TOKEN``) or an Amazon Bedrock API key in ``AWS_BEARER_TOKEN_BEDROCK``.
"""

from __future__ import annotations

from collections.abc import Mapping

from mergeworthy.config import Config

AWS_CREDENTIAL_VARS = (
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
)
# Variables that would switch Claude Code to another provider.
OTHER_PROVIDER_SWITCHES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_VERTEX")
SECRET_VARS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "NARAROUTER_API_KEY",
)


def agent_environment(config: Config, environ: Mapping[str, str]) -> dict[str, str]:
    """Environment overrides for the coding agent process (merged on top of its inherited environment)."""
    if config.provider.name == "bedrock":
        env = {"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_API_KEY": ""}
        if config.provider.aws_region:
            env["AWS_REGION"] = config.provider.aws_region
        for name in AWS_CREDENTIAL_VARS:
            value = environ.get(name, "").strip()
            if value:
                env[name] = value
        return env
    # Claude API: make sure nothing inherited switches the agent to another provider or hands it AWS keys.
    return dict.fromkeys((*OTHER_PROVIDER_SWITCHES, *AWS_CREDENTIAL_VARS, "NARAROUTER_API_KEY"), "")


def redact(text: str, environ: Mapping[str, str]) -> str:
    """Remove any provider secret value from text before it is stored or shown."""
    for name in (*SECRET_VARS, "ANTHROPIC_API_KEY"):
        value = environ.get(name, "")
        if len(value) >= 8:
            text = text.replace(value, "***")
    return text
