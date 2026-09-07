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
            "purchasers, advocates, court officials, witnesses, and the "
            "signatories OF THE INSTRUMENTS IN DISPUTE — if they are named in "
            "the documents.\n"
            "- Never write 'Party No. 1' / 'Party No. 2' / 'the plaintiff' / "
            "'the petitioner' / 'the respondent' placeholders. If you cannot "
            "find a real name for an entry, DROP the entry.\n"
            "\n"
            "EXCLUSIONS — these are NOT parties to the matter. DROP them:\n"
            "- DOCUMENT-CONTROL METADATA. Uploaded policies, terms, agreements "
            "and manuals carry version-control tables — 'Document Control', "
            "'Summary of Changes', 'Approvals', 'Roles and Responsibilities' — "
            "listing an Author, Owner, Reviewer or 'Approved By' with a job "
            "title (e.g. 'Author: Jane Roe', 'Approved By: John Doe, Chief "
            "Operating Officer'). Those people wrote or signed off the "
            "DOCUMENT; they are not participants in the dispute. Never list "
            "them as parties.\n"
            "- ANNEXURES MARKED [CITED INSTRUMENT]. From those blocks extract "
            "ONLY the contracting entity the instrument is made by or for (a "
            "party often uploads its own standard agreement). Never extract the "
            "individuals they name in document-control, approval or revision "
            "tables, and never extract the third-party vendors, analytics "
            "providers or regulators they mention in passing.\n"
            "- ADVISERS. In a consultation transcript, the lawyer or consultant "
            "assessing the matter — whoever offers a team member, a consultation "
            "report, an opinion on the merits, or a fee — is not a party to the "
            "dispute. Record them with designation \"adviser/counsel\" so they "
            "are never mistaken for the client.\n"
            "- BOILERPLATE / POLICY ENTITIES. A privacy statement, acceptable-"
            "use policy, website terms, or standard-form service agreement "
            "names its own publisher, group companies, regulators, and third-"
            "party vendors or domains (analytics providers, brand-protection "
            "services, payment processors). Include such an organisation ONLY "
            "when the case facts actually involve it — never merely because it "
            "is mentioned in a policy's own text.\n"
            "- SPEAKER / DIARIZATION PLACEHOLDERS. Audio annexures are "
            "transcribed with labels such as 'Caller', 'Receiver', 'Speaker A', "
            "'Speaker B' plus [MM:SS] timestamps. These are placeholders, NOT "
            "names. NEVER return them as a party. If the transcript reveals a "
            "speaker's real name, use that name; otherwise DROP the entry.\n"
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
            '"date_iso": "YYYY-MM-DD if a date attaches to the amount, else null", '
            '"ambiguous": true/false — true when the SOURCE states this figure unclearly, '
            'e.g. a speech-to-text disfluency like "6 or 6 16 lakh", a self-correction, '
            'or two different figures given for the same item, '
            '"ambiguity_note": "what the source literally says and which readings are possible; empty when ambiguous=false" }\n'
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
            "- AMBIGUOUS FIGURES (CRITICAL). Audio annexures are machine-"
            "transcribed and speakers self-correct, so a key amount can arrive "
            "garbled — e.g. \"approximately 6 or 6 16 lakh USD\", which could "
            "be 6 lakh or 16 lakh. NEVER silently pick one reading, and never "
            "smooth it into a range you invented. Record the figure as the "
            "source states it, set \"ambiguous\": true, and put both candidate "
            "readings in \"ambiguity_note\". Choosing arbitrarily makes the "
            "central figure of a case change between regenerations.\n"
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

