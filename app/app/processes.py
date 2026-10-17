"""Shared aspects of an entity; direction and context never form new predicates."""
import re
from typing import Literal

ProcessKind = Literal[
    "expression", "secretion", "activity", "abundance", "prevalence", "growth",
    "colonization", "production", "degradation", "conversion", "concentration", "diversity",
]

PROCESS_DEFINITIONS = {
    "expression": "Gene/RNA/protein expression",
    "secretion": "Secretion of the attached entity",
    "activity": "Activity of the attached molecule, pathway or process",
    "abundance": "Abundance of the attached taxon, strain, community or molecule; specify relative/absolute in qualifiers when reported",
    "prevalence": "Occurrence frequency of the attached entity across samples or hosts; not within-sample abundance",
    "growth": "Growth of the attached organism or community",
    "colonization": "Establishment/persistence of the attached microbe in a host or habitat",
    "production": "Production of the attached entity, e.g. butyrate production",
    "degradation": "Degradation of the attached entity/substrate",
    "conversion": "Conversion of the attached substrate; record product as a participant",
    "concentration": "Concentration of the attached substance; preserve sample context and units",
    "diversity": "Diversity of the attached community/microbiome; record the metric in qualifiers",
}

PROCESS_PATTERN = re.compile(r"\b(?:" + "|".join(PROCESS_DEFINITIONS) + r")\b", re.I)
PROCESS_CONTEXT_WORDS = set(PROCESS_DEFINITIONS) | {
    "the", "a", "an", "of", "level", "levels", "protein", "gene", "mrna",
    "serum", "plasma", "concentrations", "increased", "decreased", "unchanged",
    "high", "low", "amount", "amounts", "proportion", "percentage", "relative", "absolute",
}
