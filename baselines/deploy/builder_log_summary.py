"""Summarise a saved builder log: finished extractions and why the others failed.

The builder's /extract answers only "Failed To Process File ... or LLM Unable To Parse Content"; the
cause is in its own log ("File Failed in extraction: <cause>"). scripts/baseline_neo4j_builder.sh
saves that log (secrets already redacted) before the container is recreated and runs this on it.

    python builder_log_summary.py <builder-backend.*.log[.gz]>
"""
import gzip
import re
import statistics
import sys
from collections import Counter

FAILED = re.compile(r'File Failed in extraction: (.*)')
DONE = re.compile(r'extraction completed in ([0-9.]+) seconds for file name (\S+)')


def summarise(lines):
    causes, durations = Counter(), []
    for line in lines:
        match = FAILED.search(line)
        if match:
            # Token counts and ids differ per chunk; the cause before them is what matters.
            cause = re.sub(r'\s+', ' ', match.group(1)).strip()
            causes[re.split(r' - CompletionUsage\(|, request_id=', cause)[0][:240]] += 1
            continue
        match = DONE.search(line)
        if match and not match.group(2).startswith('kgb-end-to-end-test'):
            durations.append(float(match.group(1)))
    return causes, durations


def report(path, causes, durations):
    out = [f'builder log of the previous run, saved to {path}:']
    if durations:
        out.append(f'  finished PDFs: {len(durations)} (median {statistics.median(durations) / 60:.0f} min, '
                   f'longest {max(durations) / 60:.0f} min)')
    if causes:
        out.append(f'  failed extractions: {sum(causes.values())}; causes, most frequent first:')
        out += [f'    {count:3d} x {cause}' for cause, count in causes.most_common(6)]
    if not durations and not causes:
        out.append('  no finished or failed extractions in it')
    return '\n'.join(out)


if __name__ == '__main__':
    path = sys.argv[1]
    opener = gzip.open if path.endswith('.gz') else open
    with opener(path, 'rt', errors='replace') as handle:
        print(report(path, *summarise(handle)))
