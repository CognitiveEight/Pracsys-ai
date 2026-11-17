#!/usr/bin/env python3
"""
verify_relevant_party_details.py — Offline checks for SIM-186.

The Relevant Parties panel carries each party's address, email, phone and
masked identity numbers, read from the party's own source document. These
checks run the real ``_generate_and_emit_relevant_parties`` (and the add/edit
handlers) with the model, the vector store and the socket faked, and pin:

  * details found in the document reach the emitted party, IDs masked;
  * a value the model invents (not in the document) is dropped;
  * a lawyer's value survives a regeneration, incremental or full refresh;
  * add/edit mask a typed full number, and every emit path masks;
  * the chat context shows the details;
  * no address, email, phone or ID value reaches a log line.

Group names match specs/acceptance-criteria/SIM-186-relevant-party-details.md.
No API key, no backend, no Qdrant. Fictional data only.

USAGE
    python tests/offline/verify_relevant_party_details.py
    python tests/offline/verify_relevant_party_details.py --list
    python tests/offline/verify_relevant_party_details.py --only details_extracted
"""

import argparse
import asyncio
import copy
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root

# Record (rather than perform) the write-through persistence.
from app.workspace import persistence as _persistence  # noqa: E402

STORE = {}


def _record_persist(workspace_id, kind, data, **_kw):
    STORE[(workspace_id, kind)] = copy.deepcopy(data)


async def _restore(workspace_id, kind, **_kw):
    return copy.deepcopy(STORE.get((workspace_id, kind)))


_persistence.persist_state = _record_persist
_persistence.restore_state = _restore

from loguru import logger  # noqa: E402

from app.ai.gemini_client import gemini_client  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.langgraph.chat_nodes import chat_context_builder_node  # noqa: E402
from app.socket.events import event_handler as H  # noqa: E402
from app.vectorstore.qdrant import vector_store  # noqa: E402

RESULTS = []
EMITS = []
LOGS = []
RUN_ID = uuid.uuid4().hex[:8]
OWNER = "68b0e1f8d3a9c4b2e1f03456"
DOC_A = "68c1f0a9e4b0d2a1c3f4aa01"
DOC_B = "68c1f0a9e4b0d2a1c3f4aa02"

# Fictional party and details. None of these values may ever be logged.
NAME = "Shri Arun Mehta"
ADDRESS = "12 Lake View Road, Kothrud, Pune 411001"
EMAIL = "arun.mehta@example.com"
PHONE = "+91 98765 43210"
AADHAAR_MASKED = "XXXX-XXXX-9012"
PAN_MASKED = "ABCXX1234X"
GSTIN = "27ABCDE1234F1Z5"
INVENTED_EMAIL = "made.up@nowhere.in"
LAWYER_EMAIL = "typed.by.lawyer@example.com"
OTHER_EMAIL = "second.office@example.com"
SECRETS = [ADDRESS, EMAIL, "98765", AADHAAR_MASKED, PAN_MASKED, GSTIN, "27ABCDE",
           INVENTED_EMAIL, LAWYER_EMAIL, OTHER_EMAIL, "1234 5678 9012"]

DOC_A_NAME = "affidavit.pdf"
DOC_A_TEXT = (
    "--- Page 1 ---\nIN THE COURT OF THE CIVIL JUDGE, PUNE\n--- Page 2 ---\n"
    f"AFFIDAVIT of {NAME}, residing at {ADDRESS}, email {EMAIL}, "
    f"mobile {PHONE}. Aadhaar {AADHAAR_MASKED}, PAN {PAN_MASKED}, GSTIN {GSTIN}. "
    "The deponent states that M/s Fictional Traders supplied the goods."
)
DOC_B_TEXT = (
    f"LETTER from {NAME}, second office email {OTHER_EMAIL}, "
    "regarding the supply by M/s Fictional Traders."
)
DOCS = {}


def _ws(name):
    return f"{name}_{RUN_ID}"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (f"\n        {detail}" if detail and not ok else ""))


