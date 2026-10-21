"""Locate ambiguous source regions; corrections require explicit local evidence."""
import re


def inspect_chunk(chunk, *, article_dois=()):
    text = chunk['text']
    issues = []
    patterns = {
        'genotype': r'(?<!\w)(?:1/1|ob/1)(?!\w)',
        'micro_unit': r'\b\d+(?:\.\d+)?\s*(?:mg|mM)\b',
        # Repeated article starts in the extracted body can indicate neighboring
        # papers. This is only a locator, never an automatic article cut.
        'article_boundary': r'(?im)\b(?:abstract\s*[:\n]|research article\b|original article\b|received\s+\d.{0,50}accepted\b)',
    }
    for kind, pattern in patterns.items():
        for match in re.finditer(pattern, text):
            spans = [s for s in chunk.get('page_spans', []) if s['start'] < match.end() and s['end'] > match.start()]
            issues.append({'kind': kind, 'chunk_id': chunk.get('id'), 'start': match.start(), 'end': match.end(),
                'original': match.group(), 'replacement': None, 'basis': None, 'status': 'pending',
                'page': spans[0].get('page_start') if spans else chunk.get('page_start'),
                'page_end': spans[-1].get('page_end') if spans else chunk.get('page_end'),
                'context': text[max(0, match.start()-90):match.end()+90]})
    if len(set(article_dois)) > 1:
        for match in article_doi_matches(text):
            spans = [s for s in chunk.get('page_spans', []) if s['start'] < match.end() and s['end'] > match.start()]
            issues.append({'kind':'article_boundary', 'chunk_id':chunk.get('id'), 'start':match.start(), 'end':match.end(),
                'original':match.group(), 'replacement':None, 'basis':None, 'status':'pending',
                'page':spans[0].get('page_start') if spans else chunk.get('page_start'),
                'page_end':spans[-1].get('page_end') if spans else chunk.get('page_end'),
                'reason':'Multiple explicit article DOI markers; inspect title, columns and regions before assigning this file to one article',
                'article_doi_candidates':sorted(set(article_dois))})
    return issues


def article_doi_matches(text):
    # Explicit DOI markers are locators, not proof of neighboring papers:
    # references may also contain them. Never cut or reassign text automatically.
    return re.finditer(r'\bdoi\s*:\s*10\.\d{4,9}/[^\s<>]+', text, re.I)


def inspect_document(chunks):
    from app.doi import normalize_doi
    dois = {normalize_doi(re.sub(r'^doi\s*:\s*', '', m.group(), flags=re.I))
            for c in chunks for m in article_doi_matches(c['text'])}
    return [issue for c in chunks for issue in inspect_chunk(c, article_dois=dois - {None})]


def correct_chunk(chunk, corrections):
    """Apply reviewed, nonoverlapping offsets; keep page regions aligned.

    An empty replacement excludes an adjacent article region. No wildcard or
    global replace is accepted, including for 1/1, mg and mM.
    """
    text, spans = chunk['text'], [dict(s) for s in chunk.get('page_spans', [])]
    end_of_previous = 0
    for edit in sorted(corrections, key=lambda e: e['start']):
        start, end = edit['start'], edit['end']
        if not 0 <= start < end <= len(text) or start < end_of_previous:
            raise ValueError('Invalid or overlapping source correction regions')
        if text[start:end] != edit['original']:
            raise ValueError('Source correction does not match original text')
        if not isinstance(edit.get('replacement'), str) or not str(edit.get('basis') or '').strip():
            raise ValueError('Each correction needs a replacement and evidence basis')
        if not edit.get('page') and not edit.get('region'):
            raise ValueError('Each correction needs a page or region locator')
        end_of_previous = end
    for edit in sorted(corrections, key=lambda e: e['start'], reverse=True):
        start, end, replacement = edit['start'], edit['end'], edit['replacement']
        delta = len(replacement) - (end-start)
        mapped = []
        for span in spans:
            left, right = span['start'], span['end']
            if right <= start:
                mapped.append(span)
            elif left >= end:
                mapped.append({**span, 'start': left+delta, 'end': right+delta})
            else:
                # A correction cannot invent a precise page inside an ambiguous
                # multi-page span; retain that range and its original locator.
                new_left = min(left, start)
                new_right = max(start+len(replacement), right+delta)
                if new_left < new_right:
                    mapped.append({**span, 'start': new_left, 'end': new_right})
        spans = mapped
        text = text[:start] + replacement + text[end:]
    return {**chunk, 'text': text, 'page_spans': spans}
