"""Local controlled predicates, shared by prompts, validation and the API.

Inverse names describe semantics only: they never generate extra assertions.
These are project predicates, not asserted one-to-one Biolink mappings.
"""
import re

from app.entity_types import type_is_allowed

REGISTRY_VERSION = "gut-microbiome-v1"
EVIDENCE_CATEGORIES = {
    "experimental": "Direct experimental measurement or perturbation",
    "observational": "Non-interventional clinical, cohort or sample observation",
    "computational_prediction": "Inferred function, genome annotation or predictive model; not experimental verification",
    "cooccurrence": "Co-occurrence/network association; not an ecological mechanism",
    "author_statement": "Explicit literature assertion without a reported evidence design in this excerpt",
    "not_reported": "Evidence design unavailable; preserve uncertainty and request review, never guess",
}
QUALIFIER_DEFINITIONS = {
    "evidence_type": "One EVIDENCE_CATEGORIES key; omit if not determinable from the excerpt",
    "knowledge_level": "observed | predicted | asserted; computational_prediction requires predicted",
    "association_direction": "positive | negative | unspecified; never a causal increase/decrease",
    "comparison_group": "Explicit focal group label, also a StudyGroup participant with role comparison_group",
    "reference_group": "Explicit reference group label, also a StudyGroup participant with role reference_group",
    "metric": "Measured aspect or named metric; e.g. relative abundance, prevalence or Shannon index",
    "direction": "increased | decreased | unchanged | unspecified; focal group relative to reference group",
    "object_direction": "Legacy directional verb with unknown object aspect; preserve direction without inventing a process",
    "object.process": "Attach kind/direction to the affected object; projected as qualifiers.processes with role=object",
    "substrate": "Named substrate participant; do not encode it in the predicate",
    "product": "Named product participant; do not encode it in the predicate",
    "mediator": "Named cross-fed metabolite/substrate participant",
    "prediction_target": "Outcome being predicted, including horizon if reported; performance is not causality",
}

ANY = ("*",)
MICROBES = ("MicrobialTaxon", "MicrobialCommunity", "Microbiome", "SequenceFeature")
BIOLOGICAL_AGENTS = (*MICROBES, "Organism", "CellType", "CellLine", "Tissue", "Protein", "MolecularComplex", "Pathway", "BiologicalProcess")
CHEMICALS = ("Compound", "DietaryComponent", "Protein", "RNA")
FUNCTIONS = ("BiologicalProcess", "Pathway")
HABITATS = ("Organism", "AnatomicalStructure", "Tissue", "Environment", "Biospecimen", "ExperimentalModel")
ALL_EVIDENCE = tuple(EVIDENCE_CATEGORIES)
FUNCTION_EVIDENCE = ("experimental", "computational_prediction", "author_statement", "not_reported")
MECHANISM_EVIDENCE = ("experimental", "author_statement", "not_reported")


def _entry(group, definition, synonyms=(), subjects=ANY, objects=ANY, *,
           symmetric=False, inverse=None, required=(), evidence=ALL_EVIDENCE):
    return dict(group=group, definition=definition, synonyms=list(synonyms),
                subject_types=list(subjects), object_types=list(objects),
                symmetric=symmetric, inverse=inverse, required_qualifiers=list(required),
                evidence_categories=list(evidence))