class _FakeSocket:
    async def emit(self, event, data):
        EMITS.append((event, copy.deepcopy(data)))
        return True

    async def emit_ai_response(self, data):
        return True


def _sink(message):
    LOGS.append(str(message))


def _extraction_for(prompt):
    """What the fake extractor 'reads' from a document."""
    if "AFFIDAVIT" in prompt:
        return {"parties": [
            {"name": NAME, "designation": "deponent", "role_in_matter": "supplier's buyer",
             "first_mention_doc": "Annexure 1", "address": ADDRESS,
             "emails": [EMAIL, INVENTED_EMAIL], "phones": [PHONE],
             "identifiers": [{"type": "aadhaar", "value": AADHAAR_MASKED},
                             {"type": "pan", "value": PAN_MASKED},
                             {"type": "gstin", "value": GSTIN},
                             {"type": "passport", "value": "KXXX7777"}]},
            {"name": "M/s Fictional Traders", "designation": "supplier",
             "role_in_matter": "supplied the goods", "first_mention_doc": "Annexure 1",
             "address": "", "emails": [], "phones": [], "identifiers": []},
        ]}
    if "LETTER" in prompt:
        return {"parties": [
            {"name": NAME, "designation": "sender", "role_in_matter": "wrote the letter",
             "first_mention_doc": "Annexure 2", "address": "",
             "emails": [OTHER_EMAIL], "phones": [], "identifiers": []},
        ]}
    return {"parties": []}


async def _fake_generate(prompt="", **_kw):
    if "CONTACT AND IDENTITY DETAILS" in prompt:
        return json.dumps(_extraction_for(prompt))
    if "classifying the PARTIES" in prompt:
        names = [n for n in (NAME, "M/s Fictional Traders") if n in prompt]
        return json.dumps({"parties": [
            {"name": n, "role": "Petitioner" if n == NAME else "Respondent",
             "details": "A fictional party to the supply dispute."} for n in names
        ]})
    return "{}"


async def _get_all_documents(workspace_id=None, **_kw):
    return [{"document_id": d, "content": t, "filename": DOC_A_NAME if d == DOC_A else ""}
            for d, t in DOCS.get(workspace_id, {}).items()]


async def _signals(workspace_id, user_id, **_kw):
    return {"evidenceSnippet": ""}


async def _hydrate(workspace_id, *, user_id=None, recover=True):
    return None


def _install():
    gemini_client.generate = _fake_generate
    vector_store.get_all_documents = _get_all_documents
    H._extract_case_signals_from_documents = _signals
    H._hydrate_workspace_caches = _hydrate
    H._socket_client = _FakeSocket()


def _last_parties(ws):
    for ev, env in reversed(EMITS):
        if ev == "document_feature_response" and env.get("workspaceId") == ws \
                and env.get("featureType") == "relevant_parties":
            return env["payload"]["parties"]
    return None


def _party(parties, name):
    return next((p for p in parties or [] if p.get("name") == name), None)


async def _generate(ws):
    await H._generate_and_emit_relevant_parties(ws, OWNER, "PETITIONER")
    return _last_parties(ws)


# ------------------------------------------------------------------ groups

async def details_extracted():
    ws = _ws("extract")
    DOCS[ws] = {DOC_A: DOC_A_TEXT}
    parties = await _generate(ws)
    p = _party(parties, NAME)
    check("the party is emitted", p is not None, f"parties={parties}")
    if not p:
        return
    check("address from the document", p.get("address") == ADDRESS)
    check("email from the document", p.get("email") == EMAIL, p.get("email"))
    check("phone from the document", p.get("contactNumber") == PHONE)
    values = {i["type"]: i["value"] for i in p.get("identifiers") or []}
    check("Aadhaar and PAN carried masked",
          values.get("aadhaar") == AADHAAR_MASKED and values.get("pan") == PAN_MASKED, values)
    check("GSTIN (clear at ingestion) is masked on emit",
          values.get("gstin") and values["gstin"] != GSTIN and values["gstin"].endswith("F1Z5"),
          values)
    check("identifiers carry their source document",
          all(i.get("sourceDocId") == DOC_A for i in p.get("identifiers") or []))
    other = _party(parties, "M/s Fictional Traders")
    check("a party without details leaves the contact keys out (never \"\")",
          other is not None
          and not {"address", "email", "contactNumber"} & set(other)
          and other.get("identifiers") == [], other)


