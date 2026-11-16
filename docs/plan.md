# Build plan

Work through the tasks **in order**, one commit per task. Tick the checkboxes in the same commit that
completes the task. A task is done only when its acceptance criteria are met and `ruff check .`,
`ruff format --check .`, `mypy src` and `pytest` all pass.

Design references: [architecture.md](architecture.md), [policy.md](policy.md), [evaluation.md](evaluation.md).

**Safety rule for the whole of stage 1:** nothing writes to GitHub except to repositories listed in
`allowlist` in `mergeworthy.toml`, and the default allowlist is empty. Until month 2, the only allowed
repositories are the maintainer's own (`aakashsiwach/codeatlas-rag`, `aakashsiwach/taintmap`).

---

## Month 1: the core loop on our own repositories

### T0. Project skeleton
- [x] `pyproject.toml` (hatchling, Python ≥ 3.11, `src/mergeworthy`), console script `mergeworthy`.
- [x] Dependencies: typer, rich, pydantic>=2, httpx. Dev: pytest, ruff, mypy, respx (HTTP mocking).
- [x] `mergeworthy version`.
- [x] CI on GitHub Actions: ruff, mypy, pytest on Python 3.11 and 3.12, Ubuntu and Windows. Actions pinned to
      full commit SHAs, `permissions: contents: read`, `persist-credentials: false`.
- [x] `CONTRIBUTING.md` with setup for PowerShell and bash, and `git config core.hooksPath .githooks`.

**Acceptance:** fresh clone → `pip install -e ".[dev]"` → `mergeworthy version` works; CI green.

### T1. Configuration and ledger
- [x] `config.py`: load `mergeworthy.toml` (limits from policy.md section 3, allowlist, deny list, languages,
      budget cap, dry-run flag defaulting to true). Secrets only from environment variables
      (`GITHUB_TOKEN`, model key), never from the file.
- [x] `ledger.py`: SQLite schema from architecture.md, migrations by version number, and an attempt state
      machine that rejects transitions not in the diagram.
- [x] `mergeworthy.example.toml` documented field by field.

**Acceptance:** tests for config validation, every allowed transition, a rejected transition, and that secrets
in the TOML file cause an error.

### T2. GitHub client (read-only)
- [x] `github.py`: search issues by label and language, get repository metadata, read a file, list recent
      commits and merged pull requests, list issue comments and timeline links to pull requests.
- [x] Rate-limit handling (respect `x-ratelimit-remaining` and `retry-after`), pagination, timeouts.
- [x] All tests use recorded or hand-written responses (respx). No network in tests.

**Acceptance:** tests for each call, pagination and rate-limit backoff.

### T3. Discovery and eligibility
- [x] `discover.py`: candidate issues for the configured languages and labels; store in the ledger.
- [x] `eligibility.py`: every check in policy.md sections 1 and 2, each returning pass/fail with a reason.
- [x] AI policy check: find the contributing guide, code of conduct, PR template and `AI_POLICY*`; a rule list
      of phrases that forbid or discourage AI contributions; unclear wording → skip and flag for a human.
      Keep the phrase list in a data file with comments, so it is easy to review and extend.
- [x] `mergeworthy discover` and `mergeworthy check`.

**Acceptance:** fixture repositories covering each pass and fail reason, including an AI-forbidding policy,
an unclear policy, an assigned issue and an issue with a linked open pull request.

### T4. Sandbox runner
- [ ] `sandbox.py`: Docker container per attempt; clone at a commit into a container volume; detect the build
      tool (architecture.md table); install dependencies; run tests with a timeout; return exit code and
      trimmed output.
- [ ] No environment variables from the host are passed into the container. Tests run with
      `--network none` where possible.
- [ ] Integration tests that need Docker are marked and skipped when Docker is unavailable; unit tests cover
      command building and detection.

**Acceptance:** runs the test suites of `codeatlas-rag` (Python) and a small Maven fixture project locally.

### T5. Coding agent adapter
- [ ] `agent/`: the `CodingAgent` protocol and a Claude Agent SDK implementation. Check the SDK's current
      documentation for exact option and tool names; do not guess them.
- [ ] Built-in shell tool disabled; file read and edit tools limited to the working copy; one custom tool
      `run_in_sandbox`.
- [ ] Issue text passed as clearly marked untrusted data. Spend cap and time limit enforced.
- [ ] A fake agent for tests that applies a predefined patch.
- [ ] Call `recheck.ensure_still_available` immediately before the agent starts.

**Acceptance:** with the fake agent, an end-to-end test goes from issue to diff; the real adapter is tested
with mocks for its configuration (no shell tool, custom tool registered, budget passed).

### T6. Verification and self-review
- [ ] `verify.py`: the five rules in architecture.md.
- [ ] `review.py`: the rubric as a versioned prompt file; verdict and reasons stored in the ledger.

**Acceptance:** tests for each verification rule and for review verdict parsing, including malformed output.

### T7. Human approval
- [ ] `mergeworthy review`: lists waiting attempts; shows issue, summary, diff (paged), test results,
      verification and review results, policy checks and spend; approve or reject with a required reason.

**Acceptance:** tests drive the CLI with simulated input; rejection without a reason is refused.

### T8. Submission (dry-run by default)
- [ ] `submit.py`: fork if needed, branch, commit using the local git identity, push, open a pull request with
      a description template (issue link, change summary, how it was tested, AI disclosure from policy.md
      section 4).
- [ ] Refuses repositories not on the allowlist. Refuses attempts not in `approved`. Dry-run prints exactly
      what would be sent.
- [ ] Call `recheck.ensure_still_available` immediately before forking or pushing.

**Acceptance:** tests with mocked GitHub for the allowlist refusal, the state refusal, the description
template and dry-run output.

### T9. Follow-up
- [ ] `followup.py`: poll submitted pull requests for CI status, comments, merge and close; update the
      ledger; draft replies or fix attempts that go through approval again.
- [ ] Closing with "AI contributions not welcome" or similar adds the repository to the deny list.

**Acceptance:** tests for each pull request outcome and for the deny-list rule.

### T10. Scoreboard
- [ ] `mergeworthy scoreboard`: static HTML and Markdown from the ledger with every number in
      evaluation.md section 2.

**Acceptance:** snapshot test from a fixture ledger.

### T11. First real run *(needs the maintainer)*
- [ ] Turn off dry-run for the allowlist only, and run one attempt on an open issue in
      `aakashsiwach/taintmap` or `aakashsiwach/codeatlas-rag`. Stop and hand over to the maintainer for
      approval and submission.

---

## Month 2 (outline)
- T12. Replay benchmark harness (evaluation.md section 1) and the first 20 cases from our own repositories.
- T13. External allowlist: 5–10 hand-picked active Java/Python repositories that welcome contributions.
- T14. Claim-comment flow for projects that require claiming an issue first.
- T15. Publish the scoreboard with GitHub Pages.

## Months 3–6 (outline)
- Security track with taintmap: findings, verification, private reporting drafts for approval.
- Replay test split, run once per release.
- Packaging for teams: run against a private repository and its backlog.
