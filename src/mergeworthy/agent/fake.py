"""A fake agent for tests: applies a predefined patch instead of calling a model."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mergeworthy.agent import AgentSandbox, AttemptResult, AttemptStatus, Budget, CommandRecord, Task


@dataclass
class FakeAgent:
    """Writes ``files`` (path relative to the working copy -> new content; ``None`` deletes the file),
    then runs ``commands`` through the sandbox, like the real agent would."""

    files: dict[str, str | None]
    commands: list[str] = field(default_factory=list)
    explanation: str = "Applied the predefined patch."
    spend_usd: float = 0.0
    seen: list[Task] = field(default_factory=list)

    def attempt(self, task: Task, workdir: Path, sandbox: AgentSandbox, budget: Budget) -> AttemptResult:
        self.seen.append(task)
        root = workdir.resolve()
        for relative, content in self.files.items():
            path = (root / relative).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"fake patch escapes the working copy: {relative}")
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
        records = []
        for command in self.commands:
            result = sandbox.run(command)
            records.append(CommandRecord(command, result.exit_code, result.timed_out))
        return AttemptResult(
            AttemptStatus.COMPLETED,
            diff=sandbox.diff(),
            explanation=self.explanation,
            commands=records,
            spend_usd=self.spend_usd,
        )