async def source_fields():
    ws = _ws("source")
    DOCS[ws] = {DOC_A: DOC_A_TEXT}
    p = _party(await _generate(ws), NAME) or {}
    check("sourceDocId is the evidence file id", p.get("sourceDocId") == DOC_A, p.get("sourceDocId"))
    check("sourceDocName is the file name", p.get("sourceDocName") == DOC_A_NAME)
    check("pageNumber is the page where the party is first named", p.get("pageNumber") == 2,
          p.get("pageNumber"))

    ws = _ws("source_nopage")
    DOCS[ws] = {DOC_B: DOC_B_TEXT}  # no page markers, no stored file name
    p = _party(await _generate(ws), NAME) or {}
    check("no page markers: pageNumber is left out", "pageNumber" not in p, p)
    check("no file name known: sourceDocName is left out", "sourceDocName" not in p, p)
    check("the source id is still sent", p.get("sourceDocId") == DOC_B)

    await H.on_add_relevant_party({"workspaceId": ws, "userId": OWNER,
                                   "name": "Smt. Typed In"})
    added = _party(_last_parties(ws), "Smt. Typed In") or {}
    check("a user-added party has no source fields",
          not {"sourceDocId", "sourceDocName", "pageNumber"} & set(added), added)


async def invented_values_dropped():
    ws = _ws("invented")
    DOCS[ws] = {DOC_A: DOC_A_TEXT}
    p = _party(await _generate(ws), NAME) or {}
    check("an email not in the document is dropped", INVENTED_EMAIL not in (p.get("email") or ""))
    check("an identifier not in the document is dropped",
          all(i["type"] != "passport" for i in p.get("identifiers") or []))
    check("the drop is counted in the log",
          any("dropped_unverified=2" in m for m in LOGS), "no dropped_unverified=2 line")


async def user_value_survives_regeneration():
    ws = _ws("incremental")
    DOCS[ws] = {DOC_A: DOC_A_TEXT}
    await _generate(ws)
    await H.on_edit_relevant_party({"workspaceId": ws, "userId": OWNER,
                                    "partyId": "party_1", "email": LAWYER_EMAIL})
    DOCS[ws][DOC_B] = DOC_B_TEXT  # a new upload naming the same party
    p = _party(await _generate(ws), NAME) or {}
    check("incremental run: the lawyer's email is kept", p.get("email") == LAWYER_EMAIL,
          p.get("email"))
    check("incremental run: the new document is added to the party's sources",
          set(p.get("sourceDocIds") or []) == {DOC_A, DOC_B})

    ws = _ws("full")
    DOCS[ws] = {DOC_A: DOC_A_TEXT}
    await _generate(ws)
    await H.on_edit_relevant_party({"workspaceId": ws, "userId": OWNER,
                                    "partyId": "party_1", "email": LAWYER_EMAIL})
    DOCS[ws] = {DOC_B: DOC_B_TEXT}  # every live document is new: a full refresh
    p = _party(await _generate(ws), NAME) or {}
    check("full refresh: the lawyer's email is kept", p.get("email") == LAWYER_EMAIL,
          p.get("email"))


