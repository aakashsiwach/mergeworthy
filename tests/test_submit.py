"""Submission: gates, description template, dry run, and a real run against local git repositories."""

import json
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import respx
from fakes import FakeGitHub, load_fixture, make_issue
from pydantic import SecretStr
from test_approve import DIFF, waiting_attempt
from typer.testing import CliRunner

from mergeworthy import cli
from mergeworthy.approve import approve, render_diff
from mergeworthy.config import Config, parse_config
from mergeworthy.github import API_URL, LinkedPullRequest
from mergeworthy.gitops import Git, GitError, Identity
from mergeworthy.ledger import AttemptState, Ledger
from mergeworthy.submit import (
    DEFAULT_DISCLOSURE,
    GitHubWriter,
    SubmissionRefused,
    prepare_submission,
    pull_request_body,
    render_plan,
    submit,
)

UPSTREAM = "owner/repo"
ME = Identity("Test Person", "person@example.com")
TOKEN = SecretStr("FAKE_TOKEN_FOR_TESTS")


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Setup", "-c", "user.email=setup@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def remotes(tmp_path: Path) -> tuple[Path, str]:
    """A local 'GitHub': remotes/owner/repo.git (bare) with src/app.py = 'old'. Returns (root, commit)."""
    work = tmp_path / "seed"
    (work / "src").mkdir(parents=True)
    (work / "src" / "app.py").write_bytes(b"old\n")
    sh("init", "-q", "-b", "main", cwd=work)
    sh("add", "-A", cwd=work)
    sh("commit", "-q", "-m", "initial", cwd=work)
    root = tmp_path / "remotes"
    (root / "owner").mkdir(parents=True)
    sh("clone", "-q", "--bare", str(work), str(root / "owner" / "repo.git"), cwd=tmp_path)
    return root, sh("rev-parse", "HEAD", cwd=work)


def github() -> FakeGitHub:
    fake = load_fixture("welcoming", full_name=UPSTREAM)
    fake.issues[12] = make_issue(UPSTREAM, 12)
    return FakeGitHub(fake)


def config(tmp_path: Path, **overrides: object) -> Config:
    data: dict[str, object] = {
        "allowlist": [UPSTREAM],
        "dry_run": False,
        "ledger_path": str(tmp_path / "ledger.sqlite3"),
    }
    data.update(overrides)
    return parse_config(data)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "ledger.sqlite3") as led:
        yield led


def approved_attempt(ledger: Ledger, base_commit: str = "a" * 40) -> int:
    attempt = waiting_attempt(ledger)
    approve(ledger, attempt, "read it", render_diff(ledger, attempt))
    ledger.update_attempt(attempt, base_commit=base_commit)
    return attempt


def plan_for(ledger: Ledger, attempt: int, cfg: Config, gh: FakeGitHub | None = None, **kw: object):  # type: ignore[no-untyped-def]
    return prepare_submission(
        gh or github(),
        ledger,
        cfg,
        attempt,
        identity=ME,
        now=datetime.now(UTC),
        **kw,  # type: ignore[arg-type]
    )


# --- gates ----------------------------------------------------------------------------------------


def test_refuses_repository_not_on_allowlist(ledger: Ledger, tmp_path: Path) -> None:
    attempt = approved_attempt(ledger)
    with pytest.raises(SubmissionRefused, match="not on the allowlist"):
        plan_for(ledger, attempt, config(tmp_path, allowlist=[]))
    with pytest.raises(SubmissionRefused, match="not on the allowlist"):
        plan_for(ledger, attempt, config(tmp_path, allowlist=["owner/other"]))


def test_deny_list_wins_over_allowlist(ledger: Ledger, tmp_path: Path) -> None:
    attempt = approved_attempt(ledger)
    with pytest.raises(SubmissionRefused, match="not on the allowlist"):
        plan_for(ledger, attempt, config(tmp_path, deny_list=[UPSTREAM]))
    ledger.upsert_repository(UPSTREAM, deny_listed=True)
    with pytest.raises(SubmissionRefused, match="deny list"):
        plan_for(ledger, attempt, config(tmp_path))


