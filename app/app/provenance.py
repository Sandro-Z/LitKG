import re
from typing import Any


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
    if None in starts or None in ends:
        return None, None
    return min(starts), max(ends)


def locate_evidence_pages(
    sentence: str,
    chunk: dict[str, Any],
) -> tuple[int | None, int | None]:
    """Locate an evidence sentence using parser offsets, never LLM page guesses."""
    spans = chunk.get("page_spans") or []
    tokens = sentence.split()
    if tokens and spans:
        pattern = r"\s+".join(re.escape(token) for token in tokens)
        matches = list(re.finditer(pattern, chunk["text"], flags=re.IGNORECASE))
        if matches:
            covered = [
                span
                for span in spans
                if any(
                    span["start"] < match.end() and span["end"] > match.start()
                    for match in matches
                )
            ]
            return page_bounds(covered)

    # If the sentence cannot be matched, retain the whole chunk's known range.
    return (
        _valid_page(chunk.get("page_start")),
        _valid_page(chunk.get("page_end")),
    )


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
