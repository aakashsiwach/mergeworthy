# Architecture

mergeworthy is one Python application with a SQLite database, Docker for isolation, and the GitHub REST API.
It orchestrates an existing coding agent instead of implementing one.

## Pipeline and states

Every issue mergeworthy considers becomes an **attempt** that moves through these states. Each transition is
recorded in the ledger with a timestamp and a reason.

```text
discovered ─▶ needs_human ─▶ eligible | skipped
     │
discovered ─▶ eligible ─▶ (claim_drafted ─▶ claim_approved ─▶ claimed) ─▶ working ─▶ self_reviewed
     │            │              │                │               │            │            │
     ▼            ▼              ▼                ▼               ▼            ▼            ▼
  skipped      skipped     human_rejected      declined        declined      failed      rejected

self_reviewed ─▶ awaiting_approval ─▶ approved ─▶ submitted ─▶ in_review ─▶ merged | closed
                        │                            │
                        ▼                            ▼
                  human_rejected                   closed

any state that is not terminal ─▶ abandoned
```

`needs_human` is not terminal: it waits for `mergeworthy decide` and the next `mergeworthy check`.

Terminal states: `skipped`, `failed`, `rejected`, `human_rejected`, `declined`, `merged`, `closed`, `abandoned`.

- `human_rejected` after `claim_drafted`: the human did not approve the claim comment.
- `declined`: a maintainer turned down the claim, before or after it was posted.
- `submitted ─▶ closed`: the pull request was closed before review started.
- `abandoned`: the issue was closed, assigned to someone else, or a linked pull request appeared. Checked
  again right before work starts and right before submission.

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

### Claude Agent SDK adapter (`agent/claude.py`)

- Built-in tools: `Read`, `Edit`, `Write`, `Glob`, `Grep` only (`tools`); `Bash`, web, subagent, skill and
  other built-ins are removed (`disallowed_tools`). One in-process MCP tool, `run_in_sandbox`
  (`mcp__sandbox__run_in_sandbox`), runs a command in the sandbox without network.
- A `PreToolUse` hook (runs for every call) and `can_use_tool` deny any other tool and any path or glob
  outside the working copy.
- `setting_sources=[]`, `strict_mcp_config=True`, no skills or plugins: nothing in the untrusted working
  copy (`.claude/`, `.mcp.json`, `CLAUDE.md`) can configure the agent.
- `GITHUB_TOKEN`, `GH_TOKEN` and similar are blanked in the agent process environment.
- Spend: `max_budget_usd` from `[budget]`; turns: `max_turns` from `[agent]`; wall-clock: a timeout from
  `[budget]`. The issue is passed inside `<untrusted_issue_*>` markers, with the markers escaped in the text.

