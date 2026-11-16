# Evaluation

mergeworthy is judged by two numbers: how often its work would be merged, and how much it costs. Both are
measured before anything is sent to a real project, and then tracked in public.

## 1. Replay benchmark (offline)

A replay case is a past issue that was fixed by a merged pull request:

1. Check out the repository at the commit just before the fix was merged.
2. Give mergeworthy the original issue text.
3. Let it produce a fix in the sandbox, with the normal limits and budget.
4. Score it with the **tests from the real fix** added back in (hidden from the agent during the attempt).

```yaml
# replay/cases/<case-id>.yaml
id: example-repo-123
repository: owner/name
issue: 123
base_commit: <sha before the fix>
fix_commit: <sha of the merged fix>
hidden_tests:            # test files or test ids taken from the real fix
  - src/test/java/.../ExampleTest.java
language: java
```

### Metrics
| Metric | Meaning |
|---|---|
| Solve rate | attempts whose change makes the hidden tests pass, without breaking the rest of the suite |
| Self-review precision | of the attempts self-review accepted, the share that actually solve the case |
| Abstain rate | attempts the system rejected itself; abstaining is better than a bad pull request |
| Cost | model spend and wall-clock time per attempt |

Self-review precision matters most: it predicts what maintainers would see.

### Splits
Cases are split into `dev` and `test`. Prompts, rubrics and limits are tuned on `dev` only. `test` is run once
per release with `--final`, and those numbers are published. Test cases are chosen and written independently
of the tuning work. Never change a test case to improve a number.

Stage 1 starts with cases from the maintainer's own repositories, then adds permissively licensed public
repositories.

## 2. Live scoreboard (online)

Every real attempt is recorded in the ledger, and `mergeworthy scoreboard` publishes:

| Number | Meaning |
|---|---|
| Issues considered and skipped | with the reasons (policy, activity, tests, scope) |
| Attempts | started, failed, rejected by self-review, rejected by the human |
| Pull requests | submitted, merged, closed without merge, still open |
| Merge rate | merged ÷ (merged + closed without merge) |
| Median time to merge | from submission |
| Maintainer feedback | the reasons given when pull requests are closed |
| Cost | total and per merged pull request |

Failures are published with the same prominence as successes.
