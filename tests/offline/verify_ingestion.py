#!/usr/bin/env python3
"""
verify_ingestion.py — Offline checks for the document-ingestion pipeline.

Guards the failure modes that made generated case summaries and strategic
objectives wrong: text lost during PDF extraction, and non-parties promoted to
case participants by the entity extractor.

Covers:
  1. PDF boilerplate strip  — removes running headers, never body text
  2. Non-party filter       — drops diarization placeholders and the
                              document-control metadata of policy annexures

USAGE
    python tests/offline/verify_ingestion.py
    python tests/offline/verify_ingestion.py --pdf path/to/file.pdf   # also dump one PDF's text

REQUIREMENTS
    Run inside the project venv. No API key, network or Qdrant needed.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root

import app.socket.events  # noqa: E402,F401  (import-order workaround for a pre-existing cycle)
from app.processing.file_handlers import (  # noqa: E402
    FileProcessor,
    _strip_repeated_pdf_boilerplate,
)
from app.processing.document_classifier import is_policy_boilerplate  # noqa: E402
from app.socket.event_utils import _format_document_for_prompt  # noqa: E402
from app.socket.events import event_handler as H  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"\n        {detail}" if detail and not ok else ""))


def check_boilerplate_strip():
    print("\n[1] PDF boilerplate strip: headers out, body text intact")

    pages = [f"SCC Online Web Edition, Copyright 2024\nPage {i}\n"
             f"Body text of page {i} here.\nMore body on page {i}."
             for i in range(1, 6)]
    out = _strip_repeated_pdf_boilerplate(pages)
    check("publisher watermark removed", "SCC Online" not in out, out[:100])
    check("repeated long header removed", "Copyright 2024" not in out, out[:100])
    check("body text preserved", "Body text of page 3" in out)

    # The regression this guards: PyPDF2 emits one word per line for many PDFs,
    # so ordinary function words recur on every page. Treating them as running
    # headers deleted them, turning "our mission is to improve the effectiveness
    # and sophistication of partnership" into "our mission improve effectiveness
    # sophistication partnership" — and the model then reconstructs the gaps.
    words = "the quick brown fox and the lazy dog of the manor is to be seen".split()
    out = _strip_repeated_pdf_boilerplate(["\n".join(words) for _ in range(6)])
    for w in ("the", "and", "of", "is", "to", "by" if "by" in words else "fox"):
        check(f"function word {w!r} survives one-word-per-line layout",
              w in out.split(), out[:80])

    pages = [f"Printed for: Advocate Sharma, Delhi High Court Library\nUnique body {i}."
             for i in range(1, 6)]
    out = _strip_repeated_pdf_boilerplate(pages)
    check("long repeated header still removed", "Printed for" not in out, out[:100])
    check("unique body kept", "Unique body 3." in out)


def check_non_party_filter():
    print("\n[2] Entity extraction: only real parties reach EXTRACTED FACTS")

    parties = [
        {"name": "Amit", "designation": "service provider",
         "role_in_matter": "affiliate marketer claiming commissions"},
        {"name": "Ritu", "designation": "colleague",
         "role_in_matter": "participant in the call"},
        {"name": "Performance Horizon Group Limited", "designation": "platform operator",
         "role_in_matter": "withholding the commissions"},
        # Document-control metadata from an uploaded Acceptable Use Policy.
        {"name": "Leigh Carlin", "designation": "Author",
         "role_in_matter": "author of the Acceptable Use Policy version 1.1"},
        {"name": "Jennifer Kilmartin", "designation": "Reviewer",
         "role_in_matter": "reviewed and approved the policy"},
        {"name": "Paul Fellows", "designation": "Chief Operating Officer",
         "role_in_matter": "Approved By in the policy Approvals table"},
        # Diarization placeholders from an audio annexure.
        {"name": "Caller", "designation": "speaker",
         "role_in_matter": "speaker in the audio annexure"},
        {"name": "Receiver", "designation": "speaker",
         "role_in_matter": "the other speaker on the call"},
        {"name": "Speaker A", "designation": "speaker",
         "role_in_matter": "diarization label"},
    ]
    kept = [p["name"] for p in H._drop_non_party_entities(parties)]
    print(f"        kept: {kept}")
    for real in ("Amit", "Ritu", "Performance Horizon Group Limited"):
        check(f"real party kept: {real}", real in kept)
    for bad in ("Caller", "Receiver", "Speaker A"):
        check(f"speaker placeholder dropped: {bad}", bad not in kept)
    for bad in ("Leigh Carlin", "Jennifer Kilmartin", "Paul Fellows"):
        check(f"document-control name dropped: {bad}", bad not in kept)
    check("malformed entries ignored",
          H._drop_non_party_entities([None, {}, {"name": ""}]) == [])
    check("non-list input safe", H._drop_non_party_entities(None) == [])


def check_boilerplate_class():
    print("\n[3] Boilerplate documents are cited instruments, not fact sources")
    tos = ('Terms Of Service\nTerms of Service Agreement is made\nBetween\n'
           'The Client: Any business or agent paying IBRA Digital for a service is '
           'referred to as the Client (the "Buyer"), and\nThe Service Provider- '
           'IBRA Digital Branding Services LLC.')
    privacy = ("Privacy statement\nRevised Effective March 25, 2022\n"
               "Partnerize and its subsidiary companies (together \u201cwe\u201d) respect "
               "your privacy. We collect personal information when you use our services.")
    for name, body in (("Terms of Service.docx", tos), ("Privacy Statement.docx", privacy)):
        check(f"detected as boilerplate: {name}", is_policy_boilerplate(name, body))

    # Real evidence must never be classed as boilerplate.
    for name, body in (
        ("Agreement to Sell.docx",
         "AGREEMENT TO SELL made this 5th day of July 2016 between Sitaram s/o Ram "
         "Prasad (the Vendor) and M/s Srinet Construction Pvt Ltd for Arazi No. 262, "
         "governed by the laws of India."),
        ("FIR 145-2024.pdf",
         "FIR No. 145/2024 under Section 420 BNS at PS Kotwali on the complaint of "
         "Ramesh Kumar against Suresh Singh."),
        ("WhatsApp Audio.mp3",
         "[00:03] Amit: they removed me from the campaign. [00:15] Amit: around 6 lakh "
         "USD is pending, we may need a lawyer."),
    ):
        check(f"evidence NOT flagged: {name}", not is_policy_boilerplate(name, body))

    banner = "CITED INSTRUMENT"
    out = _format_document_for_prompt(
        {"content": tos, "metadata": {"file_name": "Terms of Service.docx",
                                      "document_type": "agreement"}}, 1, 3)
    check("boilerplate annexure carries the CITED INSTRUMENT banner", banner in out,
          out[:160])
    check("banner protects the contracting entity (a party often uploads its own terms)",
          "CONTRACTING ENTITY" in out, out[:400])
    check("banner still excludes document-control individuals",
          "never describe them as" in out.replace("\n", " "), out[:400])
    check("banner sends facts to the other annexures",
          "not from this one" in out.replace("\n", " "), out[:400])
    out = _format_document_for_prompt(
        {"content": "[00:03] Amit: 6 lakh USD pending.",
         "metadata": {"file_name": "WhatsApp Audio.mp3", "document_type": "other"}}, 2, 3)
    check("evidence annexure carries no banner", banner not in out)


def dump_pdf(path):
    text = FileProcessor.extract_text_from_pdf(open(path, "rb").read())
    print(f"\n=== {os.path.basename(path)} — {len(text)} chars ===\n")
    print(text[:3000])


def main():
    check_boilerplate_strip()
    check_non_party_filter()
    check_boilerplate_class()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", help="also extract and print this PDF's text")
    args = ap.parse_args()
    rc = main()
    if args.pdf:
        dump_pdf(args.pdf)
    sys.exit(rc)
