import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any


ENTITY_TYPES = {
    "assay": "Assay",
    "cellline": "CellLine",
    "cell_line": "CellLine",
    "compound": "Compound",
    "disease": "Disease",
    "drug": "Drug",
    "gene": "Gene",
    "mutation": "Mutation",
    "organism": "Organism",
    "other": "Other",
    "pathway": "Pathway",
    "protein": "Protein",
    "chemical": "Compound",
    "chemicalcompound": "Compound",
    "smallmolecule": "Compound",
    "medication": "Drug",
    "proteins": "Protein",
    "genes": "Gene",
    "celllines": "CellLine",
}

_GREEK = str.maketrans({
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta",
    "ε": "epsilon", "κ": "kappa", "λ": "lambda", "μ": "mu",
    "ω": "omega", "θ": "theta", "σ": "sigma", "τ": "tau",
    "ζ": "zeta", "η": "eta", "ι": "iota", "ν": "nu", "ξ": "xi",
    "ο": "omicron", "π": "pi", "ρ": "rho", "υ": "upsilon",
    "φ": "phi", "χ": "chi", "ψ": "psi",
})

_DASHES = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)


@dataclass(frozen=True)
class EntityIdentity:
    canonical_key: str
    canonical_name: str
    entity_type: str
    normalized_id: str | None
    normalized_alias: str
    aliases: tuple[str, ...] = ()
    context_resolved: bool = False
    document_scoped: bool = False
    context_ambiguous: bool = False
    local_label: bool = False


def clean_text(value: str | None, fallback: str = "Unknown") -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = text.translate(_DASHES)
    text = re.sub(r"\s+", " ", text).strip()
    return text or fallback


def normalize_entity_type(value: str | None) -> str:
    raw = clean_text(value, fallback="Other")
    key = re.sub(r"[\s-]+", "_", raw).casefold()
    compact_key = key.replace("_", "")
    return ENTITY_TYPES.get(key, ENTITY_TYPES.get(compact_key, raw))


def normalize_alias(value: str | None) -> str:
    text = clean_text(value)
    text = text.strip(" \t\r\n\"'`.,;:")
    return re.sub(r"\s+", " ", text).casefold()


def entity_family(entity_type: str) -> str:
    canonical = normalize_entity_type(entity_type)
    return "Chemical" if canonical in {"Drug", "Compound"} else canonical


def compatible_entity_types(entity_type: str) -> list[str]:
    return ["Drug", "Compound"] if entity_family(entity_type) == "Chemical" else [entity_type]


def normalize_entity_alias(value: str | None, entity_type: str) -> str:
    text = normalize_alias(value).translate(_GREEK)
    canonical_type = normalize_entity_type(entity_type)
    if canonical_type in {"Gene", "Protein", "CellLine", "Pathway"}:
        # Keep digits, slash-delimited groups, mutations and species labels.
        text = re.sub(r"[\s_-]+", "", text)
    elif canonical_type == "Disease":
        text = re.sub(r"[\s_-]+", " ", text)
    else:
        # Chemical hyphens can carry stereochemical meaning.
        text = re.sub(r"\s*-\s*", "-", text)
    return text


def normalize_external_id(value: str | None) -> str | None:
    if not value:
        return None
    normalized = clean_text(value, fallback="")
    patterns = [
        (r"(?:CHEBI\s*[:_]\s*|https?://purl\.obolibrary\.org/obo/CHEBI_)(\d+)", "CHEBI"),
        (r"(?:PubChem(?:\s*CID)?\s*:\s*|CID\s*:\s*|https?://pubchem\.ncbi\.nlm\.nih\.gov/compound/)(\d+)", "PubChem"),
        (r"(?:UniProt(?:KB)?\s*:\s*|https?://(?:www\.)?uniprot\.org/(?:uniprotkb|uniprot)/|https?://purl\.uniprot\.org/uniprot/)([A-Z0-9]+(?:-\d+)?)", "UniProt"),
        (r"(?:NCBI\s*Gene\s*:\s*|GeneID\s*:\s*|Entrez(?:\s*Gene)?\s*:\s*|https?://(?:www\.)?ncbi\.nlm\.nih\.gov/gene/)(\d+)", "NCBIGene"),
        (r"(?:DrugBank\s*:\s*|https?://(?:go\.)?drugbank\.(?:com|ca)/drugs/)(DB\d+)", "DrugBank"),
    ]
    for pattern, namespace in patterns:
        match = re.fullmatch(pattern, normalized.rstrip("/"), flags=re.I)
        if match:
            identifier = match.group(1).upper()
            if identifier.isdigit():
                identifier = str(int(identifier))
            return f"{namespace}:{identifier}"
    return re.sub(r"\s*:\s*", ":", normalized) or None


