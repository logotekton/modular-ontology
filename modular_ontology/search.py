"""Storage-independent query tokenization, scoring and evidence snippets."""

from __future__ import annotations

import re


_QUERY_TOKEN_RE = re.compile(r"[a-z0-9]+|[가-힣]+")
_QUERY_STOPWORDS = {
    "the", "and", "for", "with", "what", "which", "how", "are", "is", "of", "to",
    "this", "that", "from", "about", "tell", "show", "list", "give", "please",
    "설명", "알려", "무엇", "어떤", "어떻게", "그리고", "그것", "대해", "해줘", "주세요", "입니까", "인가요",
}


def query_terms(query: str) -> list[str]:
    """Tokenize a query into searchable substrings.

    ASCII/number words are kept whole; Hangul runs are kept whole (when short) and
    also split into character bigrams so morphological variants (조사 등) still match
    document text. Without this, substring search requires the whole phrase verbatim,
    which makes natural-language Korean questions return nothing.
    """
    terms: list[str] = []
    seen: set[str] = set()

    def push(term: str) -> None:
        if term and term not in seen:
            seen.add(term)
            terms.append(term)

    for token in _QUERY_TOKEN_RE.findall(query.lower()):
        if "가" <= token[0] <= "힣":
            if 2 <= len(token) <= 4 and token not in _QUERY_STOPWORDS:
                push(token)
            for i in range(len(token) - 1):
                push(token[i : i + 2])
        elif token.isdigit():
            push(token)
        elif len(token) >= 2 and token not in _QUERY_STOPWORDS:
            push(token)
    return terms


def score_terms(text_lower: str, terms: list[str]) -> float:
    """Weight longer terms and reward coverage of distinct query terms."""
    if not terms:
        return 0.0
    score = 0.0
    matched = 0
    for term in terms:
        count = text_lower.count(term)
        if count:
            matched += 1
            length_weight = 1.0 + 0.4 * (len(term) - 1)
            score += length_weight * (1.0 + 0.2 * min(count, 5))
    if not matched:
        return 0.0
    return score * (1.0 + 0.5 * matched)


def first_term_hit(text_lower: str, terms: list[str]) -> int:
    first = -1
    for term in terms:
        pos = text_lower.find(term)
        if pos >= 0 and (first < 0 or pos < first):
            first = pos
    return first


def search_snippet(text: str, terms: list[str]) -> str:
    """Return the existing 120-before/260-after window, or the start of the body."""
    hit = max(0, first_term_hit(text.lower(), terms))
    start = max(0, hit - 120)
    end = min(len(text), hit + 260)
    return re.sub(r"\s+", " ", text[start:end]).strip()