PREDICATES = {
    "SUBCLASS_OF": _entry("classification", "Subject is a narrower class/taxon of object; not physical membership",
        ("is a", "subtype of"), inverse="SUPERCLASS_OF"),
    "MEMBER_OF": _entry("composition", "Microbe/feature is a member of a community; not taxonomic subclassing",
        ("member of", "community member of"), MICROBES, ("MicrobialCommunity", "Microbiome"), inverse="HAS_MEMBER"),
    "PART_OF": _entry("composition", "Subject is a physical/structural component of object; not taxonomic inclusion",
        ("part of", "component of"), inverse="HAS_PART"),
    "ASSOCIATED_WITH": _entry("association", "Statistical or observed association without a causal mechanism",
        ("associated with", "linked to", "co-occurs with"), symmetric=True, inverse="ASSOCIATED_WITH"),
    "CORRELATED_WITH": _entry("association", "Statistical correlation; sign belongs in association_direction",
        ("correlated with", "correlates with", "positively correlated with", "negatively correlated with"), symmetric=True, inverse="CORRELATED_WITH",
        required=("association_direction",)),
    "DIFFERS_BETWEEN": _entry("comparison", "Subject metric differs in focal object StudyGroup relative to reference_group",
        ("differs between",), objects=("StudyGroup",),
        required=("comparison_group", "reference_group", "metric", "direction"),
        evidence=("experimental", "observational", "author_statement", "not_reported")),
    "PRODUCES": _entry("metabolism", "Subject produces object; predicted capacity must stay predicted",
        ("produces", "synthesizes", "generates"), BIOLOGICAL_AGENTS, CHEMICALS, inverse="PRODUCED_BY", evidence=FUNCTION_EVIDENCE),
    "CONSUMES": _entry("metabolism", "Subject consumes/uses object substrate",
        ("consumes", "utilizes", "uses as substrate"), BIOLOGICAL_AGENTS, CHEMICALS, inverse="CONSUMED_BY", evidence=FUNCTION_EVIDENCE),
    "TRANSFORMS": _entry("metabolism", "Subject converts object substrate to a product participant",
        ("transforms", "converts", "metabolizes"), BIOLOGICAL_AGENTS, CHEMICALS, inverse="TRANSFORMED_BY",
        required=("product",), evidence=FUNCTION_EVIDENCE),
    "ENCODES": _entry("function", "Gene/genome encodes object product or function; not proof of expression",
        ("encodes", "codes for"), ("Gene", "Genome"), ("Protein", "RNA", *FUNCTIONS),
        inverse="ENCODED_BY", evidence=FUNCTION_EVIDENCE),
    "CATALYZES": _entry("function", "Protein/complex catalyzes a reaction/process; annotation is prediction",
        ("catalyzes", "catalyses"), ("Protein", "MolecularComplex"), FUNCTIONS,
        inverse="CATALYZED_BY", evidence=FUNCTION_EVIDENCE),
    "AFFECTS": _entry("intervention", "Subject changes an aspect of object; aspect/direction belong in object.process",
        ("affects", "modulates", "regulates"), required=("object.process",), evidence=MECHANISM_EVIDENCE),
    "ACTIVATES": _entry("regulation", "Explicit activation mechanism; not positive correlation or higher abundance",
        ("activates",), objects=("Protein", "Gene", "RNA", "MolecularComplex", *FUNCTIONS),
        inverse="ACTIVATED_BY", required=("object.process",), evidence=MECHANISM_EVIDENCE),
    "INHIBITS": _entry("regulation", "Explicit inhibition mechanism; abundance changes use AFFECTS",
        ("inhibits",), inverse="INHIBITED_BY", required=("object.process",), evidence=MECHANISM_EVIDENCE),
    "COLONIZES": _entry("ecology", "Subject establishes/persists in a host or habitat; not detection alone",
        ("colonizes", "colonises", "engrafts in"), MICROBES, HABITATS,
        inverse="COLONIZED_BY", evidence=MECHANISM_EVIDENCE),
    "CROSS_FEEDS": _entry("ecology", "Subject supplies a metabolite/substrate used by object microbe",
        ("cross-feeds", "cross feeds"), MICROBES, MICROBES,
        required=("mediator",), evidence=MECHANISM_EVIDENCE),
    "COMPETES_WITH": _entry("ecology", "Explicit ecological competition; co-occurrence cannot establish it",
        ("competes with",), MICROBES, MICROBES, symmetric=True, inverse="COMPETES_WITH", evidence=MECHANISM_EVIDENCE),
    "INFECTS": _entry("ecology", "Pathogen/phage infects object host; colonization alone is insufficient",
        ("infects",), ("MicrobialTaxon",), ("Organism", "CellType", "CellLine", "ExperimentalModel"),
        inverse="INFECTED_BY", evidence=MECHANISM_EVIDENCE),
    "PREDICTS": _entry("prediction", "Subject predicts object outcome; performance does not imply causality",
        ("predicts", "predictive of"), required=("prediction_target",),
        evidence=("computational_prediction", "observational", "experimental", "author_statement", "not_reported")),
    "MEASURED_BY": _entry("measurement", "Subject entity/aspect is measured by an assay",
        ("measured by", "quantified by", "detected by"), objects=("Assay",), inverse="MEASURES"),
    "SAMPLED_FROM": _entry("measurement", "Biospecimen was collected from a source; not taxon membership",
        ("sampled from", "collected from"), ("Biospecimen",), (*HABITATS, "StudyGroup"), inverse="SOURCE_OF_SAMPLE"),
    # Baseline biomedical vocabulary remains available alongside microbiome edges.
    "BINDS_TO": _entry("biomedical", "Direct molecular binding", ("binds to", "binds"),
        CHEMICALS + ("MolecularComplex",), CHEMICALS + ("MolecularComplex",), symmetric=True, inverse="BINDS_TO", evidence=FUNCTION_EVIDENCE),
    "INTERACTS_WITH": _entry("biomedical", "Explicit biological interaction; not co-occurrence",
        ("interacts with",), symmetric=True, inverse="INTERACTS_WITH", evidence=MECHANISM_EVIDENCE),
    "TREATS": _entry("biomedical", "Therapeutic intervention for object disease/phenotype",
        ("treats", "ameliorates"), ("Compound", "Intervention", "Diet", *MICROBES), ("Disease", "Phenotype"), inverse="TREATED_BY", evidence=MECHANISM_EVIDENCE),
    "PREVENTS": _entry("biomedical", "Subject prevents object disease/clinical outcome",
        ("prevents",), objects=("Disease", "Phenotype", "BiologicalProcess"), inverse="PREVENTED_BY", evidence=MECHANISM_EVIDENCE),
    "CAUSES": _entry("biomedical", "Explicit causal claim; never inferred from association/prediction",
        ("causes",), inverse="CAUSED_BY", evidence=MECHANISM_EVIDENCE),
    "TARGETS": _entry("biomedical", "Subject explicitly targets object", ("targets",), inverse="TARGETED_BY"),
    "LOCATED_IN": _entry("biomedical", "Subject is localized to object site; detection does not establish colonization",
        ("located in", "localized in"), objects=HABITATS, inverse="LOCATION_OF"),
    "PARTICIPATES_IN": _entry("biomedical", "Subject participates in a pathway/process",
        ("participates in", "involved in"), objects=FUNCTIONS, inverse="HAS_PARTICIPANT", evidence=FUNCTION_EVIDENCE),
}


