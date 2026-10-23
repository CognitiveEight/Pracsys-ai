---
title: Re-index evidence
type: runbook
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "Before you start"
last_verified: 2026-10-21
jira: SIM-113
risk: HIGH
environments: [staging, production]
---

# Re-index evidence

## In plain words

The AI service keeps a searchable copy of every case document, split into
passages and turned into numbers that a search can compare. Two situations
make us rebuild that copy. Older cases were stored in an earlier layout and
are moved to the current one, either the first time someone uses the case or
all at once by script. And if we ever change the model that turns text into
numbers, every passage must be converted again into a new store, because the
old and new numbers cannot be mixed. This work costs money and writes to the
live database. The risk is high mainly because the old store must be deleted
by hand afterwards: case deletions no longer reach it, so a forgotten copy
keeps data customers asked us to erase.

## Before you start

- Run from a checkout with the **target** environment's `.env` (the script
  writes to the Qdrant named by `QDRANT_URL` and calls the configured
  embedding provider). Agree a window with the release owner.
- Re-embedding calls the embedding API for every passage. It is billed and
  the cost is not attached to any case (see "Known gaps").
- Take a Qdrant snapshot of the collections you will touch.
- Know which situation you are in:

| Situation | What runs |
|---|---|
| **A. Legacy layout.** Chunks still in the original collection (`<QDRANT_COLLECTION_NAME>`) with none in the evidence collection | Automatic on first use: `QdrantStore.ensure_workspace_indexed` in `app/vectorstore/qdrant.py`, called from the retrieval and read paths. Optional bulk run: `scripts/reindex_chunks.py`. |
| **B. Embedding change.** `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL` or `EMBEDDING_DIMENSIONS` changed | `scripts/reindex_chunks.py --from-collection <old evidence collection>`, run with the **new** settings before they serve traffic. |

How collection names work: the evidence collection is
`<QDRANT_COLLECTION_NAME>_evidence_v1<suffix>`, where the suffix comes from
`embedding_space_suffix()` in `app/llm/config.py`. The suffix is empty for the
original space (Gemini or gate0 provider, `gemini-embedding-001`, 768
dimensions) and `__<provider>-<model>-<dims>` for any other. So an embedding
change always produces a new collection name. `QDRANT_CHUNKS_COLLECTION`
overrides the name entirely; if it is set, check it before you start.

## Steps

### A. Bulk re-index of legacy workspaces

#### A1. Dry run

```bash
python scripts/reindex_chunks.py --dry-run
```

Expected: `<N> workspace(s) with legacy chunks in <collection>` and one
`would re-index <workspace id>` line each. Nothing is written.

#### A2. Re-index

```bash
python scripts/reindex_chunks.py                    # every workspace
python scripts/reindex_chunks.py --workspace <id>   # or one workspace
```

Expected: one `[i/N] <id>: ok` line per workspace, then
`Done. <ok> ok, <failed> failed.` A failed workspace is retried automatically
on its first use. Each document is re-chunked from its stored text and
re-embedded. Legacy documents have no page markers, so they gain page numbers
only when re-read (a new upload or a document reload).

### B. Embedding change

#### B1. Configure the new embedding space

Set `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` (and
`EMBED_PRICE_PER_MILLION_TOKENS`) in the environment you run the script from.
Do not deploy the new settings to the serving service yet.

#### B2. Dry run

```bash
python scripts/reindex_chunks.py --from-collection <old evidence collection> --dry-run
```

Expected: `<total> chunk(s) in <old> -> <new> (<dims> dims)`. If it says the
source is the configured collection, the new settings are not in effect. If it
says the collection does not exist, check the name.

#### B3. Copy and re-embed

```bash
python scripts/reindex_chunks.py --from-collection <old evidence collection>
# optional: --workspace <id> to do one case first
```

Expected: progress lines `<done>/<total>`, then
`Done. <n> chunk(s) copied, <m> without a dense vector.` and a reminder to
delete the source. Each chunk keeps its id, text, metadata and exact-term
(sparse) vector; only the dense vector is recomputed.

#### B4. Verify retrieval, then switch traffic

```bash
python scripts/eval_rag.py <golden.jsonl> --out report.json
```

Compare recall@k and MRR with a run on the old space. Then deploy the new
settings to the service.

#### B5. Delete the old evidence collection

Once the new space is serving and checked, delete the old evidence collection
with the Qdrant collection-delete API or console. The tables collection
(`<name>_tables_v1`) and the state collection (`<name>`) do not depend on the
embedding space and stay.

Expected: the old collection is gone from the collection list.

## How to check it worked

- A: a second `--dry-run` lists no workspaces, or only ones that failed.
- B: the point count of the new collection matches the `<total>` from B2, and
  `<m>` (chunks without a dense vector) is 0 or explained.
- A chat question on a known test case returns cited passages.
- B: the old collection no longer exists.

## If something goes wrong / how to undo

- **A, a workspace fails:** nothing is lost. The legacy chunks are still in
  the original collection, `ensure_workspace_indexed` returns false and reads
  fall back to the legacy collection. It retries on first use.
- **B, chunks without a dense vector:** a failed embedding batch is stored
  with `embedding_failed` set. Whole-document reads and exact-term search
  still find those chunks; dense search skips them. Re-run B3 for that
  workspace with `--workspace <id>`; it overwrites the same point ids.
- **B, retrieval worse after the switch:** redeploy the old embedding
  settings. The service reads the old collection again, as long as you have
  not deleted it yet. Do not delete in B5 until you are sure.
- **B, a case was deleted during the window:** a purge deletes only from the
  collections the running configuration names. If a case was purged while the
  other collection existed, its passages are still there. Delete the old
  collection (B5), or delete that workspace's points from it by
  `workspace_id`.

### Known gaps

- Re-embedding cost from this script is not billed to any case. It runs
  outside an inbound event, so no workspace is bound in the logging context.
- In B3 one failed embedding call marks the whole batch of 100 chunks
  `embedding_failed` (stored with a placeholder zero dense vector, which dense
  search excludes by the flag). The live write path in `add_document_chunks`
  retries chunk by chunk first; the script does not.
- There is no script to delete the old collection. B5 is manual.
- Neo4j graph facts point at chunk ids, which B keeps unchanged. Not verified
  end to end.

## Who to call (roles)

- AI team: runs the script and the evaluation.
- Release owner: agrees the window and the switch of settings.
- Tech lead: approves deleting the old collection.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
