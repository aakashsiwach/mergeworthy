"""Classify a project's policy documents as forbidding, permitting or unclear about AI contributions.

The phrase list lives in ``data/ai_policy_phrases.toml``. The text examined is untrusted repository content:
it is only matched against fixed patterns, never interpreted as instructions.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from importlib import resources

QUOTE_LIMIT = 200


class PolicyVerdict(StrEnum):
    FORBIDS = "forbids"
    PERMITS = "permits"
    UNCLEAR = "unclear"
    NO_POLICY = "no_policy"


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: re.Pattern[str]
    note: str


@dataclass(frozen=True)
class Match:
    kind: str
    note: str
    source: str
    quote: str


@dataclass(frozen=True)
class PolicyResult:
    verdict: PolicyVerdict
    matches: list[Match] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def disclosure_quotes(self) -> list[str]:
        """Sentences about disclosing AI use; the pull request should follow their wording."""
        return [m.quote for m in self.matches if m.kind == "permit" and "isclos" in m.quote]


@dataclass(frozen=True)
class Rules:
    forbid: tuple[Rule, ...]
    permit: tuple[Rule, ...]
    mention: tuple[Rule, ...]


def parse_rules(text: str) -> Rules:
    data = tomllib.loads(text)
    ai_terms = data["ai_terms"]

    def build(kind: str) -> tuple[Rule, ...]:
        rules = []
        for entry in data.get(kind, []):
            pattern = entry["pattern"].replace("{ai}", ai_terms)
            rules.append(Rule(kind, re.compile(pattern, re.IGNORECASE), entry["note"]))
        return tuple(rules)

    return Rules(forbid=build("forbid"), permit=build("permit"), mention=build("mention"))


@cache
def default_rules() -> Rules:
    text = resources.files("mergeworthy").joinpath("data/ai_policy_phrases.toml").read_text(encoding="utf-8")
    return parse_rules(text)


_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-"})
_MARKUP = re.compile(r"[*_`>#|]+")


def normalise(text: str) -> str:
    """Join lines within paragraphs, drop light Markdown markup and collapse whitespace."""
    text = text.translate(_QUOTES).replace("\r\n", "\n")
    paragraphs = re.split(r"\n\s*\n", text)
    cleaned = (_MARKUP.sub(" ", " ".join(p.split())) for p in paragraphs)
    return "\n".join(" ".join(p.split()) for p in cleaned if p.strip())


def _sentence(text: str, start: int, end: int) -> str:
    left = max(text.rfind(c, 0, start) for c in ".!?\n") + 1
    right_candidates = [i for i in (text.find(c, end) for c in ".!?\n") if i != -1]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    quote = text[left:right].strip()
    return quote if len(quote) <= QUOTE_LIMIT else quote[: QUOTE_LIMIT - 3] + "..."


def _find(rules: tuple[Rule, ...], documents: dict[str, str]) -> list[Match]:
    found = []
    for source, text in documents.items():
        for rule in rules:
            m = rule.pattern.search(text)
            if m:
                found.append(Match(rule.kind, rule.note, source, _sentence(text, m.start(), m.end())))
    return found


def classify(documents: dict[str, str], rules: Rules | None = None) -> PolicyResult:
    """Classify documents given as ``{path: text}``."""
    rules = rules or default_rules()
    texts = {path: normalise(text) for path, text in documents.items()}
    sources = sorted(texts)
    forbid = _find(rules.forbid, texts)
    if forbid:
        return PolicyResult(PolicyVerdict.FORBIDS, forbid, sources)
    permit = _find(rules.permit, texts)
    if permit:
        return PolicyResult(PolicyVerdict.PERMITS, permit, sources)
    mention = _find(rules.mention, texts)
    if mention:
        return PolicyResult(PolicyVerdict.UNCLEAR, mention, sources)
    return PolicyResult(PolicyVerdict.NO_POLICY, [], sources)
