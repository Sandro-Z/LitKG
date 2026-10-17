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
    "MicrobialTaxon": "Microbial taxon at any stated rank: bacteria, archaea, fungi, protists or viruses; preserve rank and taxon name; not a community",
    "MicrobialStrain": "Named microbial strain or isolate; retain strain/accession identifiers; distinct from its species",
    "MicrobialCommunity": "Microbial assemblage, microbiota, virome, mycobiome or defined consortium; not an individual taxon",
    "Microbiome": "Microbial community together with its collective genetic/functional repertoire and habitat, when explicitly described as a microbiome",
    "SequenceFeature": "Study-defined ASV, OTU or sequence feature; not automatically equivalent to a named taxon or strain",
    "Genome": "Genome, metagenome-assembled genome (MAG) or genomic collection; not the organism itself or a single gene",
    "Metabolite": "Chemical metabolite such as a short-chain fatty acid, bile acid or indole derivative; preserve chemical identity",
    "Diet": "Named dietary pattern or feeding regimen; distinct from an individual dietary ingredient",
    "DietaryComponent": "Named dietary ingredient, nutrient, fiber or substrate; use Compound/Metabolite for a specific chemical identity",
    "Intervention": "Named non-drug intervention: FMT, probiotic/prebiotic/synbiotic treatment or procedure; a probiotic strain itself is MicrobialStrain",
    "Biospecimen": "Collected biological material such as stool, intestinal contents, biopsy or serum sample; not its anatomical source",
    "StudyGroup": "Explicit cohort, donor/recipient group, treatment arm or comparison group; not an individual organism",
    "ExperimentalModel": "Named experimental model such as germ-free mice, an organoid or a gut simulator; use Organism for an unqualified host species",
    "Environment": "Named habitat or environmental setting; use AnatomicalStructure/Tissue for a host anatomical site",
    "ClinicalOutcome": "Named clinical endpoint or response, such as remission, recurrence or survival; not the disease itself",
    "MicrobialFunction": "Named microbial functional capacity or gene function; use Pathway for a named pathway and BiologicalProcess for a process",
    "DiversityMetric": "Named ecological diversity/richness/evenness measure such as Shannon index or Bray-Curtis dissimilarity; values belong in qualifiers",
    "Other": "Unresolved type; preserve uncertainty_reason rather than guessing",
}

ENTITY_TYPES = {name.casefold(): name for name in TYPE_DEFINITIONS}
ENTITY_TYPES.update({
    "chemical": "Compound", "chemicalcompound": "Compound", "smallmolecule": "Compound",
    "medication": "Drug", "proteins": "Protein", "genes": "Gene", "celllines": "CellLine",
    "taxon": "MicrobialTaxon", "microorganism": "MicrobialTaxon", "microbe": "MicrobialTaxon",
    "bacterium": "MicrobialTaxon", "bacteria": "MicrobialTaxon", "archaea": "MicrobialTaxon",
    "fungus": "MicrobialTaxon", "fungi": "MicrobialTaxon", "virus": "MicrobialTaxon",
    "bacteriophage": "MicrobialTaxon", "strain": "MicrobialStrain", "isolate": "MicrobialStrain",
    "microbiota": "MicrobialCommunity", "consortium": "MicrobialCommunity",
    "virome": "MicrobialCommunity", "mycobiome": "MicrobialCommunity",
    "asv": "SequenceFeature", "otu": "SequenceFeature", "mag": "Genome",
    "sample": "Biospecimen", "specimen": "Biospecimen", "cohort": "StudyGroup",
    "treatmentarm": "StudyGroup", "dietaryintervention": "Intervention",
})

# Used only for predicate endpoint constraints. These are not identity merges:
# a strain, taxon, genome and community must remain separate graph entities.
TYPE_PARENTS = {
    "MicrobialTaxon": "Organism", "MicrobialStrain": "MicrobialTaxon",
    "Metabolite": "Compound", "Drug": "Compound", "ClinicalOutcome": "Phenotype",
    "DiversityMetric": "Phenotype", "MicrobialFunction": "BiologicalProcess",
}


def type_is_allowed(entity_type, allowed):
    if "*" in allowed:
        return True
    while entity_type:
        if entity_type in allowed:
            return True
        entity_type = TYPE_PARENTS.get(entity_type)
    return False
