"""Shared aspects of an entity; direction and context never form new predicates."""
import re
from typing import Literal

ProcessKind = Literal[
    "expression", "secretion", "activity", "abundance", "prevalence", "growth",
    "production", "degradation", "conversion", "concentration",
]

PROCESS_DEFINITIONS = {
    "expression": "Gene/RNA/protein expression",
    "secretion": "Secretion of the attached entity",
    "activity": "Activity of the attached molecule, pathway or process",
    "abundance": "Abundance of the attached entity; for example a protein or cell type; specify relative/absolute in qualifiers when reported",
    "prevalence": "Occurrence frequency of the attached entity across samples; not within-sample abundance",
    "growth": "Growth of the attached organism or cell population",
    "production": "Production of the attached entity, e.g. protein production",
    "degradation": "Degradation of the attached entity/substrate",
    "conversion": "Conversion of the attached substrate; record product as a participant",
    "concentration": "Concentration of the attached substance; preserve sample context and units",
}

PROCESS_PATTERN = re.compile(r"\b(?:" + "|".join(PROCESS_DEFINITIONS) + r")\b", re.I)
PROCESS_CONTEXT_WORDS = set(PROCESS_DEFINITIONS) | {
    "the", "a", "an", "of", "level", "levels", "protein", "gene", "mrna",
    "serum", "plasma", "concentrations", "increased", "decreased", "unchanged",
    "high", "low", "amount", "amounts", "proportion", "percentage", "relative", "absolute",
}
