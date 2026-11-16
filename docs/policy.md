# Contribution policy

These rules are enforced in code wherever possible. When code and this document disagree, this document wins
and the code is a bug.

## 1. Which repositories

A repository is eligible only if **all** of these hold:

| Check | Default |
|---|---|
| Public, with an OSI-approved license | required |
| Language in scope | Java or Python (stage 1) |
| Active: a commit to the default branch within | 30 days |
| Maintainers respond: a pull request from an outside contributor merged within | 90 days |
| A contributing guide exists (`CONTRIBUTING*`, `.github/CONTRIBUTING*` or `docs/CONTRIBUTING*`) | required |
| No policy refuses AI-assisted contributions | required |
| Not on the local deny list | required |
| Tests can be run in the sandbox within | 15 minutes |

**AI policy check.** The contributing guide, code of conduct, pull request template and any `AI_POLICY*`
file are read. If any of them forbids or discourages AI-generated or AI-assisted contributions, the repository
is skipped. If the wording is unclear, the repository is skipped and listed for a human to decide. Never
argue with a project's policy and never work around it.

## 2. Which issues

An issue is eligible only if:
- it has a label meaning the maintainers want outside help (`good first issue`, `help wanted`, or the
  project's documented equivalent),
- it is open, not assigned to anyone, and has no open pull request linked to it,
- it has no comment from someone saying they are working on it within the last 30 days,
- it is specific enough to verify: a bug with reproduction steps, or a clearly described change.

If the contributing guide says to ask before working on an issue, mergeworthy **drafts a claim comment** for
human approval and waits until a maintainer agrees or assigns the issue.

## 3. Limits

| Limit | Default |
|---|---|
| Pull requests submitted per week, in total | 3 |
| Open pull requests per repository at a time | 1 |
| Attempts per issue | 1 |
| Cooldown on a repository after a pull request is closed without merging | 60 days |
| Maximum diff size (lines changed, excluding tests) | 200 |
| Model spend per attempt | configurable hard cap |

Raising a limit is a deliberate configuration change, recorded in the ledger.

## 4. What every pull request must contain

- A fix limited to the issue's scope, following the project's style and conventions.
- Tests that fail before the fix and pass after it, when the project has a test suite.
- The project's own checks passing locally in the sandbox (tests, linters, formatters it documents).
- A description that links the issue, explains the change and how it was tested.
- **AI disclosure:** a short statement that the change was prepared with AI assistance and reviewed and
  tested by the submitter. If the project's policy specifies wording, use that wording.

Commits are authored by the human who approves the pull request. That person must understand the change and
be able to answer maintainers' questions about it.

## 5. Human approval

Nothing leaves the machine without explicit approval: no pull request, no comment, no claim, no reply, no
security report. Approval shows the summary, the full diff, the test results, the self-review and the
policy checks. Rejections are recorded with a reason.

## 6. Follow-up

- Maintainer comments and CI failures produce drafted replies or fixes for approval.
- If a maintainer asks for the pull request to be closed, or says AI contributions are unwelcome, close it,
  thank them, add the repository to the deny list, and do not return.
- Never ping maintainers to hurry a review.

## 7. Security findings

- Security issues are **never** sent as public issues or pull requests first.
- A finding must be reproduced, or shown with concrete evidence, before it is reported. Unverified findings
  are dropped.
- Report through GitHub private vulnerability reporting or the process in the project's `SECURITY.md`.
- Public discussion only after a fix is released or the maintainers agree.

## 8. Sandbox and credentials

- Repository code is only built and run inside a container.
- The container never receives the GitHub token, model API keys or any other secret.
- The container has no access to the host filesystem beyond its own working copy.
- Network access during dependency installation is allowed; tests run with network disabled where the
  project allows it.
