"""Shared extraction, validation, query and UI vocabulary (served by /entity-types)."""

TYPE_DEFINITIONS = {
    "Compound": "Chemical substance; preserve stereochemistry and local labels",
    "Drug": "A therapeutic chemical or biologic",
    "Protein": "Protein, including identity-bearing isoforms and modifications",
    "Gene": "Gene or genomic locus; distinct from its protein product",
    "Disease": "Disease or disorder",
    "CellLine": "Named experimental cell line",
    "CellType": "Biological cell type, distinct from a cultured cell line",
    "Tissue": "Biological tissue",
    "AnatomicalStructure": "Organ or anatomical structure",
    "RNA": "RNA molecule or RNA class",
    "MolecularComplex": "Assembly of multiple molecular components",
    "BiologicalProcess": "Biological event or process, distinct from a molecule",
    "Phenotype": "Observed trait or phenotype",
    "Organism": "Species or organism",
    "Assay": "Measurement method or assay",
    "Pathway": "Named biological pathway",
    "Mutation": "Explicit sequence variant or mutation",
    "Intervention": "Named non-drug intervention or treatment procedure; drug molecules remain Drug",
    "Biospecimen": "Collected biological material or sample; not its anatomical source",
    "StudyGroup": "Explicit cohort, treatment arm or comparison group; not an individual organism",
    "Other": "Unresolved type; preserve uncertainty_reason rather than guessing",
}

ENTITY_TYPES = {name.casefold(): name for name in TYPE_DEFINITIONS}
ENTITY_TYPES.update({
    "chemical": "Compound",
    "chemicalcompound": "Compound",
    "smallmolecule": "Compound",
    "medication": "Drug",
    "proteins": "Protein",
    "genes": "Gene",
    "celllines": "CellLine",
    "sample": "Biospecimen",
    "specimen": "Biospecimen",
    "cohort": "StudyGroup",
    "treatmentarm": "StudyGroup",
    "perturbation": "Intervention",
})

# Endpoint constraints never merge entity identities.
TYPE_PARENTS = {
    "Drug": "Compound",
}


def type_is_allowed(entity_type, allowed):
    if "*" in allowed:
        return True
    while entity_type:
        if entity_type in allowed:
            return True
        entity_type = TYPE_PARENTS.get(entity_type)
    return False