def identifier_namespace(value: str) -> str:
    normalized = normalize_external_id(value) or value
    return normalized.split(":", 1)[0].casefold() if ":" in normalized else "unknown"


def abbreviation_key(value: str) -> str:
    return re.sub(r"[\s_-]+", "", normalize_alias(value).translate(_GREEK))


def _align_abbreviation(short: str, long: str) -> str | None:
    """Match abbreviation letters backwards, requiring the first at a word start."""
    letters = [char for char in short.casefold().translate(_GREEK) if char.isalnum()]
    if not 2 <= len(letters) <= 12 or not any(char.isalpha() for char in letters):
        return None
    folded = []
    original_positions = []
    for original_position, char in enumerate(long):
        for value in char.casefold().translate(_GREEK):
            folded.append(value)
            original_positions.append(original_position)
    comparable_long = "".join(folded)
    position = len(comparable_long) - 1
    start = None
    for index in range(len(letters) - 1, -1, -1):
        while position >= 0:
            matches = comparable_long[position] == letters[index]
            word_start = position == 0 or not comparable_long[position - 1].isalnum()
            if matches and (index > 0 or word_start):
                break
            position -= 1
        if position < 0:
            return None
        start = original_positions[position]
        position -= 1
    expansion = long[start:].strip(" ,;:")
    modifier = re.search(
        r"\b(human|mouse|murine|rat|bovine|porcine|canine|mutant|recombinant|phosphorylated|wild-type)\s*$",
        long[:start], flags=re.I,
    )
    if modifier:
        expansion = long[modifier.start():].strip(" ,;:")
    if abbreviation_key(expansion) == abbreviation_key(short):
        return None
    # Definitions should contain a full word, rather than another short symbol.
    if not any(len(token) > 3 for token in expansion.split()):
        return None
    if re.findall(r"\d+", short) != re.findall(r"\d+", expansion):
        return None
    return expansion


def extract_abbreviations(text: str) -> dict[str, str | None]:
    definitions: dict[str, dict[str, str]] = {}
    normalized_text = clean_text(text, fallback="")
    for match in re.finditer(r"\(([^()\n]{2,100})\)", normalized_text):
        inside = match.group(1).strip()
        prefix = normalized_text[:match.start()]
        clause = re.split(r"[.;:!?\n]", prefix)[-1].strip()
        short = inside
        long = " ".join(clause.split()[-20:])
        if len(inside.split()) > 2 or len(inside) > 20:
            preceding = re.search(r"([\wα-ω-]{2,20})\s*$", clause)
            if not preceding:
                continue
            short, long = preceding.group(1), inside
        if len(short.split()) > 2 or not re.fullmatch(r"[\wα-ω -]+", short):
            continue
        expansion = _align_abbreviation(short, long)
        if not expansion:
            continue
        key = abbreviation_key(short)
        comparable = abbreviation_key(expansion)
        definitions.setdefault(key, {})[comparable] = expansion
    # Ambiguous definitions in one document are deliberately left unresolved.
    return {
        key: next(iter(values.values())) if len(values) == 1 else None
        for key, values in definitions.items()
    }