`work.run_attempt` drives one attempt: re-check, `working`, clone at the default branch's head, install,
baseline tests (must pass within `max_test_minutes`; this also measures the deferred eligibility check and
warms Maven's offline cache), agent, then the diff and spend are recorded. Any failure moves the attempt to
`failed` with the reason.

## Build tool detection (stage 1)

| Ecosystem | Detected by | Test command |
|---|---|---|
| Maven | `pom.xml` | `mvn -B -q test` (or the wrapper `./mvnw`) |
| Gradle | `build.gradle*` | `./gradlew test` |
| Python (pytest) | `pyproject.toml`, `setup.cfg`, `tox.ini`, `pytest.ini` | `pytest` |
| Python (tox/nox) | `tox.ini`, `noxfile.py` | the project's documented test environment |

If the contributing guide documents a different command, that command wins.

### Rule: the working copy is untrusted, including `.git`

Tests and the agent run in the working copy, so everything in it can be hostile, including `.git` (for
example a planted `core.fsmonitor`, `core.pager`, alias or hook that runs when git is used there).

- **The host never runs git, or any other program, in the working copy.** `Sandbox` starts only the
  `docker` CLI and refuses anything else. Host code only reads plain files there, refuses paths outside it,
  and deletes symbolic links after every container run.
- **Project code sees `.git` read-only.** Install, test and agent containers mount `.git` with `readonly`.
  Only mergeworthy's own fixed git commands (clone, baseline commit, diff) run with it writable, inside a
  container. The agent's file tools are denied any path under `.git`.
- **Only the diff leaves the working copy, as text.** It is checked by `patch.validate_patch`: relative
  paths only, nothing under `.git` or `.gitmodules`, regular files only (no symlinks or submodules), no binary
  patches.
- **Submission (T8) never pushes from the working copy.** It takes the approved diff text, makes a fresh
  clean clone on the host, checks that the diff touches exactly the files recorded at approval
  (`patch.require_valid`), applies it, and commits and pushes from that clean clone.

### Sandbox implementation (`sandbox.py`)

- Per attempt: a host directory with the working copy (bind-mounted at `/work/repo`; the agent's file
  tools need it on the host) and a named volume for dependencies (`/work/deps`: Python virtualenv,
  Maven repository, Gradle home). No other host path is mounted.
- Clone and diff run inside a container (`buildpack-deps:bookworm-scm`). The host never runs `git` or any
  repository code in the working copy. Symbolic links are checked out as plain files, and any link a
  container creates is deleted after every run.
- Containers get no host environment (only `HOME`), `--cap-drop ALL`, `no-new-privileges`, and CPU,
  memory and process limits. Install and clone use the network; tests and agent commands use
  `--network none`.
- Maven resolves some artifacts (such as surefire's test provider) only when tests run. If the offline
  run reports them missing, exactly those coordinates are fetched with a pinned
  `maven-dependency-plugin:get` from Maven Central only, and the tests run offline again, up to three times.
  The fetch container never sees the project: no repository mount, working directory `/tmp`, the image's
  `mvn` (not `./mvnw`), `MAVEN_SKIP_RC` with a fresh `HOME`, empty settings, and a fresh local
  repository whose artifacts are copied into the shared one without overwriting.
- Default images: `python:3.12` (the full image, because many test suites call `git`),
  `maven:3.9-eclipse-temurin-21`, `gradle:8-jdk21`; configurable under `[sandbox]`.

## Verification rules (`verify.py`)

An attempt passes verification only if:
1. The test suite passed on the unmodified repository (baseline). If the baseline fails, skip the issue.
2. The test suite passes with the change.
3. For bug fixes, at least one new or changed test fails without the change and passes with it.
4. The diff is within the size limit and touches only files relevant to the issue.
5. The project's documented linters or formatters pass.

Implementation notes (`verify.py`):
- Rule 3 applies to every change, not only bug fixes: docs/policy.md section 4 asks for tests that fail before
  the fix and pass after it whenever the project has a test suite. The non-test changes are reverted inside a
  container (`git show HEAD:path`, `.git` read-only), the suite must fail, the fix is written back on the host
  as plain files, and the diff must be identical afterwards.
- Rule 4 also rejects unsafe patches (`patch.validate_patch`), more than 15 files, and changes to CI
  configuration, lockfiles, build wrappers and extensions, licences and governance files.
- Rule 5 runs the linters the project configures (ruff, black, isort, flake8; Spotless, Checkstyle) on the
  changed files, offline. A configured tool that is not installed fails the rule.
- Diffs never include build output: clone writes `target/`, `build/`, `__pycache__/` and similar patterns
  to `.git/info/exclude`.

## Self-review (`review.py`)

A separate agent call, with no access to the first agent's reasoning, receives the issue, the diff and the
verification results, and answers a fixed rubric: does the change solve the issue, is it the smallest
reasonable change, does it match the project's style, are the tests meaningful, is anything outside scope,
would a maintainer be likely to merge it. Any "no" rejects the attempt with the reason.

The rubric is `src/mergeworthy/data/review_rubric.md`, versioned in its front matter; the version is stored with
every verdict. The call uses the Messages API with a JSON schema output, `[review] model` (default
`claude-opus-5-5`), and server-side refusal fallback. Malformed output, a refusal, a cut-off answer or an API
error is verdict `error` and rejects the attempt (fail closed). The reviewer never sees the agent's
explanation.

## Data model (SQLite)

| Table | Key fields |
|---|---|
| `repositories` | full name, language, license, activity checks, policy check result, deny-listed, cooldown until |
| `issues` | repository, number, title, labels, url, eligibility result and reasons |
| `attempts` | issue, state, branch, diff stats, test results, review verdict, spend, timestamps |
| `transitions` | attempt, from, to, reason, timestamp |
| `pull_requests` | attempt, url, state, CI status, merged at, closed at, maintainer outcome notes |
| `limits_log` | changes to limits, with reason |
| `human_decisions` | subject (repository or issue), check, content hash, question, allow or deny, reason |

## CLI

```text
mergeworthy discover [--language java|python] [--limit N]    find and record candidate issues
mergeworthy check REPO_OR_ISSUE                              run eligibility checks and print reasons
mergeworthy decide [SUBJECT CHECK --allow|--deny -r REASON]  list or answer questions for a human
mergeworthy work ISSUE_URL [--dry-run/--no-dry-run]          run one attempt end to end up to approval
mergeworthy review                                           approve or reject waiting attempts
mergeworthy submit ATTEMPT_ID                                submit an approved attempt
mergeworthy followup                                         check open pull requests, draft replies
mergeworthy scoreboard [--output DIR]                        build the results page
mergeworthy replay [--split dev|test] [--final]              run the replay benchmark
```

`--dry-run` is the default for anything that would write to GitHub until it is explicitly configured off.

`work` is a dry run by default: it runs the eligibility checks and stored decisions without writing to the
ledger, prints the steps it would take, and starts no sandbox and spends nothing. `--no-dry-run` records the
check and runs the attempt. It refuses repositories whose contributing guide asks to claim issues first until
the claim flow (T14) exists. Exit codes: 0 done, 1 not possible or failed, 3 a human must decide.

`discover` and `check` only read from GitHub. `discover` stores candidates and opens one attempt per new issue in
`discovered`. `check` records the results in the ledger and moves a `discovered` attempt to `eligible`, `needs_human` or
`skipped` (a check that fails outright wins over one that is unclear). `check` exits
with 0 (eligible), 1 (not eligible or error), 2 (configuration error) or 3 (a human must decide).

**Human decisions.** An unclear check (AI policy wording, an issue that may not be specific enough, an
uncertain linked pull request) becomes an open question in `human_decisions`, tied to a SHA-256 hash of the
content it was about: for the AI policy, the repository's policy files. `mergeworthy decide` answers it with
a required reason. The answer is reused by every later `check` while the hash is unchanged; when the content
changes, the question is asked again. A denial moves attempts waiting in `needs_human` to `skipped` and
attempts paused mid-way (by the re-check before work or submission) to `abandoned`. An approval leaves paused
attempts resumable: the next `mergeworthy work` re-checks, reuses the decision and continues.

**Linked pull requests.** An issue has a linked pull request if the issue timeline shows an open pull
request cross-referencing it, or if a search for open pull requests mentioning the issue number finds one
that uses a closing keyword (`fixes #N`) or the issue URL. A pull request that only mentions `#N` is
uncertain and goes to a human.

**Re-check before acting.** `recheck.ensure_still_available(attempt)` re-reads the issue and checks it is
still open, not assigned to someone else (the configured `github_login` does not count) and has no linked
pull request. On failure the attempt moves to `abandoned`; an uncertain pull request blocks until a human
decides. Starting work (T5) and submitting (T8) must call it immediately before acting.

The AI policy check matches the phrases in `src/mergeworthy/data/ai_policy_phrases.toml`: any forbidding phrase
skips the repository, an explicit permission passes, any other mention of AI is unclear and goes to a human.
The license check takes GitHub's `license.spdx_id` and looks it up in a vendored copy of the SPDX License
List (`src/mergeworthy/data/spdx_licenses.json`, with its source URL and version; refresh with
`scripts/update_spdx_licenses.py`). It fails closed: a missing, unknown or non-OSI-approved identifier fails. The "tests run within 15 minutes"
check is reported as `deferred` until the sandbox (T4) measures it.

## Technology

Python 3.11+, Typer, Pydantic v2, httpx, SQLite (standard library), Docker (via the `docker` CLI), the Claude
Agent SDK. Quality: pytest, ruff, mypy. No queues, no external database, no hosted services in stage 1.
