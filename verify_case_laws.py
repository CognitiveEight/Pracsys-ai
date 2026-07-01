#!/usr/bin/env python3
"""
verify_case_laws.py — Standalone tester for the Case Laws feature.

Feed in a case (a .txt file or pasted text) and it runs the SAME code path the
app uses — Indian Kanoon + Vertex AI Search + Gemini google_search grounding,
then the strict source-verified LLM ranking — and prints the resulting case-law
precedents WITH their source URLs so you can click through and verify each one.

USAGE
-----
  # 1) From a text file containing your case summary:
  python verify_case_laws.py my_case.txt

  # 2) Paste / pipe the case text on stdin:
  python verify_case_laws.py < my_case.txt
  cat my_case.txt | python verify_case_laws.py

  # 3) Inline text:
  python verify_case_laws.py --text "Trademark infringement dispute over the mark BLOOM ..."

OPTIONS
  --query "<words>"   Override the search query sent to Indian Kanoon / grounding
                      (otherwise it is derived from the case text). Use a short,
                      focused legal phrase for the best precedents.
  --role  <ROLE>      Workspace role: ADJUDICATOR | PETITIONER | RESPONDENT |
                      MEDIATOR (default: ADJUDICATOR). Affects only tone.
  --out   <file.json> Also write the raw JSON payload to this file.

REQUIREMENTS
  - Run inside the project venv (so app deps are importable).
  - A valid GOOGLE_API_KEY in .env (needed for the LLM ranking + grounding).
    Indian Kanoon retrieval works even without a key, but the final ranked
    panel needs the model.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Optional

# Remember where the user invoked us from BEFORE we chdir, so relative file
# paths (e.g. "my_case.txt") resolve against the user's directory, not the
# project root.
_INVOCATION_CWD = os.getcwd()

# Make `app.*` importable and ensure .env is found, regardless of where this is
# run from (this file lives at the project root).
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)


# A compact stopword set mirroring the app's signal extraction so the derived
# query keeps the legally-meaningful terms.
_STOPWORDS = {
    "this",
    "that",
    "with",
    "from",
    "have",
    "into",
    "such",
    "their",
    "there",
    "which",
    "would",
    "about",
    "against",
    "under",
    "through",
    "between",
    "case",
    "claim",
    "court",
    "petitioner",
    "respondent",
    "plaintiff",
    "defendant",
    "matter",
    "notice",
    "party",
    "parties",
    "shall",
    "also",
    "been",
    "being",
    "they",
    "them",
    "these",
    "those",
    "where",
    "while",
    "after",
    "before",
    "issued",
    "alleging",
    "states",
    "stated",
    "regarding",
    "involves",
    "involve",
    "company",
    "limited",
    "private",
    "pvt",
    "ltd",
    "the",
    "and",
    "for",
    "are",
}


def _derive_query(text: str) -> str:
    """Build a focused search query from the case text (top legal keywords)."""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", text.lower())
    freq: dict = {}
    for tok in tokens:
        if tok in _STOPWORDS:
            continue
        freq[tok] = freq.get(tok, 0) + 1
    ranked = sorted(freq, key=lambda k: (-freq[k], k))[:10]
    if ranked:
        return " ".join(ranked)
    # Fallback: every token was a stopword/too short — keep the first few words
    # so a very short input still yields a usable query.
    return " ".join(text.split()[:8])


def _resolve_input_path(value: str):
    """Return an existing file path for ``value`` (resolved against the user's
    original cwd or the project root), or None if it is not a file."""
    candidates = []
    if os.path.isabs(value):
        candidates.append(value)
    else:
        candidates.append(os.path.join(_INVOCATION_CWD, value))
        candidates.append(os.path.join(_PROJECT_ROOT, value))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _looks_like_path(value: str) -> bool:
    """Heuristic: does this positional arg look like a filename the user meant
    to point at (so a missing file is a clear error, not silent text)?"""
    lowered = value.lower()
    return (
        os.sep in value
        or "/" in value
        or lowered.endswith((".txt", ".md", ".text", ".json"))
    )


def _read_case_text(args) -> str:
    if args.text:
        return args.text.strip()
    if args.input:
        path = _resolve_input_path(args.input)
        if path:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        if _looks_like_path(args.input):
            raise FileNotFoundError(
                f"File not found: {args.input}\n"
                f"(looked in {_INVOCATION_CWD} and {_PROJECT_ROOT})"
            )
        # Not file-like → treat the positional value itself as the case text.
        return args.input.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return ""


async def _run(case_text: str, query: Optional[str], role: str) -> dict:
    # Imported here so argparse/help works without loading the whole app.
    from app.socket.events import event_handler
    from app.search.case_law_finder import find_case_law_sources

    signals = {
        "workspaceTitle": case_text.splitlines()[0][:120] if case_text else "",
        "query": case_text[:400],
        "keywords": [],
        "evidenceSnippet": case_text[:5000],
    }

    # No explicit --query → use the SAME legal-issue query derivation the app
    # uses (doctrine/statute/remedy terms, party names stripped), so the verify
    # tool mirrors production behaviour. Local keyword extraction is the fallback.
    if not query:
        query = await event_handler._derive_case_law_query(signals)
    if not query:
        query = _derive_query(case_text)
    signals["query"] = query
    signals["keywords"] = query.split()

    print("\n" + "=" * 72)
    print("SEARCH QUERY:", query)
    print("=" * 72)

    sources, research_notes = await find_case_law_sources(query)
    print(f"\nSTAGE 1 — RETRIEVED SOURCES: {len(sources)}")
    print("-" * 72)
    for i, s in enumerate(sources, 1):
        print(f"{i:>2}. [{s.get('sourceName', '?')}] {s.get('url', '')}")
        title = (s.get("title") or "").strip()
        if title:
            print(f"    {title[:110]}")

    payload = await event_handler._generate_case_laws_from_sources(
        workspace_id="verify-cli",
        workspace_role=role,
        signals=signals,
        search_results=sources,
        research_notes=research_notes,
    )
    cases = payload.get("caseLaws", [])

    print("\n" + "=" * 72)
    print(f"STAGE 2 — CASE LAWS (source-verified): {len(cases)}")
    print("=" * 72)
    if not cases:
        print(
            "\n(No verified case laws produced. If STAGE 1 found sources, this\n"
            " usually means the GOOGLE_API_KEY is missing/invalid so the ranking\n"
            " model could not run — check your .env.)"
        )
    for c in cases:
        _print_digest(c)
    return payload


def _print_digest(c: dict) -> None:
    """Render one case as a legal-digest/headnote card."""
    bar = "─" * 64

    def field(label, value):
        if value:
            print(f"  {label}: {value}")

    def bullets(label, items):
        items = [i for i in (items or []) if str(i).strip()]
        if items:
            print(f"  {label}:")
            for i in items:
                print(f"    • {i}")

    stars = c.get("bindingStars")
    star_str = ("★" * stars + "☆" * (5 - stars)) if isinstance(stars, int) else ""
    cite = " | ".join(p for p in [c.get("citation"), c.get("equivalentCitation")] if p)

    print("\n" + "═" * 66)
    print(f"  {c.get('title', 'N/A')}")
    if cite:
        print(f"  {cite}")
    weight = " ".join(p for p in [c.get("court"), star_str] if p)
    binding = c.get("bindingStrength")
    if binding:
        weight = f"{weight}   [{binding}]"
    if weight.strip():
        print(f"  {weight}")
    print("═" * 66)

    field("Decision date", c.get("date"))
    field("Bench", ", ".join(c.get("bench") or []))
    field("Area of law", " · ".join(c.get("areaOfLaw") or []))
    field("Relevance", c.get("relevance"))
    conf = c.get("confidence")
    field("AI confidence", f"{conf}%" if conf is not None else None)
    print(bar)
    bullets("Legal issues", c.get("legalIssues"))
    bullets("Material facts", c.get("materialFacts"))
    field("Ratio decidendi", c.get("ratio"))
    field("Holding", c.get("holding"))
    bullets("Relief granted", c.get("relief"))
    print(bar)
    field("Summary", c.get("summary"))
    bullets("Matched facts (this matter)", c.get("matchedFacts"))
    field("Why relevant", c.get("applicabilityReason"))
    bullets("Distinguishing features", c.get("distinguishing"))
    field("⚠ Adverse impact", c.get("adverseImpact"))
    print(bar)
    field("Legal status", c.get("legalStatus"))
    print(f"  🔗 {c.get('sourceUrl', '')}  ({c.get('sourceName', '?')})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify the Case Laws feature against a case you provide.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input", nargs="?", help="Path to a .txt case file (or raw text)."
    )
    parser.add_argument("--text", help="Inline case text.")
    parser.add_argument("--query", help="Override the search query.")
    parser.add_argument("--role", default="ADJUDICATOR", help="Workspace role.")
    parser.add_argument("--out", help="Write the raw JSON payload to this file.")
    args = parser.parse_args()

    # Quieten the app's startup/info logs so the report is readable.
    try:
        from loguru import logger

        logger.remove()
        logger.add(sys.stderr, level="WARNING")
    except Exception:
        pass

    try:
        case_text = _read_case_text(args)
    except FileNotFoundError as e:
        parser.error(str(e))
    if not case_text and not args.query:
        parser.error(
            'No case provided. Pass a .txt file, --text "...", pipe text on '
            "stdin, or give --query."
        )

    # query may be None → _run() derives a legal-issue query like the app does.
    query = args.query.strip() if args.query else None

    payload = asyncio.run(_run(case_text, query, args.role.strip().upper()))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"\nRaw JSON written to: {args.out}")


if __name__ == "__main__":
    main()
