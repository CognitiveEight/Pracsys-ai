"""Prompt builders for the extraction feature (extracted from handlers, verbatim)."""


__all__ = [
    "extract_parties_subagent_prompt",
    "extract_timeline_subagent_prompt",
    "extract_assets_and_law_subagent_prompt",
]


def extract_parties_subagent_prompt(*, combined_document_content):
    return (
"You are a focused entity extractor for legal documents. Your ONLY "
            "job is to list every PARTY (person, firm, company, government "
            "office) that appears in the documents below.\n\n"
            "Return ONE JSON object with this shape (no commentary, no markdown):\n\n"
            "{\n"
            '  "parties": [\n'
            '    { "name": "exact name as written in the documents", '
            '"designation": "role / community / status as written (e.g. plaintiff, defendant, vendor, vendee, complainant, M/s ..., advocate, SDM, Sub-Registrar, member of Scheduled Caste community)", '
            '"role_in_matter": "short description of why this party matters (e.g. original owner, registered purchaser, civil-suit plaintiff)", '
            '"first_mention_doc": "Annexure N or document filename" }\n'
            "  ]\n"
            "}\n\n"
            "STRICT RULES:\n"
            "- Use names EXACTLY as written. Do NOT collapse 'Smt. Premlata' "
            "to 'Premlata' or 'Munna Lal' to 'Munnalal'. Write what the "
            "documents write, including honorifics (Smt., Shri, Dr., M/s).\n"
            "- Include every distinct party — original owners, current owners, "
            "purchasers, advocates, court officials, witnesses, official "
            "signatories — if they are named in the documents.\n"
            "- Never write 'Party No. 1' / 'Party No. 2' / 'the plaintiff' / "
            "'the petitioner' / 'the respondent' placeholders. If you cannot "
            "find a real name for an entry, DROP the entry.\n"
            "- Reference uploaded files only as Annexure N.\n"
            "- Return ONLY valid JSON. No commentary, no markdown fences.\n\n"
            "DOCUMENTS:\n"
            f"{combined_document_content}"
    )


def extract_timeline_subagent_prompt(*, combined_document_content):
    return (
"You are a focused timeline extractor for legal documents. Your "
            "ONLY job is to extract every DATED EVENT and every SUIT/CASE/FIR "
            "number that appears in the documents below.\n\n"
            "Return ONE JSON object with this shape (no commentary, no markdown):\n\n"
            "{\n"
            '  "dates": [\n'
            '    { "date_iso": "YYYY-MM-DD", '
            '"event": "what happened on this date — name the actor, the counterparty, the action (e.g. \\"Sitaram executed a registered agreement to sell Arazi No. 262 to M/s Srinet & Sandhilya Construction Pvt. Ltd.\\")", '
            '"actors": ["names exactly as written"], '
            '"source_doc": "Annexure N or document filename" }\n'
            "  ],\n"
            '  "suit_numbers": [\n'
            '    { "number": "exact case/suit/FIR identifier as written (e.g. O.S. No. 827/2019, Case No. 13779, FIR No. 145/2024)", '
            '"court": "court / forum where filed", '
            '"filed_by": "name exactly as written", '
            '"against": ["names"], '
            '"filed_on": "YYYY-MM-DD or null", '
            '"subject_matter": "what the suit is about" }\n'
            "  ]\n"
            "}\n\n"
            "STRICT RULES:\n"
            "- Walk the documents chronologically. Include EVERY dated event "
            "with a clear actor and action — agreements, notices, registrations, "
            "sale deeds, FIRs, filings, hearings, orders, judgments, payments.\n"
            "- Use Indian DD/MM/YYYY convention when parsing dates: '02/12/2008' "
            "is 2 December 2008 (date_iso = '2008-12-02').\n"
            "- For suit/case/FIR numbers: include EVERY one you can find with "
            "its exact identifier verbatim. Do not normalise 'O.S. No. 827/2019' "
            "to 'OS 827 2019' — keep the source punctuation.\n"
            "- Use names EXACTLY as written. Never invent or generalise.\n"
            "- Reference uploaded files only as Annexure N.\n"
            "- Return ONLY valid JSON.\n\n"
            "DOCUMENTS:\n"
            f"{combined_document_content}"
    )


def extract_assets_and_law_subagent_prompt(*, combined_document_content):
    return (
"You are a focused asset and legal-framework extractor for legal "
            "documents. Your ONLY job is to extract every PROPERTY / ASSET, "
            "every MONETARY AMOUNT, every STATUTE referenced, and every "
            "ANNEXURE described in the documents below.\n\n"
            "Return ONE JSON object with this shape (no commentary, no markdown):\n\n"
            "{\n"
            '  "properties": [\n'
            '    { "identifier": "Arazi No. X / Khasra No. Y / Plot N / Survey No. N etc., exactly as written", '
            '"area": "area exactly as written (e.g. 0.263 Hectares, 2400 sq.ft.)", '
            '"location": "most specific location written in the docs (Mauza / Village -> Block / Tehsil -> District -> State)", '
            '"current_owner_per_docs": "name as written, if stated" }\n'
            "  ],\n"
            '  "amounts": [\n'
            '    { "amount": "in source notation (e.g. Rs. 11,64,000)", '
            '"context": "advance / total consideration / balance / payment / loan / fine / etc.", '
            '"from": "name as written", '
            '"to": "name as written", '
            '"date_iso": "YYYY-MM-DD if a date attaches to the amount, else null" }\n'
            "  ],\n"
            '  "statutes": [\n'
            '    { "name": "exact statute name as written (e.g. Specific Relief Act, 1963; Bharatiya Nyaya Sanhita, 2023; UP Zamindari Abolition & Land Reforms Act)", '
            '"sections": ["section numbers as written, e.g. 10, 12, 143"], '
            '"applies_to": "short reason why this statute is relevant per the documents" }\n'
            "  ],\n"
            '  "annexures": [\n'
            '    { "label": "Annexure 1", '
            '"type": "Sale Deed / Affidavit / Notice / Khatauni / Written Statement / etc.", '
            '"date_iso": "YYYY-MM-DD or null", '
            '"parties": ["names exactly as written"] }\n'
            "  ]\n"
            "}\n\n"
            "STRICT RULES:\n"
            "- Use values EXACTLY as written. Preserve Indian numerical "
            "notation for amounts (Rs. 11,64,000 stays that way; do NOT "
            "convert to '11.64 lakh' or '1,164,000').\n"
            "- Include every distinct property mention, even when the same "
            "plot is referenced by different identifiers in different docs.\n"
            "- For statutes: use names as written. Include both BNS / BNSS / "
            "BSA / IT Act / etc. and any state-level land-revenue / property "
            "statutes referenced.\n"
            "- Reference uploaded files only as Annexure N.\n"
            "- Return ONLY valid JSON.\n\n"
            "DOCUMENTS:\n"
            f"{combined_document_content}"
    )

