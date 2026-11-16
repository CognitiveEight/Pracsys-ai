"""
Trust-pipeline persistence: one payload-only Qdrant collection.

``record`` says what a point is:

* ``evidence`` — one extracted claim from one passage (stored input);
* ``doc_profile`` — one document's mean embedding and relevance (stored input);
* ``decision`` — a reviewer's resolution, keys and action only (stored input);
* ``fact`` / ``conflict`` / ``review`` — derived, rebuilt from the inputs on
  every pass by :func:`app.trust.evaluate.evaluate_case`.

Deletion goes through the vector store's two choke points, not through here:
``delete_documents`` erases a document's evidence, profile and every derived
record naming it; ``delete_workspace`` erases the whole slice. Evidence carries
``original_document_id`` and derived records carry ``document_ids`` precisely so
those filters reach them.

Point ids are uuid5 of the record identity, so a replay overwrites instead of
duplicating.

**No write lands after a purge.** The purge blocks the workspace's persistence
first (``block_workspace_persistence``); every write here checks that block,
once before and once inside the worker thread right before it upserts, so a
pass already running when the purge starts cannot recreate the records the
purge is about to delete.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from uuid import NAMESPACE_DNS, uuid5

from qdrant_client.http import models
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue, PointStruct

RECORD_EVIDENCE = "evidence"
RECORD_PROFILE = "doc_profile"
RECORD_DECISION = "decision"
RECORD_FACT = "fact"
RECORD_CONFLICT = "conflict"
RECORD_REVIEW = "review"
DERIVED_RECORDS = (RECORD_FACT, RECORD_CONFLICT, RECORD_REVIEW)

_UPSERT_BATCH = 256


def _vs():
    from app.vectorstore.qdrant import vector_store

    return vector_store


def point_id(workspace_id: str, record: str, key: str) -> str:
    return str(uuid5(NAMESPACE_DNS, f"trust:{workspace_id}:{record}:{key}"))


def _filter(workspace_id: str, kind: Optional[str] = None, **match_any: Iterable[str]) -> Filter:
    must: List[Any] = [FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id))]
    if kind:
        must.append(FieldCondition(key="record", match=MatchValue(value=kind)))
    for key, values in match_any.items():
        must.append(FieldCondition(key=key, match=MatchAny(any=list(values))))
    return Filter(must=must)


async def _client():
    vs = _vs()
    if vs.client is None:
        await vs.initialize()
    return vs.client, vs.trust_collection


def _scroll(client, collection: str, flt: Filter, with_payload: Any = True) -> List[Any]:
    points, offset = [], None
    while True:
        batch, offset = client.scroll(
            collection_name=collection,
            scroll_filter=flt,
            limit=256,
            offset=offset,
            with_payload=with_payload,
            with_vectors=False,
        )
        points.extend(batch)
        if offset is None:
            return points


def _upsert(client, collection: str, points: Sequence[PointStruct]) -> None:
    workspaces = {str((p.payload or {}).get("workspace_id")) for p in points}
    if any(_blocked(ws) for ws in workspaces):
        return
    for i in range(0, len(points), _UPSERT_BATCH):
        client.upsert(collection_name=collection, points=list(points[i : i + _UPSERT_BATCH]))


def _blocked(workspace_id: str) -> bool:
    from app.workspace.persistence import is_workspace_persistence_blocked

    return is_workspace_persistence_blocked(workspace_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def evidence_key(row: Mapping[str, Any]) -> str:
    return "|".join(
        (
            str(row.get("document_id")),
            str(row.get("chunk_id")),
            str(row.get("fact_key")),
            "denial" if row.get("denial") else "",
        )
    )


async def replace_document_evidence(
    workspace_id: str, document_ids: Sequence[str], rows: Sequence[Mapping[str, Any]]
) -> int:
    """Store these documents' evidence, replacing what they had before.

    The old rows go first, so a re-read document with fewer facts keeps no
    stale evidence.
    """
    client, collection = await _client()
    base = {"workspace_id": workspace_id, "record": RECORD_EVIDENCE}
    points = [
        PointStruct(
            id=point_id(workspace_id, RECORD_EVIDENCE, evidence_key(row)),
            vector={},
            payload={
                **base,
                **row,
                "original_document_id": row.get("document_id"),
                "extracted_at": _now(),
            },
        )
        for row in rows
    ]

    if _blocked(workspace_id):
        return 0

    def _write() -> None:
        if document_ids:
            client.delete(
                collection_name=collection,
                points_selector=models.FilterSelector(
                    filter=_filter(
                        workspace_id, RECORD_EVIDENCE, original_document_id=document_ids
                    )
                ),
            )
        _upsert(client, collection, points)

    await asyncio.to_thread(_write)
    return len(points)


async def delete_document_records(workspace_id: str, document_ids: Sequence[str]) -> None:
    """Evidence and profiles of these documents (the vector store's delete does
    the same; this is the re-check under the trust lock, see pipeline.py)."""
    if not document_ids:
        return
    client, collection = await _client()
    await asyncio.to_thread(
        client.delete,
        collection_name=collection,
        points_selector=models.FilterSelector(
            filter=_filter(workspace_id, None, original_document_id=document_ids)
        ),
    )


async def put_document_profiles(workspace_id: str, vectors: Mapping[str, Sequence[float]]) -> None:
    """One profile point per document: its mean chunk embedding."""
    if not vectors or _blocked(workspace_id):
        return
    client, collection = await _client()
    points = [
        PointStruct(
            id=point_id(workspace_id, RECORD_PROFILE, doc_id),
            vector={},
            payload={
                "workspace_id": workspace_id,
                "record": RECORD_PROFILE,
                "original_document_id": doc_id,
                "vector": [float(v) for v in vector],
            },
        )
        for doc_id, vector in vectors.items()
    ]
    await asyncio.to_thread(_upsert, client, collection, points)


async def load_inputs(workspace_id: str) -> Dict[str, Any]:
    """Everything a recompute needs: evidence, profiles, decisions."""
    client, collection = await _client()

    def _read() -> Dict[str, Any]:
        evidence = [
            p.payload or {}
            for p in _scroll(client, collection, _filter(workspace_id, RECORD_EVIDENCE))
        ]
        profiles = {
            str((p.payload or {}).get("original_document_id")): (p.payload or {}).get("vector")
            for p in _scroll(client, collection, _filter(workspace_id, RECORD_PROFILE))
        }
        decisions = {
            str((p.payload or {}).get("key")): {
                "action": str((p.payload or {}).get("action")),
                "match": (p.payload or {}).get("match") or {},
            }
            for p in _scroll(client, collection, _filter(workspace_id, RECORD_DECISION))
        }
        return {"evidence": evidence, "profiles": profiles, "decisions": decisions}

    return await asyncio.to_thread(_read)


def _slim_fact(fact: Mapping[str, Any]) -> Dict[str, Any]:
    """A fact record: its evidence by reference (plus the best passage), not copied."""
    evidence = fact.get("evidence") or []
    return {
        "fact_key": fact["fact_key"],
        "slot_key": fact["slot_key"],
        "claim": fact["claim"],
        "status": fact["status"],
        "shadow_band": fact["shadow_band"],
        "confidence": fact["confidence"],
        "corroboration": fact["corroboration"],
        "source_groups": fact["source_groups"],
        "groups": fact["groups"],
        "conflict_ids": fact.get("conflict_ids") or [],
        "subjects": fact.get("subjects") or [],
        "document_ids": fact["document_ids"],
        "sources": [
            {
                "document_id": row.get("document_id"),
                "chunk_id": row.get("chunk_id"),
                "page": row.get("page"),
                "modality": row.get("modality"),
            }
            for row in evidence
        ],
        "nodes": [row["node"] for row in evidence if row.get("node")][:1],
    }


async def replace_derived(workspace_id: str, result: Mapping[str, Any]) -> None:
    """Write this pass's facts, conflicts and review items; delete the rest.

    New records are written before stale ones are deleted, so a reader never
    sees a case with no facts mid-update.
    """
    if _blocked(workspace_id):
        return
    client, collection = await _client()
    points: List[PointStruct] = []
    for fact in result.get("facts") or ():
        points.append(_derived(workspace_id, RECORD_FACT, fact["fact_key"], _slim_fact(fact)))
    for conflict in result.get("conflicts") or ():
        points.append(_derived(workspace_id, RECORD_CONFLICT, conflict["conflict_id"], conflict))
    for review in result.get("reviews") or ():
        points.append(_derived(workspace_id, RECORD_REVIEW, review["fact_key"], review))
    keep = {str(p.id) for p in points}

    def _write() -> None:
        _upsert(client, collection, points)
        stale = [
            p.id
            for p in _scroll(
                client, collection, _filter(workspace_id, None, record=DERIVED_RECORDS), False
            )
            if str(p.id) not in keep
        ]
        if stale:
            client.delete(
                collection_name=collection,
                points_selector=models.PointIdsList(points=stale),
            )

    await asyncio.to_thread(_write)


def _derived(workspace_id: str, record: str, key: str, payload: Mapping[str, Any]) -> PointStruct:
    return PointStruct(
        id=point_id(workspace_id, record, key),
        vector={},
        payload={
            **payload,
            "workspace_id": workspace_id,
            "record": record,
            "document_ids": list(payload.get("document_ids") or ()),
            "computed_at": _now(),
        },
    )


async def put_decision(
    workspace_id: str,
    key: str,
    action: str,
    decided_by: str = "",
    match: Optional[Mapping[str, Any]] = None,
) -> None:
    """Record a reviewer's resolution, with the signature that re-finds it if
    resolution later re-keys the facts (``router.conflict_signature``)."""
    client, collection = await _client()
    point = PointStruct(
        id=point_id(workspace_id, RECORD_DECISION, key),
        vector={},
        payload={
            "workspace_id": workspace_id,
            "record": RECORD_DECISION,
            "key": key,
            "action": action,
            "match": dict(match or {}),
            "decided_by": decided_by,
            "decided_at": _now(),
        },
    )
    await asyncio.to_thread(_upsert, client, collection, [point])


async def list_records(workspace_id: str, record: str) -> List[Dict[str, Any]]:
    """Every record of one kind for the case (facts, conflicts, reviews...)."""
    client, collection = await _client()
    points = await asyncio.to_thread(_scroll, client, collection, _filter(workspace_id, record))
    rows = [dict(p.payload or {}) for p in points]
    key = {"fact": "fact_key", "conflict": "conflict_id", "review": "fact_key"}.get(record, "key")
    rows.sort(key=lambda r: str(r.get(key) or ""))
    return rows
