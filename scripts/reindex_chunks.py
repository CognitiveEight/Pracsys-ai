#!/usr/bin/env python3
"""
Bulk re-index legacy document chunks into the evidence collection.

Workspaces are re-indexed automatically the first time the service touches
them (QdrantStore.ensure_workspace_indexed). This script does it for every
workspace up front — useful before a release, so the first chat turn on each
old workspace does not pay the one-off re-indexing delay.

Re-indexing re-chunks each document's stored text in the correct order and
re-embeds it, so it CALLS THE EMBEDDING API (billed) and writes to the Qdrant
configured in .env. Legacy documents gain page numbers only when re-read (a
new upload or document_reload_request); their text has no page markers.

CHANGING THE EMBEDDING MODEL (docs/MODEL_PORTABILITY.md)
Each embedding space (EMBEDDING_PROVIDER + EMBEDDING_MODEL + EMBEDDING_DIMENSIONS)
has its own evidence collection. ``--from-collection`` copies every chunk of an
existing evidence collection into the configured one, keeping its id, text,
metadata and exact-term vector, and re-embedding only the dense vector. Run it
with the NEW settings, before the new settings serve traffic. Afterwards delete
the old collection yourself: a workspace purge only reaches the configured
collections, so a stale copy would keep deleted case data.

USAGE
    python scripts/reindex_chunks.py --dry-run         # list what would be done
    python scripts/reindex_chunks.py                   # re-index every workspace
    python scripts/reindex_chunks.py --workspace <id>  # one workspace
    python scripts/reindex_chunks.py --from-collection simplertoday_evidence_v1
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _workspaces(store):
    from qdrant_client.models import Filter, IsEmptyCondition, PayloadField

    ids = set()
    offset = None

    all_chunks = Filter(must=[IsEmptyCondition(is_empty=PayloadField(key="cache_kind"))])
    while True:
        batch, offset = await asyncio.to_thread(
            store.client.scroll,
            collection_name=store.collection_name,
            scroll_filter=all_chunks,
            limit=500,
            offset=offset,
            with_payload=["workspace_id"],
            with_vectors=False,
        )
        ids.update(str((p.payload or {}).get("workspace_id") or "") for p in batch)
        if offset is None:
            break
    ids.discard("")
    return sorted(ids)


async def _reembed(store, source, args):
    """Copy ``source`` chunks into the configured evidence collection with
    dense vectors from the configured embedding model."""
    from qdrant_client.models import (FieldCondition, Filter, MatchValue,
                                      PointStruct)

    from app.ai.gemini_client import gemini_client
    from app.vectorstore.qdrant import DENSE_VECTOR, SPARSE_VECTOR

    if source == store.chunks_collection:
        raise SystemExit("--from-collection is the configured collection; nothing to do")
    if not store.client.collection_exists(source):
        raise SystemExit(f"collection {source!r} does not exist")
    scope = (
        Filter(must=[FieldCondition(key="workspace_id", match=MatchValue(value=args.workspace))])
        if args.workspace else None
    )
    total = store.client.count(collection_name=source, count_filter=scope, exact=True).count
    print(f"{total} chunk(s) in {source} -> {store.chunks_collection} "
          f"({store.vector_size} dims)")
    if args.dry_run:
        return
    done = failed = 0
    offset = None
    while True:
        batch, offset = await asyncio.to_thread(
            store.client.scroll,
            collection_name=source,
            scroll_filter=scope,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=[SPARSE_VECTOR],
        )
        if not batch:
            break
        texts = [str((p.payload or {}).get("content") or " ") for p in batch]
        try:
            dense = await gemini_client.embed_texts(texts)
        except Exception as exc:  # keep the chunk; dense search skips it
            print(f"  batch embedding failed ({exc}); storing as embedding_failed")
            dense = [None] * len(batch)
        points = []
        for p, vector in zip(batch, dense):
            payload = dict(p.payload or {})
            meta = dict(payload.get("metadata") or {})
            vectors = {DENSE_VECTOR: vector or [0.0] * store.vector_size}
            sparse = (p.vector or {}).get(SPARSE_VECTOR) if isinstance(p.vector, dict) else None
            if sparse is not None:
                vectors[SPARSE_VECTOR] = sparse
            if vector is None:
                meta["embedding_failed"] = True
                failed += 1
            else:
                meta.pop("embedding_failed", None)
            payload["metadata"] = meta
            points.append(PointStruct(id=p.id, vector=vectors, payload=payload))
        await asyncio.to_thread(
            store.client.upsert, collection_name=store.chunks_collection, points=points
        )
        done += len(points)
        print(f"  {done}/{total}")
        if offset is None:
            break
    print(f"Done. {done} chunk(s) copied, {failed} without a dense vector.")
    print(f"Delete {source} once the new space is serving (workspace purges do not reach it).")


async def _run(args):
    from app.vectorstore.qdrant import vector_store

    await vector_store.initialize()
    if args.from_collection:
        await _reembed(vector_store, args.from_collection, args)
        return
    targets = [args.workspace] if args.workspace else await _workspaces(vector_store)
    print(f"{len(targets)} workspace(s) with legacy chunks in {vector_store.collection_name}")
    if args.dry_run:
        for ws in targets:
            print(f"  would re-index {ws}")
        return
    failed = 0
    for i, ws in enumerate(targets, 1):
        ok = await vector_store.ensure_workspace_indexed(ws)
        failed += 0 if ok else 1
        print(f"  [{i}/{len(targets)}] {ws}: {'ok' if ok else 'FAILED (will retry on first use)'}")
    print(f"Done. {len(targets) - failed} ok, {failed} failed.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workspace")
    ap.add_argument(
        "--from-collection",
        help="re-embed this evidence collection into the configured embedding space",
    )
    asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    main()