def _key(value):
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


PREDICATE_ALIASES = {_key(alias): name for name, spec in PREDICATES.items()
                     for alias in (name, *spec["synonyms"])}
# No automatic inversion: converting "produced by" to PRODUCES would swap endpoints.
# Direction-only legacy verbs carry no claim about the affected aspect or mechanism.
DIRECTION_ALIASES = {
    "increases": "increased", "increased": "increased",
    "increases_abundance_of": "increased", "up_regulates": "increased", "upregulates": "increased",
    "reduces": "decreased", "decreases": "decreased", "decreased": "decreased",
    "decreases_abundance_of": "decreased",
    "down_regulates": "decreased", "downregulates": "decreased",
}


def normalize_registered_claim(claim, repairs):
    """Canonicalize supported expressions without guessing context or evidence.

    Missing metadata is visible as a review reason, preserving extraction with
    existing models. Explicitly incompatible types/evidence are quarantined by
    the caller's existing per-candidate validation/correction path.
    """
    original = claim.predicate
    if any(p.role in {'subject', 'object'} for p in claim.participants):
        raise ValueError('subject/object are reserved endpoint roles, not auxiliary participant roles')
    key = _key(original)
    name = PREDICATE_ALIASES.get(key)
    direction = DIRECTION_ALIASES.get(key)
    if direction:
        name = "AFFECTS"
        if claim.object.process:
            if claim.object.process.direction == "unspecified":
                repairs.append(dict(field="object.process.direction", original="unspecified", replacement=direction))
                claim.object.process.direction = direction
            elif claim.object.process.direction != direction:
                _review(claim, "predicate registry: legacy verb conflicts with object process direction")
        else:
            # Preserve the verb's direction even when its aspect is unknown.
            if "object_direction" not in claim.qualifiers:
                claim.qualifiers["object_direction"] = direction
                repairs.append(dict(field="qualifiers.object_direction", original=None, replacement=direction))
    if key in {"positively_correlated_with", "negatively_correlated_with"}:
        sign = "positive" if key.startswith("positively") else "negative"
        previous = claim.qualifiers.get('association_direction')
        if previous not in (None, "unspecified", sign):
            raise ValueError("Correlation predicate conflicts with association_direction")
        if previous != sign:
            claim.qualifiers['association_direction'] = sign
            repairs.append(dict(field="qualifiers.association_direction", original=previous, replacement=sign))
    if not name:
        raise ValueError(f"Unregistered predicate {original!r}; use the controlled predicate registry")
    claim.predicate = name
    if name != original:
        repairs.append(dict(field="predicate", original=original, replacement=name))
    spec = PREDICATES[name]
    for role in ("subject", "object"):
        entity = getattr(claim, role)
        if entity.type in {None, "Other"}:
            _review(claim, f"predicate registry: unresolved {role} type for {name}")
        elif not type_is_allowed(entity.type, spec[role + "_types"]):
            raise ValueError(f"{name} does not allow {role} type {entity.type}")
    if claim.subject.type not in {None, 'Other'} and claim.object.type not in {None, 'Other'}:
        if name == 'SUBCLASS_OF' and not type_is_allowed(claim.subject.type, [claim.object.type]):
            raise ValueError('SUBCLASS_OF requires compatible class/taxon types, not community membership')
        if name == 'PART_OF' and type_is_allowed(claim.subject.type, ['MicrobialTaxon', 'SequenceFeature']):
            if claim.object.type in {'MicrobialCommunity', 'Microbiome'}:
                raise ValueError('Taxon community membership requires MEMBER_OF, not PART_OF')
            if type_is_allowed(claim.object.type, ['MicrobialTaxon']):
                raise ValueError('Taxonomic inclusion requires SUBCLASS_OF, not PART_OF')
    evidence = claim.qualifiers.get("evidence_type")
    if evidence is None or evidence == "not_reported":
        _review(claim, f"predicate registry: evidence_type not reported for {name}")
    elif evidence not in spec["evidence_categories"]:
        raise ValueError(f"{name} does not accept evidence_type {evidence!r}")
    if evidence == "computational_prediction" and claim.qualifiers.get("knowledge_level") != "predicted":
        raise ValueError("Computational evidence requires qualifiers.knowledge_level=predicted")
    for required in spec["required_qualifiers"]:
        if required == "object.process":
            present = claim.object.process is not None
        elif required in {"product", "substrate", "mediator"}:
            present = any(p.role == required for p in claim.participants)
        else:
            present = _nonempty(claim.qualifiers.get(required))
        if not present:
            _review(claim, f"predicate registry: missing required {required} for {name}")
    if name == "DIFFERS_BETWEEN":
        _validate_comparison(claim, repairs)
    if name == "CORRELATED_WITH" and "association_direction" in claim.qualifiers:
        if claim.qualifiers["association_direction"] not in ("positive", "negative", "unspecified"):
            raise ValueError("Correlation requires positive|negative|unspecified association_direction")
    if name in {"AFFECTS", "ACTIVATES", "INHIBITS"} and claim.object.process is None:
        _review(claim, "predicate registry: affected object aspect is unspecified; do not infer abundance/activity")
    if name in {"ACTIVATES", "INHIBITS"} and claim.object.process and claim.object.process.kind in {"abundance", "prevalence", "concentration", "diversity"}:
        raise ValueError("Abundance/prevalence/concentration/diversity changes require AFFECTS with object.process")


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _review(claim, reason):
    if reason not in claim.review_reasons:
        claim.review_reasons.append(reason)


