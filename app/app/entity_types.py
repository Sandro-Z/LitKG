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
    "Peptide": "Explicit peptide sequence or named peptide, including modified peptidoforms; preserve sequence, termini and modifications; not its inferred parent protein",
    "Proteoform": "Explicit molecular form of a protein with stated sequence variant, splice isoform or modification pattern; preserve all known identity details without inventing unreported sites",
    "ProteinGroup": "Study-reported protein inference group whose members cannot be distinguished by the evidence; not a physical complex or single resolved protein",
    "ProteinFamily": "Named family/class of related proteins; not an individual protein or physical complex",
    "ProteinDomain": "Named protein domain or region; preserve parent protein and residue coordinates when given",
    "ProteinModification": "Named protein/peptide chemical modification; biological PTM versus sample-preparation artifact only when stated; not a modified protein itself",
    "ModificationSite": "Explicit protein-specific residue/site with parent and numbering context in its name; bare S473/K48 labels belong in qualifiers, not standalone entities",
    "Proteome": "Explicit collective protein repertoire of a cell, tissue, organism or fraction; not a single protein or inference group",
    "SubcellularLocation": "Named organelle or subcellular compartment; not a tissue, sample or assay",
    "Metabolite": "Chemical metabolite; retain chemical identity and distinguish it from a protein or peptide",
    "Intervention": "Named non-drug intervention such as genetic perturbation, knockdown or treatment procedure; drug molecules remain Drug",
    "Biospecimen": "Collected biological material such as plasma, serum, biopsy, lysate or subcellular fraction; not its anatomical source",
    "StudyGroup": "Explicit cohort, treatment arm or comparison group; not an individual organism or protein inference group",
    "ExperimentalModel": "Named experimental model such as a knockout mouse or organoid; use Organism for an unqualified species",
    "ClinicalOutcome": "Named clinical endpoint or response such as survival, remission or treatment response; not the disease itself",
    "Other": "Unresolved type; preserve uncertainty_reason rather than guessing",
}

ENTITY_TYPES = {name.casefold(): name for name in TYPE_DEFINITIONS}
ENTITY_TYPES.update({
    "chemical": "Compound", "chemicalcompound": "Compound", "smallmolecule": "Compound",
    "medication": "Drug", "proteins": "Protein", "genes": "Gene", "celllines": "CellLine",
    "peptides": "Peptide", "peptidoform": "Peptide", "modifiedpeptide": "Peptide",
    "proteinisoform": "Proteoform", "proteoforms": "Proteoform",
    "proteininferencegroup": "ProteinGroup", "domain": "ProteinDomain",
    "ptm": "ProteinModification", "posttranslationalmodification": "ProteinModification",
    "phosphosite": "ModificationSite", "organelle": "SubcellularLocation",
    "cellularcompartment": "SubcellularLocation",
    "sample": "Biospecimen", "specimen": "Biospecimen", "cohort": "StudyGroup",
    "treatmentarm": "StudyGroup", "perturbation": "Intervention",
})

# Endpoint compatibility only; these parents never merge entity identities.
TYPE_PARENTS = {
    "Proteoform": "Protein", "Metabolite": "Compound", "Drug": "Compound",
    "ClinicalOutcome": "Phenotype",
}


def type_is_allowed(entity_type, allowed):
    if "*" in allowed:
        return True
    while entity_type:
        if entity_type in allowed:
            return True
        entity_type = TYPE_PARENTS.get(entity_type)
    return False
