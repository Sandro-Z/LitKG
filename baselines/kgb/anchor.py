"""Evidence anchoring: a quote must occur exactly once in a block and satisfy text[start:end] == quote.

Order: exact match, then whitespace-normalised regex (the stored quote is the
verbatim matched block text), then choice among blocks (LitKG chunk blocks
before QC-attached original pages; within a tier the hinted block, else the
only block with a unique hit).  Failure or ambiguity returns ``None`` with a
reason; quotes are never generated or rewritten by a model here.
"""
import re

from .text_prep import chunk_blocks, original_blocks


def _hits(text, quote):
    exact = []
    i = text.find(quote)
    while i >= 0:
        exact.append((i, i + len(quote)))
        if len(exact) > 1:
            break
        i = text.find(quote, i + 1)
    if exact:
        return exact, 'exact'
    words = quote.split()
    if not words:
        return [], None
    pattern = re.compile(r'\s+'.join(re.escape(w) for w in words))
    found = []
    for m in pattern.finditer(text):
        found.append(m.span())
        if len(found) > 1:
            break
    return found, 'whitespace'


def anchor(doc, quote, hint_block=None, role='supports'):
    """Return (source_ref | None, reason)."""
    if not isinstance(quote, str) or not quote.strip():
        return None, 'empty_quote'
    ambiguous = False
    for tier in (chunk_blocks(doc), original_blocks(doc)):
        unique = []
        for block in tier:
            spans, how = _hits(block['text'], quote)
            if len(spans) == 1:
                unique.append((block, spans[0], how))
            elif len(spans) > 1:
                ambiguous = True
        chosen = None
        if hint_block is not None:
            chosen = next((u for u in unique if str(u[0].get('id')) == str(hint_block)), None)
        if chosen is None and len(unique) == 1:
            chosen = unique[0]
        if chosen is None and len(unique) > 1:
            ambiguous = True
            continue
        if chosen:
            block, (start, end), how = chosen
            text = block['text'][start:end]
            return dict(doc_id=str(doc['id']), block_id=str(block['id']), start=start, end=end,
                        quote=text, role=role, match=how), None
    return None, 'ambiguous' if ambiguous else 'not_found'


def verify(ref, doc):
    block = next((b for b in doc.get('blocks', []) if str(b.get('id')) == str(ref.get('block_id'))), None)
    return bool(block) and isinstance(ref.get('start'), int) and isinstance(ref.get('end'), int) \
        and block['text'][ref['start']:ref['end']] == ref.get('quote')
