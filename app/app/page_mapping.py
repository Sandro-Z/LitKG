"""Conservative alignment of parsed text to the local PDF's physical pages."""

import re

from app.provenance import normalized_text_with_offsets, page_bounds


class PdfPageIndex:
    def __init__(self, pages: list[tuple[int, str]]):
        raw_text = "\n".join(text for _, text in pages)
        raw_pages = []
        for page, text in pages:
            raw_pages.extend([page] * (len(text) + 1))
        self.text, offsets = normalized_text_with_offsets(raw_text)
        self.pages = [raw_pages[offset] for offset in offsets]

    @classmethod
    def from_sections(cls, sections):
        return cls([
            (section["page_spans"][0]["page_start"], section["text"])
            for section in sections if section.get("page_spans")
        ])

    def locate(self, text: str, within=None) -> tuple[int | None, int | None]:
        needle, _ = normalized_text_with_offsets(text)
        if not needle:
            return None, None
        found_pages = []
        start = self.text.find(needle)
        while start >= 0:
            first, last = self.pages[start], self.pages[start + len(needle) - 1]
            if within is None or within[0] <= first <= last <= within[1]:
                found_pages.extend((first, last))
            start = self.text.find(needle, start + 1)
        # Repeated text stays a range instead of picking an arbitrary occurrence.
        return (min(found_pages), max(found_pages)) if found_pages else (None, None)

    def page_spans(self, text: str, existing_spans=None):
        """Refine unknown/multi-page spans without changing the parsed text."""
        existing_spans = existing_spans or []
        # Include uncovered non-whitespace text so partial mappings cannot make
        # an otherwise unknown chunk appear to have a fully known page range.
        spans = []
        offset = 0
        for span in sorted(existing_spans, key=lambda value: value["start"]):
            if span["start"] > offset:
                spans.append({"start": offset, "end": span["start"],
                              "page_start": None, "page_end": None})
            spans.append(span)
            offset = span["end"]
        if offset < len(text):
            spans.append({"start": offset, "end": len(text),
                          "page_start": None, "page_end": None})
        result = []
        for span in spans:
            start, end = span["start"], span["end"]
            original = page_bounds([span])
            if original[0] is not None and original[0] == original[1]:
                result.append(dict(span))
                continue
            # Sentence-level GROBID spans normally take this path only when
            # coordinates are absent. Also handle older unsegmented paragraphs.
            boundaries = [start] + [start + match.end() for match in re.finditer(
                r"(?<=[.!?。！？])\s+|\n+", text[start:end],
            )] + [end]
            for left, right in zip(boundaries, boundaries[1:]):
                unit = text[left:right]
                content = unit.strip()
                if not content:
                    continue
                left += len(unit) - len(unit.lstrip())
                right = left + len(content)
                pages = self.locate(content, within=original if original[0] else None)
                if pages[0] is None:
                    pages = original
                result.append({"start": left, "end": right,
                               "page_start": pages[0], "page_end": pages[1]})
        return result
