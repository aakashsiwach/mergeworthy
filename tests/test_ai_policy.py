import pytest

from mergeworthy.ai_policy import PolicyVerdict, classify, default_rules, normalise


def verdict(text: str) -> PolicyVerdict:
    return classify({"CONTRIBUTING.md": text}).verdict


@pytest.mark.parametrize(
    "text",
    [
        "We do not accept AI-generated contributions.",
        "We don't accept pull requests written by ChatGPT, Copilot or similar tools.",
        "We will not review\ncode produced by large language models.",
        "AI-generated pull requests are not welcome here.",
        "Contributions created with LLMs will not be accepted.",
        "The use of generative AI is prohibited in this project.",
        "Pull requests that appear to be AI-generated will be closed without review.",
        "We ban AI-written patches.",
        "No AI-generated code, please.",
        "We strongly discourage the use of AI coding assistants.",
        "Using AI tools is discouraged.",
        "Please do not use ChatGPT or Copilot to write your contribution.",
        "Do not submit AI generated code.",
        "We only accept contributions written entirely by humans.",
        "We don\u2019t accept **AI**-generated changes.",
    ],
)
def test_forbidding_phrases(text: str) -> None:
    assert verdict(text) is PolicyVerdict.FORBIDS


@pytest.mark.parametrize(
    "text",
    [
        "AI-assisted contributions are welcome.",
        "You may use AI tools, but you are responsible for the result.",
        "AI tools are allowed as long as you understand the code.",
        "Please disclose any use of AI in your pull request description.",
        "Use of AI assistants must be disclosed in the PR.",
    ],
)
def test_permitting_phrases(text: str) -> None:
    assert verdict(text) is PolicyVerdict.PERMITS


@pytest.mark.parametrize(
    "text",
    [
        "We know many people use LLMs. Think carefully about what you submit.",
        "Our position on AI contributions is still being discussed.",
        "See the discussion about Copilot in issue 42.",
    ],
)
def test_unclear_phrases(text: str) -> None:
    assert verdict(text) is PolicyVerdict.UNCLEAR


@pytest.mark.parametrize(
    "text",
    [
        "Fork the repository and open a pull request. Run the tests first.",
        "This project integrates with the OpenAI API.",  # "AI" inside a word is not a mention
        "Wait for the CI checks to pass, then request a review.",  # lower-case "ai" inside words
        "We do not accept pull requests without tests.",
    ],
)
def test_no_policy(text: str) -> None:
    assert verdict(text) is PolicyVerdict.NO_POLICY


def test_forbid_wins_over_permit_across_documents() -> None:
    result = classify(
        {
            "CONTRIBUTING.md": "AI-assisted contributions are welcome.",
            "AI_POLICY.md": "We do not accept AI-generated code.",
        }
    )
    assert result.verdict is PolicyVerdict.FORBIDS
    assert result.matches[0].source == "AI_POLICY.md"
    assert result.sources == ["AI_POLICY.md", "CONTRIBUTING.md"]


def test_quote_is_the_matching_sentence() -> None:
    result = classify({"C.md": "Welcome! We do not accept AI-generated code. Thanks for reading."})
    assert result.matches[0].quote == "We do not accept AI-generated code."


def test_disclosure_wording_is_recorded() -> None:
    result = classify({"C.md": "Please disclose the use of AI tools with the line 'AI-assisted: yes'."})
    assert result.verdict is PolicyVerdict.PERMITS
    assert result.disclosure_quotes


def test_normalise_joins_lines_and_strips_markup() -> None:
    assert (
        normalise("We do **not**\naccept AI.\n\nNext\r\nparagraph") == "We do not accept AI.\nNext paragraph"
    )


def test_every_rule_has_a_note() -> None:
    rules = default_rules()
    assert rules.forbid
    assert rules.permit
    assert rules.mention
    assert all(rule.note for rule in (*rules.forbid, *rules.permit, *rules.mention))