@pytest.mark.parametrize("state", [AttemptState.AWAITING_APPROVAL, AttemptState.HUMAN_REJECTED])
def test_refuses_attempts_that_are_not_approved(ledger: Ledger, tmp_path: Path, state: AttemptState) -> None:
    attempt = waiting_attempt(ledger)
    if state is AttemptState.HUMAN_REJECTED:
        ledger.transition(attempt, state, "no")
    with pytest.raises(SubmissionRefused, match="only approved attempts"):
        plan_for(ledger, attempt, config(tmp_path))


def test_refuses_a_diff_changed_after_approval(ledger: Ledger, tmp_path: Path) -> None:
    attempt = approved_attempt(ledger)
    ledger.update_attempt(attempt, diff=DIFF.replace("+new", "+evil"))
    with pytest.raises(SubmissionRefused, match="SHA-256"):
        plan_for(ledger, attempt, config(tmp_path))


def test_refuses_files_that_were_not_approved(ledger: Ledger, tmp_path: Path) -> None:
    attempt = approved_attempt(ledger)
    ledger._conn.execute("UPDATE attempts SET approved_files = ? WHERE id = ?", ('["src/app.py"]', attempt))
    with pytest.raises(SubmissionRefused, match="approved files"):
        plan_for(ledger, attempt, config(tmp_path))


def test_refuses_during_cooldown_and_over_limits(ledger: Ledger, tmp_path: Path) -> None:
    attempt = approved_attempt(ledger)
    later = (datetime.now(UTC) + timedelta(days=5)).isoformat(timespec="seconds")
    ledger.upsert_repository(UPSTREAM, cooldown_until=later)
    with pytest.raises(SubmissionRefused, match="cooldown"):
        plan_for(ledger, attempt, config(tmp_path))
    ledger.upsert_repository(UPSTREAM, cooldown_until=None)
    with pytest.raises(SubmissionRefused, match="weekly limit"):
        plan_for(ledger, attempt, config(tmp_path, limits={"prs_per_week": 0}))
    with pytest.raises(SubmissionRefused, match="open pull request"):
        plan_for(ledger, attempt, config(tmp_path, limits={"open_prs_per_repository": 0}))


def test_refuses_when_issue_was_taken(ledger: Ledger, tmp_path: Path) -> None:
    attempt = approved_attempt(ledger)
    gh = github()
    gh.repos[UPSTREAM].linked[12] = [
        LinkedPullRequest(
            repository=UPSTREAM, number=9, state="open", merged=False, html_url="https://x/pull/9"
        )
    ]
    with pytest.raises(SubmissionRefused, match="no longer available"):
        plan_for(ledger, attempt, config(tmp_path), gh)


# --- description ----------------------------------------------------------------------------------


def test_pull_request_description_template(ledger: Ledger, tmp_path: Path) -> None:
    attempt = approved_attempt(ledger)
    ledger.update_attempt(
        attempt,
        verification=[
            {"rule": "baseline", "passed": True, "reason": "baseline tests passed"},
            {"rule": "tests_pass", "passed": True, "reason": "ok"},
            {"rule": "tests_fail_without_fix", "passed": True, "reason": "fail without"},
            {"rule": "linters", "passed": True, "reason": "no linters or formatters configured"},
        ],
    )
    plan = plan_for(ledger, attempt, config(tmp_path))
    body = plan.body
    assert body.startswith("Fixes #12\n\n## Summary\n\nReturn an empty result for empty input")
    assert "- The test suite passed on the unmodified repository." in body
    assert "- The new or changed tests fail without the fix and pass with it." in body
    assert "linters" not in body  # no configured linters, so no claim about them
    assert body.rstrip().endswith(DEFAULT_DISCLOSURE)
    assert plan.title == "Crash on empty input"
    assert plan.commit_message == "Crash on empty input\n\nFixes #12\n"
    assert plan.files == ["src/app.py", "tests/test_app.py"]


def test_project_disclosure_wording_must_be_supplied(ledger: Ledger, tmp_path: Path) -> None:
    attempt = approved_attempt(ledger)
    ledger.upsert_repository(
        UPSTREAM,
        policy_check={"ai_policy": {"details": {"disclosure": ["Disclose AI use with 'AI-assisted: yes'."]}}},
    )
    plan = plan_for(ledger, attempt, config(tmp_path))
    assert plan.warnings and "pass --disclosure" in plan.warnings[0]
    custom = plan_for(ledger, attempt, config(tmp_path), disclosure="AI-assisted: yes")
    assert custom.warnings == []
    assert custom.body.rstrip().endswith("AI-assisted: yes")


