# Architecture

mergeworthy is one Python application with a SQLite database, Docker for isolation, and the GitHub REST API.
It orchestrates an existing coding agent instead of implementing one.

## Pipeline and states

Every issue mergeworthy considers becomes an **attempt** that moves through these states. Each transition is
recorded in the ledger with a timestamp and a reason.

```text
discovered ─▶ eligible ─▶ (claim_drafted ─▶ claim_approved ─▶ claimed) ─▶ working ─▶ self_reviewed
     │            │                                                          │            │
     ▼            ▼                                                          ▼            ▼
  skipped      skipped                                                    failed      rejected
                                                                                          
self_reviewed ─▶ awaiting_approval ─▶ approved ─▶ submitted ─▶ in_review ─▶ merged | closed
                        │
                        ▼
                  human_rejected
```

The claim steps only happen when the project's contributing guide asks contributors to claim an issue first.

## Components

| Module | Responsibility |
|---|---|
| `config.py` | Settings from `mergeworthy.toml` and environment variables; limits from docs/policy.md |
| `ledger.py` | SQLite store: repositories, issues, attempts, state transitions, costs, outcomes |
| `github.py` | GitHub REST client: search issues, read files, fork, push, open pull requests, read comments and checks. Rate-limit aware. Dry-run mode. |
| `discover.py` | Finds candidate issues by label, language and activity |
| `eligibility.py` | Applies docs/policy.md sections 1 and 2, including the AI policy check |
| `sandbox.py` | Docker runner: clone at a commit, detect the build tool, install dependencies, run tests with timeouts |
| `agent/` | The coding agent adapter (see below) |
| `verify.py` | Objective checks: baseline tests, tests after the change, new tests fail before the fix, diff limits, scope |
| `review.py` | Self-review pass with a written rubric; can reject |
| `approve.py` | Human approval in the terminal: summary, diff, test results, checks; approve or reject with reason |
| `submit.py` | Fork, branch, commit as the configured git identity, push, open the pull request with disclosure |
| `followup.py` | Polls pull requests: CI status, maintainer comments, merge or close; drafts replies for approval |
| `scoreboard.py` | Generates the public results page from the ledger |
| `cli.py` | Typer CLI |

## The coding agent and the sandbox

The issue text and the repository's contents are **untrusted input**: anyone can write an issue, and a
repository can contain instructions aimed at AI agents. So the agent must not be able to reach anything
valuable, even if it is manipulated.

```text
host (mergeworthy)                 agent process                       sandbox container
- GitHub token                     - model API key                     - repository working copy
- ledger                           - edits files in the working copy   - build and test tools
- approval UI                      - NO shell tool                     - NO tokens, NO keys
                                   - ONE custom tool: run_in_sandbox ─▶ runs the command here
```

- The agent runs with the Claude Agent SDK (or Claude Code in headless mode) with its built-in shell tool
  **disabled**. It can read and edit files in the working copy and call one custom tool, `run_in_sandbox`,
  which executes a command in the container and returns its output.
- The sandbox container never receives the GitHub token or the model API key.
- The agent process has no GitHub token. Only `submit.py`, after human approval, uses it.
- Network: dependency installation may use the network; test runs use `--network none` where the project
  allows it.
- Every attempt has a time limit and a model spend cap.

The adapter has a small interface so other agents can be plugged in:

```python
class CodingAgent(Protocol):
    def attempt(self, task: Task, workdir: Path, sandbox: Sandbox, budget: Budget) -> AttemptResult: ...
```

`Task` holds the issue title and body (marked as untrusted), the repository's conventions summary and the
policy limits. `AttemptResult` holds the diff, the agent's explanation, the commands it ran and the spend.

## Build tool detection (stage 1)

| Ecosystem | Detected by | Test command |
|---|---|---|
| Maven | `pom.xml` | `mvn -B -q test` (or the wrapper `./mvnw`) |
| Gradle | `build.gradle*` | `./gradlew test` |
| Python (pytest) | `pyproject.toml`, `setup.cfg`, `tox.ini`, `pytest.ini` | `pytest` |
| Python (tox/nox) | `tox.ini`, `noxfile.py` | the project's documented test environment |

If the contributing guide documents a different command, that command wins.

## Verification rules (`verify.py`)

An attempt passes verification only if:
1. The test suite passed on the unmodified repository (baseline). If the baseline fails, skip the issue.
2. The test suite passes with the change.
3. For bug fixes, at least one new or changed test fails without the change and passes with it.
4. The diff is within the size limit and touches only files relevant to the issue.
5. The project's documented linters or formatters pass.

## Self-review (`review.py`)

A separate agent call, with no access to the first agent's reasoning, receives the issue, the diff and the
verification results, and answers a fixed rubric: does the change solve the issue, is it the smallest
reasonable change, does it match the project's style, are the tests meaningful, is anything outside scope,
would a maintainer be likely to merge it. Any "no" rejects the attempt with the reason.

## Data model (SQLite)

| Table | Key fields |
|---|---|
| `repositories` | full name, language, license, activity checks, policy check result, deny-listed, cooldown until |
| `issues` | repository, number, title, labels, url, eligibility result and reasons |
| `attempts` | issue, state, branch, diff stats, test results, review verdict, spend, timestamps |
| `transitions` | attempt, from, to, reason, timestamp |
| `pull_requests` | attempt, url, state, CI status, merged at, closed at, maintainer outcome notes |
| `limits_log` | changes to limits, with reason |

## CLI

```text
mergeworthy discover [--language java|python] [--limit N]    find and record candidate issues
mergeworthy check REPO_OR_ISSUE                              run eligibility checks and print reasons
mergeworthy work ISSUE_URL [--dry-run]                       run one attempt end to end up to approval
mergeworthy review                                           approve or reject waiting attempts
mergeworthy submit ATTEMPT_ID                                submit an approved attempt
mergeworthy followup                                         check open pull requests, draft replies
mergeworthy scoreboard [--output DIR]                        build the results page
mergeworthy replay [--split dev|test] [--final]              run the replay benchmark
```

`--dry-run` is the default for anything that would write to GitHub until it is explicitly configured off.

## Technology

Python 3.11+, Typer, Pydantic v2, httpx, SQLite (standard library), Docker (via the `docker` CLI), the Claude
Agent SDK. Quality: pytest, ruff, mypy. No queues, no external database, no hosted services in stage 1.
