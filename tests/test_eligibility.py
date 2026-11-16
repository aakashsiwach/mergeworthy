from datetime import timedelta

import pytest
from fakes import NOW, FakeGitHub, days_ago, load_fixture, make_issue, outside_merge

from mergeworthy.config import Config, parse_config
from mergeworthy.eligibility import (
    Report,
    Status,
    check_issue,
    check_repository,
    fetch_policy_documents,
)
from mergeworthy.github import Comment, LinkedPullRequest

CONFIG = Config()


def repo_report(name: str, config: Config = CONFIG, **kwargs: object) -> Report:
    fake = load_fixture(name, **kwargs)  # type: ignore[arg-type]
    gh = FakeGitHub(fake)
    _, _, report = check_repository(gh, config, fake.repository.full_name, now=NOW)
    return report


def status(report: Report, name: str) -> Status:
    result = report.get(name)
    assert result is not None, name
    return result.status


# --- repositories -----------------------------------------------------------------------------------------


def test_welcoming_repository_is_eligible() -> None:
    report = repo_report("welcoming")
    assert report.eligible, report.summary()
    assert not report.needs_human
    assert status(report, "tests_in_sandbox") is Status.DEFERRED
    assert report.get("claim_first").details["required"] is False  # type: ignore[union-attr]
    assert {r.name for r in report.results} == {
        "public",
        "not_archived",
        "license",
        "language",
        "recent_commit",
        "outside_merge",
        "contributing_guide",
        "ai_policy",
        "not_denied",
        "cooldown",
        "tests_in_sandbox",
        "claim_first",
    }


def test_policy_documents_are_found_in_all_locations() -> None:
    fake = load_fixture("welcoming")
    docs = fetch_policy_documents(FakeGitHub(fake), fake.repository)
    assert set(docs.contributing) == {"CONTRIBUTING.md"}
    assert set(docs.code_of_conduct) == {"CODE_OF_CONDUCT.md"}
    assert set(docs.pull_request_templates) == {".github/pull_request_template.md"}
    template_dir = load_fixture("ai_template")
    docs = fetch_policy_documents(FakeGitHub(template_dir), template_dir.repository)
    assert set(docs.pull_request_templates) == {".github/PULL_REQUEST_TEMPLATE/default.md"}


@pytest.mark.parametrize("fixture", ["ai_forbidden", "ai_discouraged", "ai_template"])
def test_ai_forbidding_policy_fails(fixture: str) -> None:
    report = repo_report(fixture)
    assert not report.eligible
    assert not report.needs_human
    assert status(report, "ai_policy") is Status.FAIL
    assert report.get("ai_policy").details["matches"]  # type: ignore[union-attr]


def test_ai_policy_file_is_read() -> None:
    report = repo_report("ai_forbidden")
    assert "AI_POLICY.md" in report.get("ai_policy").reason  # type: ignore[union-attr]


def test_unclear_ai_policy_is_skipped_and_flagged() -> None:
    report = repo_report("ai_unclear")
    assert not report.eligible
    assert report.needs_human
    assert status(report, "ai_policy") is Status.UNCLEAR
    assert status(report, "contributing_guide") is Status.PASS  # docs/CONTRIBUTING.md


def test_ai_policy_with_disclosure_passes_and_records_wording() -> None:
    report = repo_report("ai_disclosure")
    assert report.eligible, report.summary()
    result = report.get("ai_policy")
    assert result is not None
    assert result.details["verdict"] == "permits"
    assert result.details["disclosure"]


def test_missing_contributing_guide_fails() -> None:
    report = repo_report("no_guide")
    assert status(report, "contributing_guide") is Status.FAIL
    assert not report.eligible


def test_claim_first_is_detected() -> None:
    report = repo_report("claim_first")
    assert report.eligible
    assert report.get("claim_first").details["required"] is True  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("overrides", "check"),
    [
        ({"private": True}, "public"),
        ({"archived": True}, "not_archived"),
        ({"license_spdx_id": None}, "license"),
        ({"license_spdx_id": "NOASSERTION"}, "license"),
        ({"license_spdx_id": "BUSL-1.1"}, "license"),
        ({"language": "Rust"}, "language"),
        ({"language": None}, "language"),
    ],
)
def test_repository_metadata_failures(overrides: dict[str, object], check: str) -> None:
    report = repo_report("welcoming", **overrides)
    assert status(report, check) is Status.FAIL
    assert not report.eligible


def test_license_check_accepts_other_osi_licenses() -> None:
    assert status(repo_report("welcoming", license_spdx_id="Apache-2.0"), "license") is Status.PASS


def test_language_must_be_configured() -> None:
    config = parse_config({"languages": ["java"]})
    assert status(repo_report("welcoming", config), "language") is Status.FAIL


def test_inactive_repository_fails() -> None:
    fake = load_fixture("welcoming")
    fake.commits[0] = fake.commits[0].model_copy(update={"committed_at": days_ago(31)})
    _, _, report = check_repository(FakeGitHub(fake), CONFIG, fake.repository.full_name, now=NOW)
    assert status(report, "recent_commit") is Status.FAIL


@pytest.mark.parametrize(
    "merged",
    [
        [],
        [outside_merge(days=91)],
        [outside_merge(association="MEMBER")],
        [outside_merge(association="OWNER")],
        [outside_merge(association="COLLABORATOR")],
        [outside_merge(author="dependabot[bot]", association="NONE")],
    ],
)
def test_no_recent_outside_merge_fails(merged: list[object]) -> None:
    fake = load_fixture("welcoming")
    fake.merged = merged  # type: ignore[assignment]
    _, _, report = check_repository(FakeGitHub(fake), CONFIG, fake.repository.full_name, now=NOW)
    assert status(report, "outside_merge") is Status.FAIL


