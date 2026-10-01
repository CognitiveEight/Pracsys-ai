#!/usr/bin/env python3
"""
Measure retrieval and citation quality on a golden set.

Compares two retrieval configurations on the same questions:

* ``baseline`` — dense-only style: hybrid search without query rewriting or
  reranking (closest to the old single-query behaviour);
* ``full`` — the new pipeline: query rewrite + hybrid search + rerank +
  diversity (app/rag/retriever.py).

With ``--answers`` it also generates a cited answer per question from the
retrieved passages and scores citation precision, citation coverage and the
unsupported-figure rate after the citation checker (app/rag/citations.py).

REQUIREMENTS
    * The workspaces named in the golden set must already be ingested into
      the Qdrant configured in .env (evidence collection).
    * GOOGLE_API_KEY for query embeddings, rewriting, reranking and answers.
      Every call is billed like production traffic.

USAGE
    python scripts/eval_rag.py data/eval/golden.jsonl
    python scripts/eval_rag.py data/eval/golden.jsonl --answers --k 5 --out report.json
    python scripts/eval_rag.py data/eval/golden.jsonl --graph   # with the knowledge graph

GOLDEN SET FORMAT
    See app/rag/eval/metrics.py (one JSON object per line). Build it from real,
    anonymised cases with the lawyers: the question, the passage(s) that answer
    it, and the facts a correct answer must state.
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.rag.eval.metrics import (  # noqa: E402
    citation_coverage,
    citation_precision,
    mean,
    recall_at_k,
    reciprocal_rank,
)


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip() and not line.startswith("#")]


async def _answer(case, hits):
    """A cited answer from the retrieved passages, checked like chat's."""
    from app.ai.gemini_client import gemini_client, is_generation_error
    from app.rag.citations import verify_and_number
    from app.rag.evidence import EvidencePack, render_document_block

    pack = EvidencePack()
    items = [pack.add_passage(h, annexure=None, file_name=None) for h in hits]
    block = render_document_block([i for i in items if i is not None])
    prompt = (
        "Answer the question using ONLY the passages. After every sentence that "
        "states a fact from them, put the label(s) of the supporting passage(s), "
        "e.g. [E2]. If the passages do not contain the answer, say so.\n\n"
        f"PASSAGES:\n{block}\n\nQUESTION: {case['question']}"
    )
    raw = await gemini_client.generate(prompt, temperature=0.2, max_tokens=1500, thinking_level="HIGH")
    if is_generation_error(raw):
        return None
    text, citations, report = verify_and_number(raw, pack)
    return {
        "answer": text,
        "citations": citations,
        "unsupported": len(report.unsupported),
        "invalid_labels": len(report.invalid_labels),
    }


async def _graph_hits(graph_store, case):
    from app.retrieval.panel_bridge import retrieve_for_panel
    from app.vectorstore.qdrant import vector_store

    evidence = await retrieve_for_panel(
        "intelligence_chat", case["workspace_id"], graph_store=graph_store, question=case["question"]
    )
    chunk_ids = {e.get("chunkId") for e in evidence.evidence if e.get("modality") == "graph"}
    if not chunk_ids:
        return []
    chunks = await vector_store.get_document_chunks(case["workspace_id"])
    return [c for c in chunks if c.get("document_id") in chunk_ids]


async def _graph_metrics(graph_store, case):
    from app.rag.eval.metrics import chronology_completeness, entity_resolution_precision

    out = {}
    if case.get("parties"):
        rows = await graph_store.query("party_network", case["workspace_id"])
        parties = [{"canonical_name": r.get("canonical_name") or r.get("name"), "aliases": r.get("aliases") or []}
                   for r in rows if r.get("canonical_name") or r.get("name")]
        out["entity_resolution"] = entity_resolution_precision(parties, case["parties"])
    if case.get("chronology"):
        rows = await graph_store.query("chronology", case["workspace_id"])
        events = [{"date": r.get("event_date") or r.get("date"), "description": r.get("description")} for r in rows]
        out["chronology"] = chronology_completeness(events, case["chronology"])
    return out


async def _run(args):
    from app.core.knowledge_plane import setup_knowledge_plane
    from app.rag.retriever import retrieve_passages

    # With NEO4J_URL set, "full" includes graph-fact passages and the graph
    # metrics run; without it the comparison is hybrid-only.
    graph_store = await setup_knowledge_plane(strict=False) if args.graph else None
    cases = _load(args.golden)
    rows = []
    for case in cases:
        row = {"id": case.get("id"), "question": case["question"]}
        for name, opts in (("baseline", {"use_rewrite": False, "use_rerank": False}), ("full", {})):
            hits = await retrieve_passages(case["workspace_id"], case["question"], limit=args.k * 2, **opts)
            if name == "full" and graph_store is not None:
                # "full" + graph: the source passages of the graph facts the
                # chat profile retrieves, appended after the hybrid hits.
                hits = hits + await _graph_hits(graph_store, case)
            row[name] = {
                "recall": recall_at_k(hits, case.get("expected") or [], args.k),
                "rr": reciprocal_rank(hits, case.get("expected") or []),
            }
            if args.answers and name == "full":
                ans = await _answer(case, hits[: args.k * 2])
                if ans:
                    row["answer"] = {
                        "citation_precision": citation_precision(ans["citations"], case),
                        "citation_coverage": citation_coverage(ans["answer"]),
                        "unsupported": ans["unsupported"],
                        "invalid_labels": ans["invalid_labels"],
                        "text": ans["answer"],
                    }
        if graph_store is not None:
            row["graph"] = await _graph_metrics(graph_store, case)
        rows.append(row)
        print(f"- {row['id']}: recall@{args.k} baseline={row['baseline']['recall']:.2f} "
              f"full={row['full']['recall']:.2f}")

    summary = {
        "cases": len(rows),
        f"recall@{args.k}": {m: mean(r[m]["recall"] for r in rows) for m in ("baseline", "full")},
        "MRR": {m: mean(r[m]["rr"] for r in rows) for m in ("baseline", "full")},
    }
    answered = [r["answer"] for r in rows if r.get("answer")]
    if answered:
        summary["citation_precision"] = mean(a["citation_precision"] for a in answered)
        summary["citation_coverage"] = mean(a["citation_coverage"] for a in answered)
        summary["unsupported_rate"] = sum(1 for a in answered if a["unsupported"]) / len(answered)
    print(json.dumps(summary, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "cases": rows}, fh, indent=2, ensure_ascii=False)
        print(f"Report written to {args.out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("golden", help="golden set, JSON Lines")
    ap.add_argument("--k", type=int, default=5, help="cut-off for recall@k (default 5)")
    ap.add_argument("--answers", action="store_true", help="also generate and score cited answers")
    ap.add_argument("--out", help="write the full report as JSON")
    ap.add_argument("--graph", action="store_true",
                    help="include knowledge-graph evidence and metrics (needs NEO4J_URL)")
    asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    main()
