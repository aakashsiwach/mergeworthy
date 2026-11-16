# mergeworthy

**An autonomous open-source engineer that only sends pull requests worth merging.**

> **Status: design stage.** There is no usable release yet. This repository holds the vision, the
> contribution policy, the architecture and the build plan.

---

mergeworthy finds open issues that maintainers have asked for help with, understands the codebase, writes
a fix with tests inside an isolated sandbox, challenges its own work, and hands a finished pull request to a
human for a short final review. Only what the human approves is submitted.

It is built for one idea: **AI should make maintainers' lives easier, not harder.** Maintainers are already
flooded with low-quality AI-generated pull requests and reports. mergeworthy is designed to be the opposite:
few, well-tested, policy-respecting contributions, with an honest public record of every result.

## How it works

```text
find issue ─▶ check rules ─▶ understand code ─▶ fix in sandbox ─▶ self-review ─▶ human approves ─▶ submit ─▶ follow up ─▶ learn
```

1. **Find work.** Open issues labelled *good first issue* or *help wanted* in active repositories.
2. **Check the rules.** Read the project's contributing guide and AI policy. Projects that do not accept
   AI-assisted contributions are skipped. Projects that ask contributors to claim an issue first get a
   claim comment, drafted for approval, before any work starts.
3. **Understand the code** with a code graph and impact analysis.
4. **Fix it** in a Docker sandbox: change, tests, and the project's own test suite. No credentials ever enter
   the sandbox.
5. **Self-review.** A second pass tries to reject the fix: failing or missing tests, oversized diffs, style
   that does not match the project, changes outside the issue's scope.
6. **Human approval.** A person reads the summary, the diff and the test results, and approves or rejects.
7. **Submit and follow up.** Fork, branch, pull request. CI results and maintainer comments come back as
   drafted replies or fixes, again for approval.
8. **Learn.** Every outcome, merged or closed, is recorded with the reason.

## Principles

- **Quality over volume.** Hard limits on pull requests per week and per repository.
- **Respect project rules.** Contributing guides and AI policies are followed, not worked around.
- **Honest disclosure.** Pull requests say that AI assistance was used and that a human reviewed and tested
  the change.
- **A human approves everything that leaves.** Pull requests, comments and reports are never sent without it.
- **Public scoreboard.** Attempts, approvals, merges, rejections with reasons, and cost are published.
- **Safe by construction.** Repository code only ever runs inside a sandbox without tokens.

The full rules are in [docs/policy.md](docs/policy.md).

## Documentation

| Document | What it covers |
|---|---|
| [VISION.md](VISION.md) | Why mergeworthy exists and where it is going |
| [docs/policy.md](docs/policy.md) | Contribution rules: which repositories, disclosure, limits, security reports |
| [docs/architecture.md](docs/architecture.md) | Components, data model, sandbox, agents, CLI |
| [docs/evaluation.md](docs/evaluation.md) | How quality is measured: replay benchmark and live scoreboard |
| [docs/plan.md](docs/plan.md) | The task-by-task build plan |

## Related projects

- [CodeAtlas](https://github.com/aakashsiwach/codeatlas-rag): code understanding for Java/Spring codebases
- [taintmap](https://github.com/aakashsiwach/taintmap): maps how untrusted input reaches AI agents and automations

## License

[MIT](LICENSE) © Aakash Siwach
