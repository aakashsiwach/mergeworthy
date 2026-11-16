# mergeworthy: vision

*Author: Aakash Siwach ([@aakashsiwach](https://github.com/aakashsiwach))*

## The problem

Open source runs the world, and most of it is maintained by a small number of overworked people. Their
issue trackers are full of small, well-defined problems labelled *good first issue* or *help wanted*, waiting
for someone to pick them up.

AI coding agents can now solve many of those problems. But the way AI has met open source so far has mostly
hurt maintainers: floods of untested pull requests, invented vulnerability reports and generated comments that
cost more time to review than they save. Some projects have closed their bug bounties; many now refuse
AI-assisted contributions altogether.

The capability is real. The trust is missing.

## The idea

mergeworthy is an autonomous engineer built around trust:

- It does the work end to end: finding suitable issues, understanding the code, writing and testing a fix,
  and reviewing it critically.
- A human approves everything that leaves, and stands behind it.
- It follows each project's rules, discloses AI assistance, and keeps its volume deliberately low.
- It publishes its results, including failures, so anyone can judge its quality.

The measure of success is not the number of pull requests opened. It is the share that maintainers merge, and
the time they save.

## Why now

1. **Agents can do real engineering work.** Modern coding agents can read a codebase, change it, and run its
   tests. The engineering around them, choosing work, isolating execution, verifying results and earning
   trust, is now the hard part.
2. **Maintainers need help and protection at the same time.** A contributor that is useful *and* respectful
   is rare. That is the gap.
3. **Organisations will want the same thing internally.** Every company has a backlog of small, well-defined
   tasks. An engine that clears them safely, with approval and evidence, is valuable far beyond open source.

## Where it goes

### Stage 1: a trustworthy contributor (months 1–6)
Java and Python repositories on GitHub. A steady, small number of high-quality pull requests per week, each
approved by a human. A public scoreboard. A security track that uses [taintmap](https://github.com/aakashsiwach/taintmap)
to find real risks and report them privately.

### Stage 2: for teams
The same engine pointed at a company's own repositories and backlog: issues in, reviewed pull requests out,
with policies, approvals and an audit trail.

### Stage 3: the engineering department of an autonomous organisation
Agents that take work from a backlog, deliver it with evidence, and earn wider autonomy as their record
proves reliable, with humans setting goals and remaining accountable.

## What mergeworthy will not do

- Submit anything without human approval.
- Contribute to projects whose policies do not accept AI-assisted work.
- Open many pull requests to the same project, or chase volume.
- Publish vulnerability details before maintainers have had the chance to fix them.
- Hide that AI was involved.