async def edit_masks_identifier():
    ws = _ws("edit")
    DOCS[ws] = {DOC_A: DOC_A_TEXT}
    await _generate(ws)
    await H.on_edit_relevant_party({
        "workspaceId": ws, "userId": OWNER, "partyId": "party_1",
        "address": "Typed address, Pune 411002",
        "identifiers": [{"type": "aadhaar", "value": "1234 5678 9012"}],
    })
    p = _party(_last_parties(ws), NAME) or {}
    check("edit: a typed Aadhaar is emitted masked",
          [i["value"] for i in p.get("identifiers") or []] == ["XXXX-XXXX-9012"],
          p.get("identifiers"))
    cached = _party(H._workspace_relevant_parties_payload[ws]["parties"], NAME) or {}
    check("edit: the cache never holds the full number",
          "123456789012" not in json.dumps(cached).replace(" ", ""))
    check("edit: the address is stored", p.get("address") == "Typed address, Pune 411002")

    await H.on_add_relevant_party({
        "workspaceId": ws, "userId": OWNER, "name": "Smt. Fictional Witness",
        "identifiers": [{"type": "gstin", "value": GSTIN}], "email": OTHER_EMAIL,
    })
    added = _party(_last_parties(ws), "Smt. Fictional Witness") or {}
    check("add: a typed GSTIN is emitted masked",
          [i["value"] for i in added.get("identifiers") or []] == ["XXXXXXXXXXXF1Z5"],
          added.get("identifiers"))


async def emit_masks_every_path():
    ws = _ws("replay")
    H._workspace_relevant_parties_payload[ws] = {"parties": [
        {"id": "party_1", "name": NAME, "role": "Petitioner", "details": "x",
         "identifiers": [{"type": "pan", "value": "ABCDE1234F"}]},
    ]}
    await H._emit_relevant_parties(ws, OWNER)
    p = _party(_last_parties(ws), NAME) or {}
    check("a cached unmasked PAN is masked on re-emit",
          [i["value"] for i in p.get("identifiers") or []] == ["ABCXX1234X"],
          p.get("identifiers"))


async def chat_sees_details():
    ws = _ws("chat")
    DOCS[ws] = {DOC_A: DOC_A_TEXT}
    parties = await _generate(ws)
    state = chat_context_builder_node({"chat_context": {
        "role_context": {"role": "PETITIONER"},
        "workspace_state": {"workspace_id": ws},
        "relevant_parties": parties,
    }})
    # Only the built prompt counts: the returned state also echoes the input.
    blob = str(state.get("prompt_context") or "")
    check("the chat prompt was built", "RELEVANT PARTIES" in blob, state.get("error", ""))
    check("chat context carries the address", ADDRESS in blob)
    check("chat context carries the masked Aadhaar", AADHAAR_MASKED in blob)
    check("chat context never carries the clear GSTIN", GSTIN not in blob)


async def no_values_logged():
    leaked = sorted({s for s in SECRETS for m in LOGS if s in m})
    check("no address, email, phone or ID value in any log line", not leaked, f"leaked={leaked}")


CHECKS = [
    ("details_extracted", details_extracted),
    ("source_fields", source_fields),
    ("invented_values_dropped", invented_values_dropped),
    ("user_value_survives_regeneration", user_value_survives_regeneration),
    ("edit_masks_identifier", edit_masks_identifier),
    ("emit_masks_every_path", emit_masks_every_path),
    ("chat_sees_details", chat_sees_details),
    ("no_values_logged", no_values_logged),  # last: scans every group's logs
]


async def _run(only):
    saved = settings.citations_enabled
    settings.citations_enabled = False
    _install()
    try:
        for name, fn in CHECKS:
            if only and name not in only:
                continue
            print(f"\n[{name}]")
            try:
                await fn()
            except Exception as e:  # a crash is a failed group, not a dead suite
                check(f"{name} ran without raising", False, repr(e))
    finally:
        settings.citations_enabled = saved


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show the group names")
    ap.add_argument("--only", nargs="*", default=None, help="run only these groups")
    ap.add_argument("--verbose", action="store_true", help="also print service logs")
    args = ap.parse_args()
    if args.list:
        for name, _ in CHECKS:
            print(name)
        return 0
    if not args.verbose:
        logger.remove()
    logger.add(_sink, level="DEBUG", format="{message}")
    asyncio.run(_run(set(args.only) if args.only else None))
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