def _validate_comparison(claim, repairs):
    # A bare differential edge is uninterpretable; do not project one without
    # the two source-grounded groups, metric and focal-vs-reference direction.
    for name in ("comparison_group", "reference_group", "metric", "direction"):
        if not _nonempty(claim.qualifiers.get(name)):
            raise ValueError(f"DIFFERS_BETWEEN requires qualifiers.{name}")
    if claim.qualifiers["direction"] not in {"increased", "decreased", "unchanged", "unspecified"}:
        raise ValueError("Invalid DIFFERS_BETWEEN direction")
    groups = {}
    for role in ("comparison_group", "reference_group"):
        candidates = [p.entity for p in claim.participants if p.role == role and p.entity.type == "StudyGroup"]
        if len(candidates) != 1:
            raise ValueError(f"DIFFERS_BETWEEN requires one grounded {role} StudyGroup participant")
        entity = candidates[0]
        groups[role] = entity
        labels = {entity.text.casefold(), (entity.mention or entity.text).casefold()}
        if claim.qualifiers[role].strip().casefold() not in labels:
            raise ValueError(f"DIFFERS_BETWEEN {role} must match its participant")
    focal, reference = groups["comparison_group"], groups["reference_group"]
    if focal.text.casefold() == reference.text.casefold():
        raise ValueError("DIFFERS_BETWEEN requires two distinct groups")
    if claim.object.text.casefold() != focal.text.casefold():
        raise ValueError("DIFFERS_BETWEEN object must be the focal comparison_group")
    if claim.subject.process:
        direction = claim.qualifiers['direction']
        if claim.subject.process.direction == 'unspecified':
            if direction != 'unspecified':
                repairs.append(dict(field='subject.process.direction', original='unspecified', replacement=direction))
                claim.subject.process.direction = direction
        elif claim.subject.process.direction != direction:
            raise ValueError("DIFFERS_BETWEEN subject process direction must match comparison direction")


def predicate_prompt():
    """Compact registry projection; avoid repeating evidence/type lists per row."""
    rows = []
    for name, spec in PREDICATES.items():
        rows.append(f"{name}: {spec['definition']}; S={','.join(spec['subject_types'])}; "
                    f"O={','.join(spec['object_types'])}; required={','.join(spec['required_qualifiers']) or '-'}")
    return "\n".join(rows)
