"""
Ontology-constrained extraction — only schema-valid facts reach the graph
(work-plan task 2.3).

Keel's stock extraction emits a domain-neutral entity model (PERSON,
ORGANIZATION, PROJECT, DATE…). This replaces it with a pass constrained by the
legal ontology: the prompt is *generated from* :mod:`app.ontology.schema`, and
everything the model returns is validated against that same schema before it is
allowed anywhere near Neo4j.

The design point is that the guarantee lives in the validator, not the prompt.
A prompt asks; a validator enforces. :func:`extract_facts` therefore:

1. builds the extraction prompt from the live schema, so a new node type is
   automatically extractable and cannot be forgotten;
2. parses the model's JSON defensively — a model that returns prose, a fenced
   block, or a partial object must degrade to "nothing extracted", never to a
   crash mid-ingest;
3. stamps **provenance** on every node and edge from the chunk it came from,
   which is what makes the provenance invariant satisfiable at all;
4. validates each fact and **drops** the invalid ones, recording why;
5. normalises what the ontology says must be normalised — statutory provisions
   through :mod:`app.statutes.resolver`, dates through
   :mod:`app.ontology.temporal`, party names through
   :mod:`app.ontology.entity_resolution`.

Nothing here calls an LLM directly. :func:`extract_facts` takes a ``complete``
callable, so the same code runs against Gemini in production and against a
fixture in tests — and so the caller owns the usage metering.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from loguru import logger

from app.ontology.entity_resolution import canonical_index, canonicalise, resolve_entities
from app.ontology.schema import (
    EDGE_TYPES,
    NODE_TYPES,
    NODES_BY_LABEL,
    ONTOLOGY_VERSION,
    PROPERTY_ANCHORED,
    OntologyViolation,
    validate_edge,
    validate_node,
)
from app.ontology.temporal import parse_date
from app.statutes.resolver import Resolution, resolve_provision

#: Confidence below which a fact is kept but marked unverified rather than
#: asserted. Mirrors the register's `unverified` idea: low confidence is not the
#: same as false.
DEFAULT_CONFIDENCE_FLOOR = 0.35

#: Node labels whose ``canonical_name`` participates in entity resolution.
_RESOLVED_LABELS = ("Party", "Advocate", "Forum")

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass
class ExtractionResult:
    """
    What one extraction pass produced.

    Attributes:
        nodes: Schema-valid nodes, provenance stamped.
        edges: Schema-valid relationships, provenance stamped.
        rejected: Facts dropped by validation, each with the reason. Never
            silently discarded — a high rejection rate is the signal that the
            prompt or the schema needs work.
        unverified: Facts kept but flagged (low confidence, or an unresolvable
            statutory citation).
        stats: Counts for the ingestion log.
    """

    nodes: List[Dict[str, Any]] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    unverified: List[Dict[str, Any]] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)

    @property
    def rejection_rate(self) -> float:
        total = len(self.nodes) + len(self.edges) + len(self.rejected)
        return round(len(self.rejected) / total, 4) if total else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ontologyVersion": ONTOLOGY_VERSION,
            "nodes": self.nodes,
            "edges": self.edges,
            "rejected": self.rejected,
            "unverified": self.unverified,
            "stats": {**self.stats, "rejectionRate": self.rejection_rate},
        }


def build_extraction_prompt(labels: Optional[Sequence[str]] = None) -> str:
    """
    Generate the extraction prompt from the live ontology.

    Generated rather than hand-written so a schema change cannot leave the
    prompt describing an ontology that no longer exists.

    Args:
        labels: Restrict to these node types. Useful for a targeted second pass
            over a document type known to carry only certain facts.

    Returns:
        The system prompt text.
    """
    specs = [n for n in NODE_TYPES if not labels or n.label in labels]
    node_lines = []
    for spec in specs:
        properties = ", ".join(f"{p.name}{'*' if p.required else ''}" for p in spec.properties)
        node_lines.append(f"- {spec.label}: {spec.description}. Properties: {properties}")

    edge_lines = [
        f"- {e.type}: ({'|'.join(e.from_labels)}) -> ({'|'.join(e.to_labels)}). {e.description}"
        for e in EDGE_TYPES
        if e.type not in PROPERTY_ANCHORED and not e.derived
    ]

    return f"""You extract structured legal facts from Indian litigation documents.

