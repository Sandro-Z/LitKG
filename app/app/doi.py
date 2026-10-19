"""Local DOI recovery from document text, with conservative disambiguation."""

import re
from urllib.parse import unquote


_DOI = re.compile(
    r"(?<![\w/])(?:https?://(?:dx\.)?doi\.org/\s*)?10\.\d{4,9}/[^\s\"“”]+", re.I,
)
_REFERENCE_HEADING = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.)]?\s+)?"
    r"(?:references(?:\s+and\s+notes)?|bibliography|literature\s+cited|参考文献)"
    r"\s*[:：]?\s*$", re.I,
)
_POST_REFERENCE_HEADING = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.)]?\s+)?"
    r"(?:appendi(?:x|ces)\b|(?:electronic\s+)?supplementary\s+(?:material|information|data)\b|"
    r"supporting\s+information\b|(?:additional|author|article)\s+information\b|"
    r"acknowledg(?:e)?ments?\b|declarations\b|附录|补充材料)", re.I,
)
_OWN_ARTICLE = re.compile(
    r"\b(?:cite\s+this\s+(?:article|paper)|(?:article|paper)\s+doi|"
    r"doi\s+(?:of|for)\s+(?:this|the\s+present)\s+(?:article|paper)|"
    r"this\s+(?:article|paper)(?:'s)?\s+doi)\b", re.I,
)
_OTHER_OBJECT = re.compile(
    r"\b(?:datasets?|data\s+(?:availability|accessibility|doi)|"
    r"(?:source|research)\s+data|data\s+(?:are|is|can\s+be)|"
    r"(?:code|software)\s+(?:availability|doi)|repository|zenodo|figshare|dryad)\b",
    re.I,
)
_CITATION = re.compile(
    r"\bet\s+al\.\s*[,]?\s*\(?(?:19|20)\d{2}\b|"
    r"(?:^|\n)\s*(?:\[\d+\]|\d+[.)])\s+[^\n]*\b(?:19|20)\d{2}\b|"
    r"(?:^|\n)\s*[A-Z][\w'’-]+,\s*[A-Z]\.[^\n]*\b(?:19|20)\d{2}\b",
    re.I,
)


def normalize_doi(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.translate(str.maketrans("", "", "\u00ad\u200b\ufeff")).strip()
    value = re.sub(r"^doi\s*:\s*", "", value, flags=re.I)
    if re.match(r"https?://(?:dx\.)?doi\.org/", value, re.I):
        value = re.sub(r"^https?://(?:dx\.)?doi\.org/\s*", "", value, flags=re.I)
        value = unquote(value.split("?", 1)[0].split("#", 1)[0])
    value = value.rstrip(".,;:。；，：'\"’”")
    # Citation punctuation may wrap a DOI; parentheses inside a DOI are valid.
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}"), ("<", ">")):
        while value.endswith(closing) and value.count(closing) > value.count(opening):
            value = value[:-1].rstrip(".,;")
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value, re.I) else None


def _non_reference_blocks(texts):
    """Keep reference state across pages, and resume at appendices/back matter."""
    in_references = False
    for text in texts:
        lines = []
        for line in text.splitlines():
            if _REFERENCE_HEADING.fullmatch(line):
                if lines:
                    yield "\n".join(lines)
                    lines = []
                in_references = True
            elif _POST_REFERENCE_HEADING.match(line):
                in_references = False
                lines.append(line)
            elif not in_references:
                lines.append(line)
        if lines:
            yield "\n".join(lines)


def extract_document_doi(texts) -> str | None:
    """Prefer self-identifying article DOIs; leave competing candidates unset."""
    candidates = {}
    for text in _non_reference_blocks(texts):
        text = text.translate(str.maketrans("", "", "\u00ad\u200b\ufeff"))
        # Repair only unambiguous wrap points. In particular, preserve a DOI's
        # real hyphens instead of applying prose dehyphenation to its suffix.
        text = re.sub(r"(10\.\d{4,9}/)\s*\n\s*", r"\1", text)
        text = re.sub(r"(10\.\d{4,9}/[^\s<>\"]*[-/])\s*\n\s*(?=\w)", r"\1", text)
        previous_end = 0
        for match in _DOI.finditer(text):
            context = text[max(previous_end, match.start() - 240):match.start()]
            # Keep paragraph boundaries so a dataset paragraph does not taint
            # an otherwise independent article identifier in a footer.
            context = context.rsplit("\n\n", 1)[-1]
            own = list(_OWN_ARTICLE.finditer(context))
            other = list(_OTHER_OBJECT.finditer(context))
            previous_end = match.end()
            if _CITATION.search(context) and not own:
                continue
            if other and (not own or max(m.start() for m in other) > own[-1].start()):
                continue
            doi = normalize_doi(match.group())
            if doi is None:
                continue
            rank = 1 if own else 0
            key = doi.casefold()
            if key not in candidates or rank > candidates[key][0]:
                candidates[key] = (rank, doi)
    if not candidates:
        return None
    best_rank = max(rank for rank, _ in candidates.values())
    best = [doi for rank, doi in candidates.values() if rank == best_rank]
    return best[0] if len(best) == 1 else None
