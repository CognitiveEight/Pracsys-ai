"""Contact and identity details on Relevant Parties (SIM-186).

Each party can carry the address, email, phone and identity numbers the
evidence gives for it. Three rules hold here, in code rather than in a prompt:

* a value is kept only if it appears in the party's own source document
  (``verify_party_details``), so nothing the model invents reaches the panel;
* an identity number never leaves unmasked (``mask_identifier``), whatever path
  produced it: the stored evidence already holds masked forms, but GSTIN/CIN
  are not masked at ingestion and a lawyer can type a full number into an edit;
* a value already on the party (typed by the lawyer, or verified earlier) wins
  over a new extraction (``merge_party_details``).

Pure functions, no I/O. Never log the values handled here.
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.rag.page_kinds import split_pages
from app.processing.pii_detector import (
    _mask_keep_last,
    mask_aadhaar,
    mask_bank_account,
    mask_dl,
    mask_epic,
    mask_pan,
    mask_passport,
    mask_udyam,
)

#: Wire ``type`` -> display ``label``.
IDENTIFIER_LABELS: Dict[str, str] = {
    "aadhaar": "Aadhaar",
    "pan": "PAN",
    "passport": "Passport",
    "voter_id": "Voter ID",
    "driving_licence": "Driving Licence",
    "bank_account": "Bank Account",
    "udyam": "Udyam",
    "gstin": "GSTIN",
    "cin": "CIN",
    "other": "ID",
}

_TYPE_ALIASES: Dict[str, str] = {
    "aadhar": "aadhaar",
    "aadhaar_number": "aadhaar",
    "uid": "aadhaar",
    "uidai": "aadhaar",
    "pan_number": "pan",
    "pan_card": "pan",
    "passport_number": "passport",
    "voter": "voter_id",
    "voterid": "voter_id",
    "voter_id_number": "voter_id",
    "epic": "voter_id",
    "dl": "driving_licence",
    "driving_license": "driving_licence",
    "driving_licence_number": "driving_licence",
    "licence": "driving_licence",
    "license": "driving_licence",
    "bank": "bank_account",
    "account": "bank_account",
    "account_number": "bank_account",
    "bank_account_number": "bank_account",
    "udyam_number": "udyam",
    "msme": "udyam",
    "gst": "gstin",
    "gst_number": "gstin",
    "cin_number": "cin",
}

_MASKERS = {
    "aadhaar": mask_aadhaar,
    "pan": mask_pan,
    "passport": mask_passport,
    "voter_id": mask_epic,
    "driving_licence": mask_dl,
    "bank_account": mask_bank_account,
    "udyam": mask_udyam,
}

#: Most digits a masked value may show (the "last 4").
_MAX_VISIBLE_DIGITS = 4

_PHONE_RUN_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_PIN_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_identifier_type(raw: Any) -> str:
    """Map a model- or user-supplied type to the wire vocabulary ("other" if unknown)."""
    key = re.sub(r"[\s.-]+", "_", str(raw or "").strip().lower()).strip("_")
    key = _TYPE_ALIASES.get(key, key)
    return key if key in IDENTIFIER_LABELS else "other"


def _compact(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _visible_digits(masked: str) -> int:
    return sum(ch.isdigit() for ch in masked)


def mask_identifier(id_type: str, value: Any) -> str:
    """Display form of an identity number: masked, at most the last 4 digits visible.

    Idempotent: an already-masked value comes back unchanged in shape.
    """
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    masker = _MASKERS.get(normalize_identifier_type(id_type))
    if masker is not None:
        masked = masker(raw)
    else:
        masked = _mask_keep_last(_compact(raw), _MAX_VISIBLE_DIGITS)
    if _visible_digits(masked) > _MAX_VISIBLE_DIGITS:
        masked = _mask_keep_last(_compact(raw), _MAX_VISIBLE_DIGITS)
    return masked


def _identifier_dedup_key(item: Dict[str, Any]) -> Tuple[str, str]:
    return (str(item.get("type") or ""), _compact(item.get("value") or "")[-4:])


def normalize_identifiers(raw: Any, *, source_doc_id: str = "") -> List[Dict[str, str]]:
    """Coerce a list of identifiers to ``{type, label, value, sourceDocId}``, masked, de-duplicated."""
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        id_type = normalize_identifier_type(item.get("type"))
        value = mask_identifier(id_type, item.get("value"))
        if not value:
            continue
        entry = {
            "type": id_type,
            "label": IDENTIFIER_LABELS[id_type],
            "value": value,
            "sourceDocId": str(item.get("sourceDocId") or source_doc_id or "").strip(),
        }
        key = _identifier_dedup_key(entry)
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        items = value
    elif value:
        items = re.split(r"[;,]", str(value))
    else:
        items = []
    return [str(v).strip() for v in items if str(v or "").strip()]


def _phone_digits(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _verify_address(address: str, text_lower: str, words: set) -> bool:
    tokens = [t for t in _WORD_RE.findall(address.lower()) if len(t) >= 2]
    if len(tokens) < 3:
        return False
    for pin in _PIN_RE.findall(address):
        if pin not in text_lower:
            return False
    present = sum(1 for t in tokens if t in words)
    return present / len(tokens) >= 0.7


def verify_party_details(
    raw: Dict[str, Any], source_text: str, *, source_doc_id: str = ""
) -> Tuple[Dict[str, Any], int]:
    """Keep only the details of one extracted party that appear in ``source_text``.

    Returns ``(details, dropped)`` where ``details`` has ``address``, ``email``,
    ``contactNumber`` and ``identifiers`` (masked), and ``dropped`` counts the
    values that were not found in the text.
    """
    text = source_text or ""
    text_lower = text.lower()
    words = set(_WORD_RE.findall(text_lower))
    compact_text = _compact(text)
    # Full digit strings of every phone-like run: two numbers separated only by
    # a space merge into one run, so match by containment, not equality.
    phone_runs = [re.sub(r"\D", "", m) for m in _PHONE_RUN_RE.findall(text)]
    dropped = 0

    address = str(raw.get("address") or "").strip()
    if address and not _verify_address(address, text_lower, words):
        address, dropped = "", dropped + 1

    emails: List[str] = []
    for email in _as_list(raw.get("emails") or raw.get("email")):
        if "@" in email and email.lower() in text_lower:
            emails.append(email)
        else:
            dropped += 1

    phones: List[str] = []
    for phone in _as_list(raw.get("phones") or raw.get("phone") or raw.get("contactNumber")):
        digits = _phone_digits(phone)
        if len(digits) >= 8 and any(digits in run for run in phone_runs):
            phones.append(phone)
        else:
            dropped += 1

    kept_ids: List[Dict[str, Any]] = []
    raw_ids = raw.get("identifiers")
    for item in raw_ids if isinstance(raw_ids, list) else []:
        if not isinstance(item, dict):
            continue
        compact_value = _compact(item.get("value") or "")
        if len(compact_value) >= 6 and compact_value in compact_text:
            kept_ids.append(item)
        else:
            dropped += 1

    details = {
        "address": address,
        "email": ", ".join(dict.fromkeys(emails)),
        "contactNumber": ", ".join(dict.fromkeys(phones)),
        "identifiers": normalize_identifiers(kept_ids, source_doc_id=source_doc_id),
    }
    return details, dropped


def empty_details() -> Dict[str, Any]:
    return {"address": "", "email": "", "contactNumber": "", "identifiers": []}


def merge_party_details(primary: Dict[str, Any], secondary: Dict[str, Any]) -> Dict[str, Any]:
    """``primary`` values win; ``secondary`` only fills blanks and adds new identifiers."""
    merged = empty_details()
    for field in ("address", "email", "contactNumber"):
        merged[field] = str(primary.get(field) or "").strip() or str(
            secondary.get(field) or ""
        ).strip()
    ids = normalize_identifiers(list(primary.get("identifiers") or []))
    seen = {_identifier_dedup_key(i) for i in ids}
    for item in normalize_identifiers(list(secondary.get("identifiers") or [])):
        key = _identifier_dedup_key(item)
        if key not in seen:
            seen.add(key)
            ids.append(item)
    merged["identifiers"] = ids
    return merged


def party_details_of(party: Dict[str, Any]) -> Dict[str, Any]:
    """The detail fields of a party dict, normalised (missing fields empty)."""
    return {
        "address": str(party.get("address") or "").strip(),
        "email": str(party.get("email") or "").strip(),
        "contactNumber": str(party.get("contactNumber") or "").strip(),
        "identifiers": normalize_identifiers(party.get("identifiers")),
    }


#: Fields the backend wants left out, not sent as "", when unknown (SIM-184).
_OMIT_WHEN_EMPTY = ("address", "email", "contactNumber", "sourceDocId", "sourceDocName")

#: Backend limit on ``sourceDocName``.
SOURCE_DOC_NAME_MAX = 500


def mask_party_payload(payload: Any) -> None:
    """Shape a relevant_parties payload for the wire, in place.

    Runs at the emit chokepoint, so every path (generation, cache replay,
    edits, legacy entries) leaves the same way:

    * every identifier is re-masked, so no unmasked number can be sent;
    * unknown contact and source fields are left out rather than sent as "",
      and ``pageNumber`` only goes out as a whole number of 1 or more, as the
      backend contract asks.
    """
    if not isinstance(payload, dict):
        return
    for party in payload.get("parties") or []:
        if not isinstance(party, dict):
            continue
        party["identifiers"] = normalize_identifiers(party.get("identifiers"))
        for field in _OMIT_WHEN_EMPTY:
            if field in party and not str(party.get(field) or "").strip():
                del party[field]
        page = party.get("pageNumber")
        if "pageNumber" in party and not (
            isinstance(page, int) and not isinstance(page, bool) and page >= 1
        ):
            del party["pageNumber"]


def first_mention_page(
    text: str, names: Iterable[str], *, skip: Iterable[int] = ()
) -> Optional[int]:
    """First page (``--- Page N ---``) naming any of ``names``, outside ``skip``.

    ``skip`` holds the document's index pages (SIM-192): a paper-book's index
    names every party in its cause title, which made "Page 1" the citation for
    everyone. ``None`` when the text has no page markers (DOCX, audio) or no
    other page names the party: the backend wants the field left out then.
    """
    wanted = [n.strip().lower() for n in names if n and len(n.strip()) >= 3]
    skipped = set(skip)
    for number, page_text in sorted(split_pages(text).items()):
        if number in skipped:
            continue
        lower = page_text.lower()
        if any(n in lower for n in wanted):
            return number
    return None


def details_page(
    text: str, party: Dict[str, Any], *, skip: Iterable[int] = ()
) -> Optional[int]:
    """First page, outside ``skip``, holding any of the party's verified details.

    The panel shows the address, phone, email and IDs, so the page they were
    read from is the citation a lawyer can check (SIM-192). ``None`` when the
    party has no details or the text has no page markers.
    """
    phones = [_phone_digits(p) for p in _as_list(party.get("contactNumber"))]
    phones = [p for p in phones if len(p) >= 8]
    emails = [e.lower() for e in _as_list(party.get("email")) if "@" in e]
    ids = [
        _compact(i.get("value") or "")
        for i in party.get("identifiers") or []
        if isinstance(i, dict) and len(_compact(i.get("value") or "")) >= 6
    ]
    address = str(party.get("address") or "").strip()
    if not (phones or emails or ids or address):
        return None
    skipped = set(skip)
    for number, page_text in sorted(split_pages(text).items()):
        if number in skipped:
            continue
        lower = page_text.lower()
        digits = re.sub(r"\D", "", page_text)
        compact = _compact(page_text)
        if (
            any(p in digits for p in phones)
            or any(e in lower for e in emails)
            or any(i in compact for i in ids)
            or (
                address
                and _verify_address(address, lower, set(_WORD_RE.findall(lower)))
            )
        ):
            return number
    return None

def details_counts(parties: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """How many parties carry each detail. For the log line; never log values."""
    counts = {"addresses": 0, "emails": 0, "phones": 0, "ids": 0}
    for p in parties:
        if not isinstance(p, dict):
            continue
        counts["addresses"] += bool(str(p.get("address") or "").strip())
        counts["emails"] += bool(str(p.get("email") or "").strip())
        counts["phones"] += bool(str(p.get("contactNumber") or "").strip())
        counts["ids"] += len(p.get("identifiers") or [])
    return counts