def test_documentation_only_wording() -> None:
    body = pull_request_body(
        issue_number=3,
        explanation="Fix typo",
        verification=[
            {"rule": "tests_fail_without_fix", "passed": True, "reason": "documentation-only: exempt"}
        ],
        disclosure="x",
    )
    assert "Documentation-only change, so no new test." in body


# --- dry run --------------------------------------------------------------------------------------


def test_render_plan_shows_exactly_what_would_be_sent(ledger: Ledger, tmp_path: Path) -> None:
    attempt = approved_attempt(ledger)
    plan = plan_for(ledger, attempt, config(tmp_path))
    text = render_plan(plan, Git(), push_repository="me/repo", fork_needed=True)
    assert f"POST /repos/{UPSTREAM}/forks" in text
    assert "fresh clone of https://github.com/owner/repo.git at " + "a" * 40 in text
    assert f"approved diff (sha256 {plan.diff_sha256})" in text
    assert "commit as Test Person <person@example.com>" in text
    assert "push to https://github.com/me/repo.git refs/heads/" in text
    payload = json.loads(text[text.index("{\n") :])
    assert payload["head"] == f"me:{plan.branch}"
    assert payload["base"] == "main"
    assert payload["body"] == plan.body


class NoGit(Git):
    """Fails on any git command except reading the identity."""

    def identity(self) -> Identity:
        return ME

    def run(self, args: list[str], **kwargs: object) -> str:  # type: ignore[override]
        raise AssertionError(f"git must not run in a dry run: {args}")


