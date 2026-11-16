# Contributing to mergeworthy

Thanks for your interest. mergeworthy is in early development; the build plan is in
[docs/plan.md](docs/plan.md) and the rules it follows are in [docs/policy.md](docs/policy.md).

## Setup

You need Python 3.11 or newer and git. Docker is needed later for the sandbox (task T4).

### bash (Linux, macOS, Git Bash)

```bash
git clone https://github.com/aakashsiwach/mergeworthy.git
cd mergeworthy
python -m venv .venv
source .venv/bin/activate          # Git Bash on Windows: source .venv/Scripts/activate
pip install -e ".[dev]"
git config core.hooksPath .githooks
mergeworthy version
```

### PowerShell (Windows)

```powershell
git clone https://github.com/aakashsiwach/mergeworthy.git
cd mergeworthy
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
git config core.hooksPath .githooks
mergeworthy version
```

`git config core.hooksPath .githooks` enables the repository's `commit-msg` hook, which removes AI attribution
lines so commits carry only the author's identity. Run it once per clone.

## Checks

A change is ready only when all of these pass:

```text
ruff check .
ruff format --check .
mypy src
pytest
```

CI runs the same checks on Python 3.11 and 3.12, on Ubuntu and Windows.

## Rules

- One task from `docs/plan.md` per commit, with its checkboxes ticked in the same commit.
- Tests never use the network. GitHub calls are mocked with respx.
- Never put tokens or API keys in files, tests, fixtures or logs. Use placeholders such as
  `FAKE_TOKEN_FOR_TESTS`.
- Commit messages in the imperative mood, without AI attribution lines.
