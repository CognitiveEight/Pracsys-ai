---
paths:
  - "app/rag/**/*.py"
  - "app/vectorstore/**/*.py"
  - "app/ontology/**/*.py"
  - "app/graph/**/*.py"
  - "app/retrieval/**/*.py"
  - "app/workspace/**/*.py"
---

# Retrieval, storage and citations

Full detail: CLAUDE.md "Retrieval and citations", "Knowledge graph",
"Deleted documents must stay visible as gaps".

- Write chunks only through `add_document_chunks` (deterministic ids,
  replace-per-document).
- Delete only through `delete_documents` / `delete_workspace`: they clear the
  evidence, tables and graph stores together.
- Embeddings are never padded, truncated or zero-filled; a failure is
  flagged `embedding_failed`.
- New prompt content builders use `fit_to_budget`; never cut documents from
  the front.
- Nothing is cited that was not checked: panel citations are attached only at
  `_emit_document_feature`; fields they add are additive.
- The knowledge graph is optional and augment-only; it must never gate a
  response.
- New per-workspace state must be reached by the workspace purge.
- Coverage: `tests/offline/verify_rag.py` (store checks need a local Qdrant).