@pytest.mark.parametrize(
    ("toml_dry_run", "flag"), [("true", "--no-dry-run"), ("false", "--dry-run"), ("true", "--dry-run")]
)
def test_cli_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml_dry_run: str, flag: str
) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    with Ledger(ledger_path) as led:
        attempt = approved_attempt(led)
    cfg = tmp_path / "mergeworthy.toml"
    cfg.write_text(
        f'dry_run = {toml_dry_run}\nallowlist = ["{UPSTREAM}"]\nledger_path = "{ledger_path.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "make_client", github)
    monkeypatch.setattr(cli, "make_git", NoGit)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with respx.mock(base_url=API_URL, assert_all_called=False) as api:
        post = api.post(path__regex=r".*").respond(500)
        result = CliRunner().invoke(cli.app, ["submit", str(attempt), flag, "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert "nothing was sent" in result.output
    assert not post.called
    with Ledger(ledger_path) as led:
        assert led.get_attempt(attempt).state is AttemptState.APPROVED


# --- real submission against local repositories ---------------------------------------------------


def writer_api(api: respx.MockRouter, *, can_push: bool) -> dict[str, respx.Route]:
    routes = {
        "user": api.get("/user").respond(json={"login": "me"}),
        "repo": api.get(f"/repos/{UPSTREAM}").respond(json={"permissions": {"push": can_push}}),
        "fork": api.post(f"/repos/{UPSTREAM}/forks").respond(202, json={"full_name": "me/repo"}),
        "pull": api.post(f"/repos/{UPSTREAM}/pulls").respond(
            201, json={"number": 77, "html_url": f"https://github.com/{UPSTREAM}/pull/77"}
        ),
    }
    return routes


@pytest.mark.parametrize("can_push", [True, False])
def test_real_submission_from_a_clean_clone(
    ledger: Ledger, tmp_path: Path, remotes: tuple[Path, str], can_push: bool
) -> None:
    root, base = remotes
    if not can_push:
        (root / "me").mkdir()
        sh(
            "clone",
            "-q",
            "--bare",
            str(root / "owner" / "repo.git"),
            str(root / "me" / "repo.git"),
            cwd=tmp_path,
        )
    attempt = approved_attempt(ledger, base_commit=base)
    cfg = config(tmp_path)
    gh = github()
    plan = plan_for(ledger, attempt, cfg, gh)
    git = Git(base_url=root.as_posix())
    with respx.mock(base_url=API_URL, assert_all_called=False) as api:
        routes = writer_api(api, can_push=can_push)
        writer = GitHubWriter(TOKEN, cfg)
        result = submit(
            plan, gh=gh, writer=writer, ledger=ledger, config=cfg, git=git, token=TOKEN, workspace=tmp_path
        )
        assert routes["fork"].called is (not can_push)
        sent = json.loads(routes["pull"].calls.last.request.content)

    target = root / ("owner" if can_push else "me") / "repo.git"
    log = sh("log", "-1", "--format=%an <%ae>%n%s", plan.branch, cwd=target)
    assert log == "Test Person <person@example.com>\nCrash on empty input"
    files = sh("diff", "--name-only", base, plan.branch, cwd=target).split()
    assert files == ["src/app.py", "tests/test_app.py"]
    assert sh("show", f"{plan.branch}:src/app.py", cwd=target) == "new"

    assert result.number == 77
    assert sent == {
        "title": "Crash on empty input",
        "head": f"{'owner' if can_push else 'me'}:{plan.branch}",
        "base": "main",
        "body": plan.body,
        "maintainer_can_modify": True,
    }
    assert ledger.get_attempt(attempt).state is AttemptState.SUBMITTED
    [pr] = ledger.pull_requests()
    assert (pr["repository"], pr["number"], pr["state"], pr["branch"]) == (UPSTREAM, 77, "open", plan.branch)
    assert not list(tmp_path.glob("mergeworthy-submit-*"))  # the clean clone is removed


def test_submission_rechecks_and_abandons_before_writing(ledger: Ledger, tmp_path: Path) -> None:
    attempt = approved_attempt(ledger)
    cfg = config(tmp_path)
    gh = github()
    plan = plan_for(ledger, attempt, cfg, gh)
    gh.repos[UPSTREAM].issues[12] = make_issue(UPSTREAM, 12, state="closed")
    from mergeworthy.recheck import NoLongerAvailable

    with respx.mock(base_url=API_URL, assert_all_called=False) as api:
        routes = writer_api(api, can_push=True)
        with pytest.raises(NoLongerAvailable):
            submit(
                plan,
                gh=gh,
                writer=GitHubWriter(TOKEN, cfg),
                ledger=ledger,
                config=cfg,
                git=NoGit(),
                token=TOKEN,
            )
        assert not routes["pull"].called
    assert ledger.get_attempt(attempt).state is AttemptState.ABANDONED


def test_writer_guards_every_write(tmp_path: Path) -> None:
    with respx.mock(base_url=API_URL, assert_all_called=False) as api:
        post = api.post(path__regex=r".*").respond(201, json={})
        dry = GitHubWriter(TOKEN, config(tmp_path, dry_run=True))
        with pytest.raises(SubmissionRefused, match="dry_run"):
            dry.create_pull_request(UPSTREAM, head="me:b", base="main", title="t", body="b")
        outside = GitHubWriter(TOKEN, config(tmp_path, allowlist=["owner/other"]))
        with pytest.raises(SubmissionRefused, match="allowlist"):
            outside.create_fork(UPSTREAM)
        assert not post.called


def test_git_output_never_contains_the_token(tmp_path: Path) -> None:
    with pytest.raises(GitError) as info:
        Git().run(
            ["rev-parse", "--verify", "FAKE_TOKEN_FOR_TESTS"], cwd=tmp_path, token="FAKE_TOKEN_FOR_TESTS"
        )
    assert "FAKE_TOKEN_FOR_TESTS" not in str(info.value)


# --- branches ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("number", "title", "expected"),
    [
        (12, "Crash on empty input", "mergeworthy/12-crash-on-empty-input"),
        (7, "Fix `parse()` -- handles  UTF-8 \u00e9!", "mergeworthy/7-fix-parse-handles-utf-8"),
        (3, "!!!", "mergeworthy/3-change"),
        (
            9,
            "A very long title that keeps going well past the forty character limit",
            "mergeworthy/9-a-very-long-title-that-keeps-going-well",
        ),
    ],
)
def test_branch_name(number: int, title: str, expected: str) -> None:
    from mergeworthy.gitops import branch_name, check_branch

    branch = branch_name(number, title)
    assert branch == expected
    check_branch(branch, issue_number=number, default_branch="main")


@pytest.mark.parametrize(
    ("branch", "reason"),
    [
        ("main", "only mergeworthy/"),
        ("mergeworthy/issue-12-1", "only mergeworthy/"),
        ("mergeworthy/12-../../main", "only mergeworthy/"),
        ("mergeworthy/12-Crash", "only mergeworthy/"),
        ("feature/12-crash", "only mergeworthy/"),
        ("mergeworthy/13-crash", "not for issue #12"),
        ("mergeworthy/12-crash", "default branch"),
    ],
)
def test_check_branch_refuses(branch: str, reason: str) -> None:
    from mergeworthy.gitops import check_branch

    default = "mergeworthy/12-crash" if reason == "default branch" else "main"
    with pytest.raises(GitError, match=reason):
        check_branch(branch, issue_number=12, default_branch=default)


def test_plan_uses_the_derived_branch_not_stored_data(ledger: Ledger, tmp_path: Path) -> None:
    attempt = approved_attempt(ledger)
    ledger.update_attempt(attempt, branch="main")  # stored data is never trusted for the branch
    plan = plan_for(ledger, attempt, config(tmp_path))
    assert plan.branch == "mergeworthy/12-crash-on-empty-input"


def test_refuses_when_branch_would_be_the_default_branch(ledger: Ledger, tmp_path: Path) -> None:
    attempt = approved_attempt(ledger)
    gh = github()
    repo = gh.repos[UPSTREAM].repository
    gh.repos[UPSTREAM].repository = repo.model_copy(
        update={"default_branch": "mergeworthy/12-crash-on-empty-input"}
    )
    with pytest.raises(SubmissionRefused, match="default branch"):
        plan_for(ledger, attempt, config(tmp_path), gh)


def test_refuses_to_push_to_an_existing_branch(
    ledger: Ledger, tmp_path: Path, remotes: tuple[Path, str]
) -> None:
    root, base = remotes
    bare = root / "owner" / "repo.git"
    sh("branch", "mergeworthy/12-crash-on-empty-input", base, cwd=bare)  # someone already has this branch
    before = sh("rev-parse", "mergeworthy/12-crash-on-empty-input", cwd=bare)
    attempt = approved_attempt(ledger, base_commit=base)
    cfg = config(tmp_path)
    gh = github()
    plan = plan_for(ledger, attempt, cfg, gh)
    with respx.mock(base_url=API_URL, assert_all_called=False) as api:
        routes = writer_api(api, can_push=True)
        with pytest.raises(SubmissionRefused, match="already exists"):
            submit(
                plan,
                gh=gh,
                writer=GitHubWriter(TOKEN, cfg),
                ledger=ledger,
                config=cfg,
                git=Git(base_url=root.as_posix()),
                token=TOKEN,
                workspace=tmp_path,
            )
        assert not routes["pull"].called
    assert sh("rev-parse", "mergeworthy/12-crash-on-empty-input", cwd=bare) == before
    assert ledger.get_attempt(attempt).state is AttemptState.APPROVED


def test_push_itself_never_updates_an_existing_branch(tmp_path: Path, remotes: tuple[Path, str]) -> None:
    root, base = remotes
    bare = root / "owner" / "repo.git"
    git = Git(base_url=root.as_posix())
    clone = tmp_path / "clone"
    git.fresh_clone(UPSTREAM, base, "mergeworthy/5-x", clone)
    (clone / "new.txt").write_text("x", encoding="utf-8")
    git.commit(clone, ["new.txt"], ME, "x")
    git.push(clone, UPSTREAM, "mergeworthy/5-x", None)  # creating a new branch works
    (clone / "new.txt").write_text("y", encoding="utf-8")
    git.commit(clone, ["new.txt"], ME, "y")
    with pytest.raises(GitError, match="push failed"):  # the lease refuses: the branch now exists
        git.push(clone, UPSTREAM, "mergeworthy/5-x", None)
    for forbidden in ("main", "refs/heads/main", "mergeworthy/5-x:main"):
        with pytest.raises(GitError, match="refusing to push"):
            git.push(clone, UPSTREAM, forbidden, None)
    assert sh("rev-parse", "main", cwd=bare) == base
