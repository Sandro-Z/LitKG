"""Measured entity aspects; modification identities remain in entity names."""
import re
from typing import Literal

ProcessKind = Literal[
    'expression',
    'secretion',
    'activity',
    'abundance',
    'concentration',
    'production',
    'degradation',
    'conversion',
    'localization',
    'occupancy',
    'phosphorylation',
    'dephosphorylation',
    'acetylation',
    'deacetylation',
    'methylation',
    'demethylation',
    'ubiquitination',
    'deubiquitination',
    'glycosylation',
    'cleavage',
    'stability',
    'turnover',
    'solubility',
    'aggregation',
]

PROCESS_DEFINITIONS = {
    'expression': 'Expression of the attached gene/RNA/protein; not inferred from peptide intensity alone',
    'secretion': 'Secretion of the attached entity',
    'activity': 'Functional or enzymatic activity; not equivalent to abundance or phosphorylation',
    'abundance': 'Measured abundance of the attached protein/peptide/proteoform; retain relative/absolute and quantification method',
    'concentration': 'Concentration with reported sample context and units',
    'production': 'Production/synthesis of the attached entity',
    'degradation': 'Degradation of the attached entity',
    'conversion': 'Conversion of the attached substrate; product is a named participant',
    'localization': 'Localization/distribution of the attached entity; location is a named participant',
    'occupancy': 'Fractional occupancy of a stated modification site; not raw modified-peptide intensity',
    'phosphorylation': 'Phosphorylation level of the attached substrate; not total protein abundance',
    'dephosphorylation': 'Dephosphorylation of the attached substrate',
    'acetylation': 'Acetylation level of the attached substrate',
    'deacetylation': 'Deacetylation of the attached substrate',
    'methylation': 'Methylation level of the attached substrate',
    'demethylation': 'Demethylation of the attached substrate',
    'ubiquitination': 'Ubiquitination level of the attached substrate; retain linkage/site if stated',
    'deubiquitination': 'Deubiquitination of the attached substrate',
    'glycosylation': 'Glycosylation of the attached substrate; retain glycan/site if stated',
    'cleavage': 'Proteolytic cleavage of the attached substrate; products remain participants',
    'stability': 'Stability of the attached protein or proteoform',
    'turnover': 'Turnover of the attached entity; retain synthesis/degradation context and half-life',
    'solubility': 'Solubility of the attached entity',
    'aggregation': 'Aggregation of the attached entity',
}

PROCESS_PATTERN = re.compile(r"\b(?:" + "|".join(PROCESS_DEFINITIONS) + r")\b", re.I)
PROCESS_CONTEXT_WORDS = set(PROCESS_DEFINITIONS) | {
    "the", "a", "an", "of", "level", "levels", "protein", "gene", "mrna",
    "serum", "plasma", "concentrations", "increased", "decreased", "unchanged",
    "high", "low", "amount", "amounts", "proportion", "percentage", "relative", "absolute",
}
