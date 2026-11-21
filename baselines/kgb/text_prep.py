"""Per-document input text shared by every system ("same-text" variant).

LitKG chunk blocks are concatenated in order after removing the overlap
between neighbouring chunks.  Documents without chunk blocks fall back to the
QC-attached original pages (``source_origin`` = ``litkg_original`` /
``uploaded_original``).  Every character of the input text maps back to
``(block_id, offset in block)``; the separators between blocks map to nothing.
"""
import re
from bisect import bisect_right
from dataclasses import dataclass, field

ORIGINAL_ORIGINS = {'litkg_original', 'uploaded_original'}
SEPARATOR = '\n\n'
MIN_OVERLAP = 20
MAX_OVERLAP = 4000


def is_visual(block):
    """LitKG VLM image-page chunks describe images; they are not source prose."""
    return any(isinstance(s, dict) and isinstance(s.get('image_source'), dict) for s in block.get('page_spans') or [])


def chunk_blocks(doc):
    return [b for b in doc.get('blocks', []) if b.get('source_origin') not in ORIGINAL_ORIGINS and not is_visual(b)
            and isinstance(b.get('text'), str)]


def original_blocks(doc):
    return [b for b in doc.get('blocks', []) if b.get('source_origin') in ORIGINAL_ORIGINS and isinstance(b.get('text'), str)]


def overlap(prev, nxt, minimum=MIN_OVERLAP, maximum=MAX_OVERLAP):
    """Longest k with prev[-k:] == nxt[:k] (k >= minimum), else 0."""
    upper = min(len(prev), len(nxt), maximum)
    for k in range(upper, minimum - 1, -1):
        if prev.endswith(nxt[:k]):
            return k
    return 0


@dataclass
class DocText:
    doc_id: str
    text: str
    segments: list = field(default_factory=list)   # (doc_start, doc_end, block_id, block_start)
    source: str = 'chunks'
    overlaps_removed: list = field(default_factory=list)

    def locate(self, start, end):
        """Map a [start, end) span of the input text to (block_id, block_start, block_end) or None."""
        starts = [s[0] for s in self.segments]
        i = bisect_right(starts, start) - 1
        if i < 0:
            return None
        doc_start, doc_end, block_id, block_start = self.segments[i]
        if not (doc_start <= start < end <= doc_end):
            return None
        return block_id, block_start + start - doc_start, block_start + end - doc_start

    def block_at(self, position):
        starts = [s[0] for s in self.segments]
        i = bisect_right(starts, position) - 1
        if i >= 0 and self.segments[i][0] <= position < self.segments[i][1]:
            return self.segments[i][2]
        return None


def build(doc):
    blocks, source = chunk_blocks(doc), 'chunks'
    if not any(b['text'].strip() for b in blocks):
        blocks, source = original_blocks(doc), 'original'
    parts, segments, removed = [], [], []
    position, previous = 0, None
    for block in blocks:
        text = block['text']
        skip = overlap(previous, text) if previous is not None else 0
        if skip:
            removed.append(dict(block_id=str(block.get('id')), chars=skip))
        piece = text[skip:]
        if not piece:
            previous = text
            continue
        if parts:
            parts.append(SEPARATOR)
            position += len(SEPARATOR)
        segments.append((position, position + len(piece), str(block.get('id')), skip))
        parts.append(piece)
        position += len(piece)
        previous = text
    return DocText(str(doc['id']), ''.join(parts), segments, source, removed)


_SENTENCE = re.compile(r'(?<=[.!?])\s+(?=[\(\["“]?[A-Z0-9])|\n{2,}')


def sentences(text):
    """Simple, deterministic sentence spans [(start, end)] (no external tokenizer)."""
    spans, start = [], 0
    for match in _SENTENCE.finditer(text):
        if text[start:match.start()].strip():
            spans.append(_trim(text, start, match.start()))
        start = match.end()
    if text[start:].strip():
        spans.append(_trim(text, start, len(text)))
    return spans


def _trim(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
