import re
import unicodedata
from typing import Any


def text_boundary(text, limit, lower=0, *, sentences=True):
    """Choose an original-source offset before a budget, never rewrite text.

    Prefer sentence boundaries, then whitespace. Unbroken text may still need
    a hard split; every caller retains overlap and complete source coverage.
    """
    limit = min(len(text), limit)
    if limit >= len(text) or limit <= lower:
        return limit
    if sentences:
        matches = list(re.finditer(r'[.!?][\)\]"\u201d]?\s+(?=[A-Z])', text[lower:limit]))
        if matches:
            return lower + matches[-1].end()
    if text[limit].isspace() or text[limit-1].isspace():
        return limit
    spaces = list(re.finditer(r'\s+', text[lower:limit]))
    return lower + spaces[-1].end() if spaces else limit


def _valid_page(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def page_bounds(spans: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    """Only report a range when every covered text span has known pages."""
    if not spans:
        return None, None
    starts = [_valid_page(span.get("page_start")) for span in spans]
    ends = [_valid_page(span.get("page_end")) for span in spans]
    if None in starts or None in ends or any(start > end for start, end in zip(starts, ends)):
        return None, None
    return min(starts), max(ends)


def normalized_text_with_offsets(text: str) -> tuple[str, list[int]]:
    """Normalize PDF typography for text alignment, retaining original offsets."""
    line_hyphens = {match.start() for match in re.finditer(
        r"(?<=\w)[-\u2010]\s*\n\s*(?=\w)", text,
    )}
    translation = str.maketrans({
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u2010": "-", "\u2011": "-", "\u2013": "-", "\u2014": "-",
    })
    characters, offsets = [], []
    for offset, character in enumerate(text):
        if offset in line_hyphens or character == "\u00ad":
            continue
        for normalized in unicodedata.normalize("NFKC", character).casefold().translate(translation):
            if not normalized.isspace():
                characters.append(normalized)
                offsets.append(offset)
    return "".join(characters), offsets


def _evidence_matches(sentence: str, text: str) -> list[tuple[int, int]]:
    tokens = sentence.split()
    if not tokens:
        return []
    pattern = r"\s+".join(re.escape(token) for token in tokens)
    matches = [(match.start(), match.end()) for match in re.finditer(pattern, text, re.I)]
    if matches:
        return matches
    needle, _ = normalized_text_with_offsets(sentence)
    haystack, offsets = normalized_text_with_offsets(text)
    if not needle:
        return []
    start = haystack.find(needle)
    while start >= 0:
        matches.append((offsets[start], offsets[start + len(needle) - 1] + 1))
        start = haystack.find(needle, start + 1)
    return matches


def locate_evidence_pages(
    sentence: str,
    chunk: dict[str, Any],
) -> tuple[int | None, int | None]:
    """Locate an evidence sentence using parser offsets, never LLM page guesses."""
    spans = chunk.get("page_spans") or []
    if sentence and spans:
        matches = _evidence_matches(sentence, chunk["text"])
        if matches:
            covered = [
                span
                for span in spans
                if any(
                    span["start"] < end and span["end"] > start
                    for start, end in matches
                )
            ]
            return page_bounds(covered)

    # If the sentence cannot be matched, retain the whole chunk's known range.
    return page_bounds([chunk])


def evidence_location(row: dict[str, Any]) -> dict[str, Any]:
    claim_json = row.get("claim_json") or {}
    evidence = claim_json.get("evidence") or {}
    page = _valid_page(evidence.get("page"))
    if page is not None:
        page_end = _valid_page(evidence.get("page_end")) or page
    else:
        page = _valid_page(row.get("chunk_page_start"))
        page_end = _valid_page(row.get("chunk_page_end")) or page
    return {
        "title": row.get("title"),
        "doi": row.get("doi"),
        "page": page,
        "page_end": page_end,
        "section": evidence.get("section") or row.get("section_title"),
        "table_or_figure": evidence.get("table_or_figure"),
    }


def add_claim_location(row: dict[str, Any]) -> dict[str, Any]:
    result = {**row, **evidence_location(row)}
    result.pop("chunk_page_start", None)
    result.pop("chunk_page_end", None)
    return result