Return ONLY a JSON object of the form:
{{"nodes": [{{"label": "...", "properties": {{...}}, "confidence": 0.0-1.0}}],
  "edges": [{{"type": "...", "from": {{"label": "...", "key": "..."}},
              "to": {{"label": "...", "key": "..."}}, "properties": {{...}},
              "confidence": 0.0-1.0}}]}}

NODE TYPES (properties marked * are required):
{chr(10).join(node_lines)}

RELATIONSHIP TYPES:
{chr(10).join(edge_lines)}

RULES
1. Extract only what the text states. Do not infer, complete or assume a fact
   the document does not contain. An incomplete graph is recoverable; a
   fabricated one is not.
2. Every fact must be traceable to this passage. If you cannot point at the
   words that support it, do not emit it.
3. Use the exact label and property names above. Do not invent new ones.
4. `confidence` is your own assessment in [0,1] that the text supports the fact.
5. Dates: copy the text's own wording. Do not reformat or guess a missing
   component — downstream normalisation handles the format and records how
   precise it was.
6. Statutory references: copy the act and section exactly as written, including
   a legacy IPC/CrPC/Evidence Act reference. Downstream resolution maps it to
   the current provision.
7. `key` on an edge endpoint is the identifying value of the node it refers to
   (a Party's canonical_name, a Provision's section_number, and so on).
8. Emit nothing rather than something uncertain. An empty result is a valid
   answer for a passage that contains no legal facts.
"""


#: Properties worth matching an edge's scalar ``key`` against when the node's
#: identity is composite. Ordered by how strongly each identifies a node, so an
#: exact hit on a name beats one on a free-text description.
_ENDPOINT_MATCH_PROPERTIES = (
    "canonical_name",
    "citation",
    "annexure_ref",
    "matter_id",
    "document_id",
    "relief_type",
    "obligation_type",
    "event_type",
    "filing_type",
    "asset_type",
    "medium",
    "purpose",
    "operative_direction",
    "reference",
    "identifier",
    "section_number",
    "description",
)


def _build_endpoint_index(
    nodes: Sequence[Mapping[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Index this pass's accepted nodes by label, for endpoint addressing.

    Args:
        nodes: Accepted ``{"label", "properties"}`` entries.

    Returns:
        ``label -> [properties, ...]`` in extraction order.
    """
    index: Dict[str, List[Dict[str, Any]]] = {}
    for node in nodes:
        label = str(node.get("label") or "")
        properties = node.get("properties")
        if label and isinstance(properties, Mapping):
            index.setdefault(label, []).append(dict(properties))
    return index


def _identity_map(label: str, properties: Mapping[str, Any]) -> Dict[str, Any]:
    """The identity properties of one node, as a key map for MATCH."""
    spec = NODES_BY_LABEL[label]
    return {key: properties.get(key) for key in spec.identity}