def entity_identity(
    text: str | None,
    entity_type: str | None,
    normalized_id: str | None = None,
    abbreviations: dict[str, str | None] | None = None,
    document_id: int | None = None,
) -> EntityIdentity:
    original_name = clean_text(text)
    canonical_name = original_name
    canonical_type = normalize_entity_type(entity_type)
    external_id = normalize_external_id(normalized_id)
    if canonical_type == "Other" and external_id:
        canonical_type = {"uniprot": "Protein", "ncbigene": "Gene", "chebi": "Compound", "pubchem": "Compound", "drugbank": "Drug"}.get(identifier_namespace(external_id), canonical_type)
    definitions = {**(abbreviations or {}), **extract_abbreviations(original_name)}
    context_ambiguous = abbreviation_key(original_name) in definitions and definitions[abbreviation_key(original_name)] is None
    expansion = definitions.get(abbreviation_key(original_name))
    inline = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", original_name)
    if inline and abbreviation_key(inline.group(2)) in definitions:
        expansion = definitions[abbreviation_key(inline.group(2))]
    if expansion:
        canonical_name = expansion
    normalized_name = normalize_entity_alias(canonical_name, canonical_type)
    aliases = {original_name, canonical_name}
    for abbreviation, full_name in definitions.items():
        if full_name and normalize_entity_alias(full_name, canonical_type) == normalized_name:
            aliases.add(abbreviation)

    if external_id:
        identity_part = f"id:{external_id.casefold()}"
    else:
        identity_part = f"name:{normalized_name}"
    non_specific = normalize_alias(canonical_name) in {
        "unknown", "unspecified", "unnamed", "compound", "drug", "protein", "gene",
        "the compound", "the drug", "the protein", "the gene",
    }
    local_label = non_specific or bool(
        entity_family(canonical_type) == "Chemical"
        and re.fullmatch(r"(?:compound|drug|molecule|inhibitor|sample|probe)\s*[-#]?\s*(?:\d+[a-z]?|[a-z])", canonical_name, flags=re.I)
    )
    document_scoped = bool(
        document_id is not None and external_id is None
        and (
            context_ambiguous or local_label
        )
    )
    if document_scoped:
        identity_part = f"document:{document_id}|{identity_part}"

    return EntityIdentity(
        canonical_key=f"{entity_family(canonical_type).casefold()}|{identity_part}",
        canonical_name=canonical_name,
        entity_type=canonical_type,
        normalized_id=external_id,
        normalized_alias=normalized_name,
        aliases=tuple(sorted(aliases)),
        context_resolved=bool(expansion),
        document_scoped=document_scoped,
        context_ambiguous=context_ambiguous,
        local_label=local_label,
    )


def normalize_predicate(value: str | None) -> str:
    predicate = clean_text(value, fallback="RELATED_TO")
    predicate = re.sub(r"[^0-9A-Za-z]+", "_", predicate)
    predicate = re.sub(r"_+", "_", predicate).strip("_")
    return (predicate or "RELATED_TO").upper()


def _normalize_qualifier_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return clean_text(value, fallback="")
    if isinstance(value, dict):
        return {
            str(key): _normalize_qualifier_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_qualifier_value(item) for item in value]
    return value


def canonicalize_qualifiers(qualifiers: dict[str, Any] | None) -> dict[str, Any]:
    if not qualifiers:
        return {}
    return _normalize_qualifier_value(qualifiers)


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _fingerprint_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.casefold()
    if isinstance(value, dict):
        return {
            key: _fingerprint_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_fingerprint_value(item) for item in value]
    return value


def qualifiers_fingerprint(qualifiers: dict[str, Any] | None) -> str:
    canonical = canonicalize_qualifiers(qualifiers)
    comparable = _fingerprint_value(canonical)
    return hashlib.sha256(stable_json(comparable).encode("utf-8")).hexdigest()


def claim_fingerprint(
    *,
    subject: EntityIdentity,
    predicate: str,
    object_: EntityIdentity,
    qualifiers: dict[str, Any] | None,
    evidence_text: str,
    negated: bool,
    speculative: bool,
) -> str:
    payload = {
        "subject": subject.canonical_key,
        "predicate": normalize_predicate(predicate),
        "object": object_.canonical_key,
        "qualifiers": canonicalize_qualifiers(qualifiers),
        "evidence": normalize_alias(evidence_text),
        "negated": bool(negated),
        "speculative": bool(speculative),
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
