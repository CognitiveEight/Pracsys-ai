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

USAGE
    python scripts/reindex_chunks.py --dry-run         # list what would be done
    python scripts/reindex_chunks.py                   # re-index every workspace
    python scripts/reindex_chunks.py --workspace <id>  # one workspace
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


async def _run(args):
    from app.vectorstore.qdrant import vector_store

    await vector_store.initialize()
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
    asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    main()