def test_first_time_contributor_counts_as_outside() -> None:
    fake = load_fixture("welcoming")
    fake.merged = [outside_merge(association="FIRST_TIME_CONTRIBUTOR")]
    _, _, report = check_repository(FakeGitHub(fake), CONFIG, fake.repository.full_name, now=NOW)
    assert status(report, "outside_merge") is Status.PASS


def test_deny_list_from_config_and_ledger() -> None:
    fake = load_fixture("welcoming")
    gh = FakeGitHub(fake)
    name = fake.repository.full_name
    config = parse_config({"deny_list": [name]})
    _, _, report = check_repository(gh, config, name, now=NOW)
    assert status(report, "not_denied") is Status.FAIL
    _, _, report = check_repository(gh, CONFIG, name, now=NOW, ledger_denied=True)
    assert status(report, "not_denied") is Status.FAIL


def test_cooldown() -> None:
    fake = load_fixture("welcoming")
    gh = FakeGitHub(fake)
    name = fake.repository.full_name
    _, _, report = check_repository(gh, CONFIG, name, now=NOW, cooldown_until=NOW + timedelta(days=1))
    assert status(report, "cooldown") is Status.FAIL
    _, _, report = check_repository(gh, CONFIG, name, now=NOW, cooldown_until=NOW - timedelta(days=1))
    assert status(report, "cooldown") is Status.PASS


# --- issues -----------------------------------------------------------------------------------------------


def issue_report(**issue_overrides: object) -> tuple[FakeGitHub, Report]:
    fake = load_fixture("welcoming")
    gh = FakeGitHub(fake)
    issue = make_issue(fake.repository.full_name, 7, **issue_overrides)
    return gh, check_issue(gh, CONFIG, issue, now=NOW)


def test_good_issue_is_eligible() -> None:
    _, report = issue_report()
    assert report.eligible, report.summary()


@pytest.mark.parametrize(
    ("overrides", "check"),
    [
        ({"labels": ["bug"]}, "help_label"),
        ({"state": "closed"}, "open"),
        ({"assignees": ["someone"]}, "unassigned"),
        ({"body": "Broken."}, "specific"),
    ],
)
def test_issue_failures(overrides: dict[str, object], check: str) -> None:
    _, report = issue_report(**overrides)
    assert status(report, check) is Status.FAIL
    assert not report.eligible


def test_assigned_issue_reason_names_assignee() -> None:
    _, report = issue_report(assignees=["someone"])
    assert "someone" in report.get("unassigned").reason  # type: ignore[union-attr]


def test_label_match_is_case_insensitive() -> None:
    _, report = issue_report(labels=["Help Wanted"])
    assert status(report, "help_label") is Status.PASS


def test_vague_issue_needs_human() -> None:
    body = "It would be nice to have a nicer way of handling configuration in the main module overall."
    _, report = issue_report(body=body)
    assert status(report, "specific") is Status.UNCLEAR
    assert report.needs_human


def make_repo_with(issue_number: int = 7) -> tuple[FakeGitHub, str]:
    fake = load_fixture("welcoming")
    return FakeGitHub(fake), fake.repository.full_name


def test_linked_open_pull_request_fails() -> None:
    gh, name = make_repo_with()
    gh.repos[name].linked[7] = [
        LinkedPullRequest(repository=name, number=8, state="open", merged=False, html_url="https://x/pull/8")
    ]
    report = check_issue(gh, CONFIG, make_issue(name, 7), now=NOW)
    assert status(report, "no_open_pull_request") is Status.FAIL
    assert "https://x/pull/8" in report.get("no_open_pull_request").reason  # type: ignore[union-attr]


def test_linked_closed_pull_request_passes() -> None:
    gh, name = make_repo_with()
    gh.repos[name].linked[7] = [
        LinkedPullRequest(
            repository=name, number=8, state="closed", merged=False, html_url="https://x/pull/8"
        )
    ]
    report = check_issue(gh, CONFIG, make_issue(name, 7), now=NOW)
    assert status(report, "no_open_pull_request") is Status.PASS


def comment(body: str, days: float, author: str = "helper") -> Comment:
    return Comment(id=1, author=author, author_association="NONE", body=body, created_at=days_ago(days))


@pytest.mark.parametrize(
    "body",
    [
        "I'm working on this!",
        "I am working on a fix",
        "Can I work on this?",
        "I\u2019d like to take this one.",
        "I'll take this",
        "Please assign this to me",
        "/assign",
    ],
)
def test_recent_claim_comment_fails(body: str) -> None:
    gh, name = make_repo_with()
    gh.repos[name].comments[7] = [comment(body, days=3)]
    report = check_issue(gh, CONFIG, make_issue(name, 7), now=NOW)
    assert status(report, "no_recent_claim") is Status.FAIL


@pytest.mark.parametrize(
    "c",
    [
        comment("I'm working on this!", days=31),  # too old
        comment("I'm working on this!", days=1, author="stale[bot]"),  # bots do not count
        comment("This also happens on Windows.", days=1),
    ],
)
def test_other_comments_pass(c: Comment) -> None:
    gh, name = make_repo_with()
    gh.repos[name].comments[7] = [c]
    report = check_issue(gh, CONFIG, make_issue(name, 7), now=NOW)
    assert status(report, "no_recent_claim") is Status.PASS
