"""OpenAI-compatible coding agent tests; all HTTP is mocked."""
from pathlib import Path
from typing import Any

from mergeworthy.agent import AttemptStatus, Budget, Task
from mergeworthy.agent.openai import OpenAICodingAgent
from mergeworthy.config import ModelPrice, Prices
from mergeworthy.sandbox import RunResult


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


class FakeSandbox:
    def __init__(self, root: Path) -> None:
        self.workdir = root
        self.commands: list[str] = []

    def run(self, command: str) -> RunResult:
        self.commands.append(command)
        return RunResult(0, "ok", False, 0.1)

    def diff(self) -> str:
        return "diff --git a/x.py b/x.py\n+x = 1\n"


def task() -> Task:
    return Task("owner/repo", 1, "https://github.com/owner/repo/issues/1",
                "fix", "body", "pytest -q")


def prices() -> Prices:
    return Prices(models={"router/model": ModelPrice(input_per_mtok=1, output_per_mtok=2)})


def response(message: dict[str, Any], prompt: int = 1000, completion: int = 100) -> FakeResponse:
    return FakeResponse({"model": "router/model", "usage": {"prompt_tokens": prompt,
                         "completion_tokens": completion}, "choices": [{"message": message}]})


def test_requires_explicit_price_before_making_paid_request(tmp_path: Path) -> None:
    client = FakeClient([])
    agent = OpenAICodingAgent(model="router/model", api_key="key", base_url="https://router.test/v1",
                              client=client)
    result = agent.attempt(task(), tmp_path, FakeSandbox(tmp_path), Budget(1, 1, 2))
    assert result.status is AttemptStatus.FAILED
    assert "no configured price" in (result.error or "")
    assert client.calls == []


def test_tool_call_executes_only_through_sandbox_and_counts_spend(tmp_path: Path) -> None:
    tool_call = {"id": "call-1", "type": "function", "function": {
        "name": "run_in_sandbox", "arguments": '{"command":"pytest -q"}'}}
    client = FakeClient([
        response({"role": "assistant", "content": None, "tool_calls": [tool_call]}),
        response({"role": "assistant", "content": "Fixed and tested."}),
    ])
    sandbox = FakeSandbox(tmp_path)
    agent = OpenAICodingAgent(model="router/model", api_key="secret", base_url="https://router.test/v1",
                              client=client, prices=prices())
    result = agent.attempt(task(), tmp_path, sandbox, Budget(1, 1, 4))
    assert result.status is AttemptStatus.COMPLETED
    assert result.diff.startswith("diff --git")
    assert sandbox.commands == ["pytest -q"]
    assert result.spend_usd == 0.0024
    assert client.calls[0]["url"] == "https://router.test/v1/chat/completions"
    assert client.calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert client.closed is False


def test_spend_cap_stops_before_executing_returned_tool_call(tmp_path: Path) -> None:
    tool_call = {"id": "call-1", "type": "function", "function": {
        "name": "run_in_sandbox", "arguments": '{"command":"dangerous"}'}}
    client = FakeClient([response({"role": "assistant", "tool_calls": [tool_call]}, 2_000_000, 0)])
    sandbox = FakeSandbox(tmp_path)
    agent = OpenAICodingAgent(model="router/model", api_key="key", base_url="https://router.test/v1",
                              client=client, prices=prices())
    result = agent.attempt(task(), tmp_path, sandbox, Budget(1, 1, 4))
    assert result.status is AttemptStatus.BUDGET_EXCEEDED
    assert sandbox.commands == []


def test_file_tools_cannot_escape_repository_or_access_git(tmp_path: Path) -> None:
    agent = OpenAICodingAgent(model="router/model", api_key="key", base_url="https://router.test/v1",
                              prices=prices())
    sandbox = FakeSandbox(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        agent._execute("write_file", {"path": "../escape", "content": "x"}, tmp_path, sandbox, [])
    with pytest.raises(ValueError):
        agent._execute("read_file", {"path": ".git/config"}, tmp_path, sandbox, [])
