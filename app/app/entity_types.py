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
    "Other": "Unresolved type; preserve uncertainty_reason rather than guessing",
}

ENTITY_TYPES = {name.casefold(): name for name in TYPE_DEFINITIONS}
ENTITY_TYPES.update({
    "chemical": "Compound", "chemicalcompound": "Compound", "smallmolecule": "Compound",
    "medication": "Drug", "proteins": "Protein", "genes": "Gene", "celllines": "CellLine",
})