def _address_endpoint(
    label: str,
    key: Any,
    endpoints: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[Any, Optional[str]]:
    """
    Turn an edge endpoint's key into something the writer can MATCH.

    A scalar-identity label (Party, Matter, Forum, ...) is already addressable
    by the model's scalar key and passes straight through. A composite-identity
    label (Breach, Relief, Obligation, ...) cannot be, so the referenced node is
    located among this pass's own nodes and its full identity map is returned.

    Ambiguity is refused rather than guessed, for the same reason entity
    resolution refuses it: matching the wrong Breach would attach an allegation
    to the wrong facts, which is worse than losing the edge.

    Args:
        label: The endpoint's node label.
        key: The model's scalar key, or a key map it supplied itself.
        endpoints: Output of :func:`_build_endpoint_index`.

    Returns:
        ``(key, None)`` on success, or ``(key, reason)`` when the endpoint
        cannot be addressed.
    """
    spec = NODES_BY_LABEL.get(label)
    if spec is None:
        return key, f"unknown label {label!r}"

    # The model occasionally supplies a full key map. Honour it.
    if isinstance(key, Mapping):
        supplied = {k: v for k, v in key.items() if k in spec.identity}
        if len(supplied) == len(spec.identity):
            return supplied, None
        return key, "supplied key map does not cover the identity"

    if len(spec.identity) == 1:
        if key in (None, ""):
            return key, "no key given"
        return key, None

    if key in (None, ""):
        return key, "no key given for a composite-identity node"

    needle = str(key).strip().casefold()
    candidates = endpoints.get(label) or []
    if not candidates:
        return key, f"no {label} node was extracted from this passage to match"

    exact: List[Mapping[str, Any]] = []
    partial: List[Mapping[str, Any]] = []
    for properties in candidates:
        values = [
            str(properties[p]).strip().casefold()
            for p in _ENDPOINT_MATCH_PROPERTIES
            if properties.get(p) not in (None, "")
        ]
        if any(v == needle for v in values):
            exact.append(properties)
        elif any(needle in v or v in needle for v in values if v):
            partial.append(properties)

    chosen = exact or partial
    if not chosen:
        return key, f"no extracted {label} matches key {str(key)[:60]!r}"
    if len(chosen) > 1:
        return key, (
            f"key {str(key)[:60]!r} matches {len(chosen)} extracted {label} nodes; "
            "refusing to guess"
        )
    return _identity_map(label, chosen[0]), None


def parse_model_json(raw: str) -> Dict[str, Any]:
    """
    Parse the model's response into ``{"nodes": [...], "edges": [...]}``.

    Tolerates a fenced code block and leading/trailing prose, because those are
    the two ways a model most often wraps otherwise-valid JSON. Anything
    genuinely unparseable yields empty lists — a document that produces no facts
    is a normal outcome, and crashing an ingest over a malformed response is not.

    Args:
        raw: The model's raw text.

    Returns:
        A dict with ``nodes`` and ``edges`` lists, both possibly empty.
    """
    if not raw or not raw.strip():
        return {"nodes": [], "edges": []}

    candidate = raw.strip()
    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            candidate = candidate[start : end + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        logger.warning("Extraction response was not valid JSON ({}); treating as empty", exc)
        return {"nodes": [], "edges": []}

    if not isinstance(parsed, dict):
        logger.warning("Extraction response was not a JSON object; treating as empty")
        return {"nodes": [], "edges": []}

    nodes = parsed.get("nodes")
    edges = parsed.get("edges")
    return {
        "nodes": nodes if isinstance(nodes, list) else [],
        "edges": edges if isinstance(edges, list) else [],
    }


def _provenance(
    workspace_id: str, document_id: str, chunk_id: str, page: Optional[int], confidence: float
) -> Dict[str, Any]:
    """The mandatory block the ontology requires on every node and edge."""
    return {
        "workspace_id": workspace_id,
        "source_document_id": document_id,
        "chunk_id": chunk_id,
        "page": page,
        "extraction_confidence": max(0.0, min(1.0, float(confidence))),
    }


def _normalise_provision(properties: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Resolve a Provision node against the statutory register.

    Returns the node's updated properties, or None when the citation cannot be
    emitted as a current provision at all (an unmapped legacy section, or a
    malformed one) — Section 4.3's rule that an unresolvable citation is flagged,
    never silently emitted.
    """
    resolved = resolve_provision(
        properties.get("statute_ref", ""), properties.get("section_number", "")
    )
    if resolved.resolution is Resolution.INVALID:
        return None
    if resolved.resolution is Resolution.UNMAPPED_LEGACY:
        return None

    updated = dict(properties)
    updated["resolution_status"] = (
        "resolved"
        if resolved.resolution in (Resolution.RESOLVED, Resolution.MAPPED)
        else "unverified"
    )
    if resolved.section:
        updated["section_number"] = resolved.section
    if resolved.short_title:
        updated["statute_ref"] = resolved.short_title
    if resolved.heading and not updated.get("heading"):
        updated["heading"] = resolved.heading
    return updated


def _normalise_dates(label: str, properties: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise any date-typed property, recording precision where the schema has it."""
    from app.ontology.schema import NODES_BY_LABEL

    spec = NODES_BY_LABEL.get(label)
    if spec is None:
        return properties
    updated = dict(properties)
    for prop in spec.properties:
        if prop.type != "date":
            continue
        raw = updated.get(prop.name)
        if not raw:
            continue
        parsed = parse_date(str(raw))
        if parsed is None:
            continue
        updated[prop.name] = parsed.iso
        if "date_precision" in spec.property_names and prop.name in (
            "event_date",
            "hearing_date",
            "filed_on",
            "order_date",
        ):
            updated.setdefault("date_precision", parsed.precision)
    return updated


def extract_facts(
    chunks: Sequence[Mapping[str, Any]],
    complete: Callable[[str, str], str],
    workspace_id: str,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> ExtractionResult:
    """
    Run ontology-constrained extraction over a document's chunks.

    Args:
        chunks: Items with ``chunk_id``, ``document_id``, ``text`` and
            optionally ``page``.
        complete: ``(system_prompt, user_text) -> raw_model_text``. Injected so
            this module never owns an LLM client and so the caller keeps the
            usage-metering scope open.
        workspace_id: Written onto every node and edge; the tenant boundary.
        confidence_floor: Facts below this are kept but marked unverified.

    Returns:
        An :class:`ExtractionResult`. Never raises for a bad model response — a
        failure to extract is data, not an exception.
    """
    prompt = build_extraction_prompt()
    result = ExtractionResult()
    raw_nodes: List[Dict[str, Any]] = []
    raw_edges: List[Dict[str, Any]] = []

    for chunk in chunks:
        chunk_id = str(chunk.get("chunk_id") or "")
        document_id = str(chunk.get("document_id") or "")
        text = str(chunk.get("text") or "")
        if not text.strip() or not chunk_id or not document_id:
            continue
        try:
            response = complete(prompt, text)
        except Exception as exc:  # noqa: BLE001 - one chunk must not sink the document
            logger.warning("Extraction failed for chunk {}: {}", chunk_id, exc)
            result.rejected.append({"chunk_id": chunk_id, "reason": f"model error: {exc}"})
            continue

        parsed = parse_model_json(response)
        page = chunk.get("page")
        for node in parsed["nodes"]:
            if isinstance(node, dict):
                raw_nodes.append({**node, "_chunk": chunk_id, "_doc": document_id, "_page": page})
        for edge in parsed["edges"]:
            if isinstance(edge, dict):
                raw_edges.append({**edge, "_chunk": chunk_id, "_doc": document_id, "_page": page})

    # --- Entity resolution across the whole document ------------------------
    mentions = [
        {"name": n.get("properties", {}).get("canonical_name"), "source_document_id": n["_doc"]}
        for n in raw_nodes
        if n.get("label") in _RESOLVED_LABELS and n.get("properties", {}).get("canonical_name")
    ]
    index = canonical_index(resolve_entities(mentions)) if mentions else {}

    # --- Validate nodes -----------------------------------------------------
    for node in raw_nodes:
        label = str(node.get("label") or "")
        properties = node.get("properties")
        if not isinstance(properties, dict):
            result.rejected.append({"label": label, "reason": "properties is not an object"})
            continue

        properties = _normalise_dates(label, properties)
        if label in _RESOLVED_LABELS and properties.get("canonical_name"):
            properties["canonical_name"] = canonicalise(properties["canonical_name"], index)
        if label == "Provision":
            normalised = _normalise_provision(properties)
            if normalised is None:
                result.rejected.append(
                    {
                        "label": label,
                        "reason": "statutory citation could not be resolved to a current provision",
                        "properties": properties,
                    }
                )
                continue
            properties = normalised

        confidence = float(node.get("confidence", 0.5) or 0.0)
        full = {
            **properties,
            **_provenance(workspace_id, node["_doc"], node["_chunk"], node["_page"], confidence),
        }
        try:
            validate_node(label, full)
        except OntologyViolation as exc:
            result.rejected.append({"label": label, "reason": str(exc), "properties": properties})
            continue

        entry = {"label": label, "properties": full}
        if confidence < confidence_floor:
            entry["unverified"] = True
            result.unverified.append(entry)
        result.nodes.append(entry)

    # --- Address composite-identity endpoints -------------------------------
    # Only 8 of the 20 node types are identified by a single property. Edges
    # pointing at the other 12 — Breach, Relief, Obligation, Event, Order,
    # Provision and friends — used to be dropped by the writer with "composite
    # -identity endpoints need an explicit key map", which orphaned every
    # legally interesting fact: on a real matter 7 of 12 edges were lost and
    # `reliefs` / `obligation_chain` returned nothing from a graph that held
    # three Relief and three Breach nodes.
    #
    # The fix resolves the endpoint against the nodes extracted in this same
    # pass rather than asking the model to restate a three-part key it would
    # have to reproduce exactly. The nodes are right here; matching them is
    # deterministic, and no prompt change means no new rejection risk.
    endpoints = _build_endpoint_index(result.nodes)

    # --- Validate edges -----------------------------------------------------
    for edge in raw_edges:
        edge_type = str(edge.get("type") or "")
        from_ref, to_ref = edge.get("from"), edge.get("to")
        if not isinstance(from_ref, dict):
            result.rejected.append({"type": edge_type, "reason": "missing 'from' endpoint"})
            continue
        from_label = str(from_ref.get("label") or "")
        to_label = str(to_ref.get("label")) if isinstance(to_ref, dict) else None

        properties = edge.get("properties") if isinstance(edge.get("properties"), dict) else {}
        confidence = float(edge.get("confidence", 0.5) or 0.0)
        full = {
            **properties,
            **_provenance(workspace_id, edge["_doc"], edge["_chunk"], edge["_page"], confidence),
        }
        try:
            validate_edge(edge_type, from_label, to_label, full)
        except OntologyViolation as exc:
            result.rejected.append({"type": edge_type, "reason": str(exc)})
            continue

        from_key = from_ref.get("key")
        to_key = to_ref.get("key") if isinstance(to_ref, dict) else None
        if from_label in _RESOLVED_LABELS and from_key:
            from_key = canonicalise(str(from_key), index)
        if to_label in _RESOLVED_LABELS and to_key:
            to_key = canonicalise(str(to_key), index)

        # Composite-identity endpoints become an explicit key map here, so the
        # writer can MATCH them. A scalar-identity endpoint is left as-is.
        from_key, from_why = _address_endpoint(from_label, from_key, endpoints)
        if from_why:
            result.rejected.append(
                {"type": edge_type, "reason": f"tail {from_label}: {from_why}"}
            )
            continue
        if to_label:
            to_key, to_why = _address_endpoint(to_label, to_key, endpoints)
            if to_why:
                result.rejected.append(
                    {"type": edge_type, "reason": f"head {to_label}: {to_why}"}
                )
                continue

        entry = {
            "type": edge_type,
            "from": {"label": from_label, "key": from_key},
            "to": {"label": to_label, "key": to_key} if to_label else None,
            "properties": full,
        }
        if confidence < confidence_floor:
            entry["unverified"] = True
            result.unverified.append(entry)
        result.edges.append(entry)

    result.stats = {
        "chunks": len(chunks),
        "nodesExtracted": len(result.nodes),
        "edgesExtracted": len(result.edges),
        "rejected": len(result.rejected),
        "unverified": len(result.unverified),
        "entitiesResolved": len(index),
    }
    logger.info(
        "Extraction: {} chunks -> {} nodes, {} edges ({} rejected, {} unverified)",
        len(chunks),
        len(result.nodes),
        len(result.edges),
        len(result.rejected),
        len(result.unverified),
    )
    if result.rejection_rate > 0.3:
        logger.warning(
            "Extraction rejection rate is {:.0%} — the prompt or the ontology "
            "needs attention, not the individual facts",
            result.rejection_rate,
        )
    return result
