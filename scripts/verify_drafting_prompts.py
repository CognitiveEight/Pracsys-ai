"""Drafting-prompt verification runner (offline, no Gemini, no I/O).

Guards the correspondence-vs-court-filing document class gate: a legal
notice must be argued from the contract and the record, while a petition
or bail application must still cite the provision it is filed under.

`app/prompts/drafting.py` has no runtime dependencies beyond the standard
library, so the whole prompt surface is checkable without a backend, a socket
connection, or an API key.

Usage:

    python scripts/verify_drafting_prompts.py

Exits non-zero if any assertion fails, so CI can wire this up later.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.prompts.drafting import (  # noqa: E402
    DOC_CLASS_CORRESPONDENCE,
    DOC_CLASS_FILING,
    DOC_CLASS_UNKNOWN,
    DRAFT_RENDER_INSTRUCTION,
    DRAFTING_SYSTEM_INSTRUCTION,
    _build_style_block,
    build_draft_render_prompt,
    build_suggested_drafts_prompt,
    build_template_applicability_prompt,
    infer_document_class,
)

FAILURES: list = []


def check(label: str, condition: bool) -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label)
    if not condition:
        FAILURES.append(label)


def render(
    draft_title: str,
    *,
    template_document_type: str = "",
    template_text=None,
    guardrail_level: str = "Comprehensive",
) -> str:
    return build_draft_render_prompt(
        role="PETITIONER",
        case_summary="Client's dues under a service agreement remain unpaid.",
        case_facts_brief="Agreement dated 25.10.2024; ₹12,37,913 outstanding.",
        template_name="Template" if template_text else None,
        template_text=template_text,
        gathered_facts_json="{}",
        draft_title=draft_title,
        lawyer_instructions=None,
        mode="template" if template_text else "blank",
        guardrail_level=guardrail_level,
        template_document_type=template_document_type or None,
    )


NOTICE_TITLE = "Legal Notice for Breach of the Service Agreement dated 12.03.2024"
BAIL_TITLE = "Bail Application under Section 483 BNSS"
FAKE_NOTICE_TEMPLATE = (
    "ADVOCATE & CO.\n\nTO,\n<Recipient>\n\nSUBJECT: LEGAL NOTICE\n\n"
    "Dear Sir/Madam,\n1. Under instructions from my client...\n"
    "2. You are called upon to pay within 15 days.\n\nYours faithfully,\n"
)


def main() -> int:
    print("\n1. Document class inference")
    cases = [
        (NOTICE_TITLE, "", DOC_CLASS_CORRESPONDENCE),
        ("Reply to Legal Notice dated 20 July 2026", "", DOC_CLASS_CORRESPONDENCE),
        ("Notice invoking arbitration", "", DOC_CLASS_CORRESPONDENCE),
        ("Cheque bounce notice under Section 138", "", DOC_CLASS_CORRESPONDENCE),
        (BAIL_TITLE, "", DOC_CLASS_FILING),
        ("Writ Petition against the impugned order", "", DOC_CLASS_FILING),
        ("Consumer Complaint for deficiency in service", "", DOC_CLASS_FILING),
        # Template classification carries an uninformative title.
        ("Draft for Mr. Sharma", "Reply Notice", DOC_CLASS_CORRESPONDENCE),
        ("Draft for Mr. Sharma", "Writ Petition", DOC_CLASS_FILING),
        # Fail-safe: a filing signal from EITHER source wins.
        ("Bail Application", "Legal Notice", DOC_CLASS_FILING),
        # Not in either label table, but the title reads as a court document.
        ("Affidavit of the deponent", "", DOC_CLASS_FILING),
        # Neither signal resolves -> unchanged behaviour.
        ("Draft for Mr. Sharma", "", DOC_CLASS_UNKNOWN),
        ("", "", DOC_CLASS_UNKNOWN),
    ]
    for title, tdt, expected in cases:
        got = infer_document_class(title, tdt)
        check(f"{title or '(empty)'!r} + {tdt or '(none)'!r} -> {expected}", got == expected)

    print("\n2. Correspondence discipline reaches a notice render")
    notice = render(NOTICE_TITLE)
    for marker in (
        "CORRESPONDENCE DISCIPLINE",
        "STATUTES ONLY WHEN LOAD-BEARING",
        "quote the CLAUSE NUMBER",
        "itemised demand list",
        "absolute date",
    ):
        check(f"notice prompt contains {marker!r}", marker in notice)

    print("\n3. Court filings are untouched")
    bail = render(BAIL_TITLE)
    for marker in ("CORRESPONDENCE DISCIPLINE", "LOAD-BEARING"):
        check(f"bail prompt omits {marker!r}", marker not in bail)
    check(
        "bail prompt keeps the court-petition skeleton guidance",
        "court-petition skeleton" in bail or "court heading" in bail,
    )
    check(
        "bail prompt is byte-identical to the no-class render",
        bail == build_draft_render_prompt(
            role="PETITIONER",
            case_summary="Client's dues under a service agreement remain unpaid.",
            case_facts_brief="Agreement dated 25.10.2024; ₹12,37,913 outstanding.",
            template_name=None,
            template_text=None,
            gathered_facts_json="{}",
            draft_title=BAIL_TITLE,
            lawyer_instructions=None,
            mode="blank",
            guardrail_level="Comprehensive",
        ),
    )

    print("\n4. Template mode: content rule, not structure rule")
    templated = render(
        "Draft for Mr. Sharma",
        template_document_type="Legal Notice",
        template_text=FAKE_NOTICE_TEMPLATE,
    )
    check("template notice gets the discipline", "CORRESPONDENCE DISCIPLINE" in templated)
    check("template notice gets the fidelity rider", "TEMPLATE MODE + CORRESPONDENCE" in templated)
    check(
        "an existing statutory-grounds paragraph is preserved",
        "that paragraph STAYS and you fill it" in templated,
    )
    check(
        "the template contract survives",
        "the template's skeleton is a CONTRACT" in templated,
    )

    print("\n5. Guardrail wording is class-aware")
    corr_guard = _build_style_block(None, None, "Comprehensive", DOC_CLASS_CORRESPONDENCE)
    filing_guard = _build_style_block(None, None, "Comprehensive", DOC_CLASS_FILING)
    check(
        "correspondence guardrail verifies the record",
        "SUPPORTED BY THE INSTRUMENT AND THE RECORD" in corr_guard,
    )
    check(
        "correspondence guardrail no longer demands citation checks",
        "verify every statute/section citation" not in corr_guard,
    )
    check(
        "filing guardrail is unchanged",
        "verify every statute/section citation" in filing_guard,
    )

    print("\n6. Base instruction / registry")
    for marker in (
        "LOAD-BEARING STATUTE TEST",
        "WITHOUT PREJUDICE",
        "ITEMISED DEMAND",
        # Court-filing scaffolding must survive the registry rewrite.
        "MOST RESPECTFULLY SHOWETH",
        "VERIFICATION",
    ):
        check(f"system instruction contains {marker!r}", marker in DRAFTING_SYSTEM_INSTRUCTION)
    check(
        "notices are told not to open a statutory-grounds paragraph",
        '"Legal Grounds"' in DRAFTING_SYSTEM_INSTRUCTION,
    )
    check(
        "the old blanket 'cite applicable provisions' rule is gone",
        "cite\n  applicable provisions" not in DRAFTING_SYSTEM_INSTRUCTION
        and "→ cite applicable provisions" not in DRAFTING_SYSTEM_INSTRUCTION,
    )

    print("\n7. Render instruction depth mandate")
    check(
        "depth bullet is class-aware",
        "PRE-LITIGATION CORRESPONDENCE" in DRAFT_RENDER_INSTRUCTION,
    )
    check(
        "IPC/CrPC prohibition survives",
        "never IPC / CrPC / Evidence Act" in DRAFT_RENDER_INSTRUCTION,
    )

    print("\n8. Upstream title anchors")
    suggestions = build_suggested_drafts_prompt(
        role="PETITIONER",
        case_summary="",
        applicable_laws_brief="",
        strategy_brief="",
        analysis_brief="",
        list_of_dates_brief="",
        template_library_brief="",
    )
    applicability = build_template_applicability_prompt(
        role="PETITIONER",
        case_summary="",
        applicable_laws_brief="",
        strategy_brief="",
        analysis_brief="",
        list_of_dates_brief="",
        template_document_type="Legal Notice",
        template_legal_purpose="Recovery of contractual dues.",
    )
    for name, prompt in (("suggestions", suggestions), ("applicability", applicability)):
        check(
            f"{name} prompt drops the statute-titled notice exemplar",
            "Legal Notice under Section 35 of the Consumer Protection Act" not in prompt,
        )
        check(
            f"{name} prompt keeps the statute rule for court filings",
            "For COURT FILINGS, include the enabling statute" in prompt,
        )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All drafting-prompt checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
