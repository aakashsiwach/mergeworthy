"""The coding agent adapter (docs/architecture.md, "The coding agent and the sandbox").

An agent edits files in the attempt's working copy and runs commands only through ``run_in_sandbox``. It never
gets a shell, the GitHub token, or access outside the working copy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from mergeworthy.sandbox import RunResult


@dataclass(frozen=True)
class Task:
    """What the agent is asked to do. ``issue_title`` and ``issue_body`` are untrusted input."""

    repository: str
    issue_number: int
    issue_url: str
    issue_title: str
    issue_body: str
    test_command: str
    conventions: str = ""
    max_diff_lines: int = 200


@dataclass(frozen=True)
class Budget:
    max_spend_usd: float
    max_minutes: float
    max_turns: int


@dataclass(frozen=True)
class CommandRecord:
    command: str
    exit_code: int
    timed_out: bool


class AttemptStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    TIMED_OUT = "timed_out"


@dataclass
class AttemptResult:
    status: AttemptStatus
    diff: str = ""
    explanation: str = ""
    commands: list[CommandRecord] = field(default_factory=list)
    spend_usd: float = 0.0
    error: str | None = None


class AgentSandbox(Protocol):
    """What an agent may do in the sandbox: read the working copy path, run commands, and get the diff."""

    @property
    def workdir(self) -> Path: ...

    def run(self, command: str) -> RunResult: ...

    def diff(self) -> str: ...


class CodingAgent(Protocol):
    def attempt(self, task: Task, workdir: Path, sandbox: AgentSandbox, budget: Budget) -> AttemptResult: ...
