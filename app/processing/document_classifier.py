"""
Document classification + structured field extraction.

A single Gemini call replaces the previous summary/strength prompt and
additionally returns a coarse ``documentType`` and a ``extractedFields``
dict tailored to that type. The classifier runs on text that has already
been PII-redacted by ``pii_detector.scan_text``, so identifier numbers
do not leave this service to the LLM.

Recognised document types (kept stable for the backend contract):

ID documents
    aadhaar_card, pan_card, passport, voter_id, driving_licence,
    msme_certificate, gst_certificate, ration_card, birth_certificate,
    marriage_certificate, death_certificate

Financial / non-ID
    bank_statement, itr_return, salary_slip, form_16, electricity_bill,
    water_bill, gas_bill, telecom_bill, property_tax_receipt,
    sale_deed, rental_agreement, loan_agreement

Legal / case
    fir, complaint, court_order, judgment, notice, affidavit,
    contract, agreement, power_of_attorney, will, summons

Communication / other
    email, letter, photograph, screenshot, other
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

from loguru import logger


SUPPORTED_DOCUMENT_TYPES = (
    # ID
    "aadhaar_card", "pan_card", "passport", "voter_id", "driving_licence",
    "msme_certificate", "gst_certificate", "ration_card", "birth_certificate",
    "marriage_certificate", "death_certificate",
    # Financial / non-ID
    "bank_statement", "itr_return", "salary_slip", "form_16",
    "electricity_bill", "water_bill", "gas_bill", "telecom_bill",
    "property_tax_receipt", "sale_deed", "rental_agreement", "loan_agreement",
    # Legal / case
    "fir", "complaint", "court_order", "judgment", "notice", "affidavit",
    "contract", "agreement", "power_of_attorney", "will", "summons",
    # Communication / other
    "email", "letter", "photograph", "screenshot",
    "other",
)


_CLASSIFIER_PROMPT_HEADER = """You are an Indian-law document analyst. Read the redacted document excerpt and return a single JSON object describing it.

Identifier numbers (Aadhaar, PAN, GSTIN, passport, EPIC, driving licence, bank account) have been masked with X. Do not try to recover them — work from the surrounding context (headings, layout cues, names, dates, amounts).

Output JSON with EXACTLY these keys:
- "summary": 2-3 short sentences in English, under 250 characters, describing what the document is and why it might matter as evidence.
- "evidentiaryStrength": one of "Low", "Medium", "High".
- "documentType": one of: {types}.
- "extractedFields": object of useful structured fields. Keys must use snake_case. Choose keys that fit the documentType. Examples (NOT exhaustive):
    aadhaar_card  -> name, dob, gender, address, issued_date
    pan_card      -> name, fathers_name, dob
    passport      -> name, dob, place_of_birth, place_of_issue, date_of_issue, date_of_expiry
    voter_id      -> name, fathers_or_husbands_name, dob_or_age, address
    driving_licence -> name, dob, address, valid_from, valid_until, vehicle_classes
    msme_certificate -> enterprise_name, owner_name, date_of_incorporation, enterprise_type, address, activities
    gst_certificate -> legal_name, trade_name, address, date_of_registration, constitution
    bank_statement -> account_holder, bank_name, branch, period_from, period_to, opening_balance, closing_balance, total_credits, total_debits
    itr_return -> assessee_name, assessment_year, gross_total_income, total_tax_paid, refund_or_demand
    salary_slip -> employee_name, employer_name, period, gross_pay, net_pay, deductions
    form_16 -> employee_name, employer_name, assessment_year, gross_salary, tax_deducted
    electricity_bill / water_bill / gas_bill / telecom_bill -> consumer_name, service_address, billing_period, amount_due, due_date, provider
    sale_deed / rental_agreement / loan_agreement -> parties, property_or_subject, consideration_or_amount, period_or_tenure, date_of_agreement
    fir / complaint -> fir_number, police_station, date, complainant, accused, sections_invoked, brief_facts
    court_order / judgment -> court, case_number, date, parties, key_directions
    notice / affidavit / will / power_of_attorney -> issuing_party_or_deponent, date, addressee, subject, key_points
    email / letter -> from, to, date, subject, key_points
    other / photograph / screenshot -> any-fields-you-can-infer

  Use plain strings or arrays of strings. Dates as ISO (YYYY-MM-DD) when possible. Skip a field if the answer is not present — do NOT guess. Keep each value under 200 characters.

If you cannot tell what the document is, set documentType to "other" and put what you observed in extractedFields.

Output ONLY the JSON object — no markdown, no code fences, no commentary.
"""


def _build_prompt(redacted_text: str) -> str:
    snippet = (
        (redacted_text.strip()[:6000] + "...")
        if len(redacted_text) > 6000
        else redacted_text.strip()
    )
    types = ", ".join(SUPPORTED_DOCUMENT_TYPES)
    header = _CLASSIFIER_PROMPT_HEADER.format(types=types)
    return (
        f"{header}\n\nRedacted document excerpt:\n{snippet}\n\nJSON:"
    )


def _strip_code_fence(raw: str) -> str:
    raw = raw.strip()
    if "```" in raw:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if m:
            return (m.group(1) or "").strip()
    return raw


def _slice_first_json_object(raw: str) -> str:
    start = raw.find("{")
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    return ""


def _coerce_extracted_fields(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for k, v in value.items():
        if not isinstance(k, str):
            continue
        key = re.sub(r"[^a-z0-9_]", "_", k.strip().lower()).strip("_")
        if not key:
            continue
        if isinstance(v, str):
            cleaned[key] = v.strip()[:200]
        elif isinstance(v, (int, float, bool)):
            cleaned[key] = v
        elif isinstance(v, list):
            cleaned[key] = [
                (str(item).strip()[:200] if not isinstance(item, (int, float, bool)) else item)
                for item in v
                if item is not None
            ][:20]
        elif isinstance(v, dict):
            # flatten one level — backend prefers flat fields
            for sub_k, sub_v in v.items():
                if not isinstance(sub_k, str):
                    continue
                sub_key = f"{key}_{re.sub(r'[^a-z0-9_]', '_', sub_k.strip().lower()).strip('_')}"
                if isinstance(sub_v, str):
                    cleaned[sub_key] = sub_v.strip()[:200]
    return cleaned


async def classify_and_extract(
    redacted_text: str, filename: str
) -> Dict[str, Any]:
    """Run Gemini once to produce summary + evidentiary strength +
    document type + structured fields. Always returns a dict shaped like:

        {
          "summary": str,
          "evidentiary_strength": "Low" | "Medium" | "High",
          "document_type": str,
          "extracted_fields": dict,
        }

    Falls back to safe defaults on any error. Never raises.
    """
    safe_default = {
        "summary": "",
        "evidentiary_strength": "Medium",
        "document_type": "other",
        "extracted_fields": {},
    }

    if not redacted_text or len(redacted_text.strip()) < 30:
        safe_default["evidentiary_strength"] = "Low"
        safe_default["summary"] = "Insufficient extractable content."
        return safe_default

    try:
        from app.ai.gemini_client import gemini_client

        gemini_client.initialize()
        prompt = _build_prompt(redacted_text)
        raw = await gemini_client.generate(
            prompt, temperature=0.2, max_tokens=12000
        )
        if not raw or "Error" in (raw[:80] or ""):
            logger.debug(
                "Classifier got empty/error response for {}", filename
            )
            return safe_default

        cleaned = _slice_first_json_object(_strip_code_fence(raw))
        if not cleaned:
            logger.debug(
                "Classifier could not find JSON in response for {} (first 300 chars: {})",
                filename,
                (raw[:300] if raw else ""),
            )
            return safe_default

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning(
                "Classifier JSON parse failed ({}); first 300 chars: {}",
                str(e),
                (cleaned[:300] if cleaned else ""),
            )
            return safe_default

        summary = (data.get("summary") or "").strip()
        strength = (
            data.get("evidentiaryStrength")
            or data.get("evidentiary_strength")
            or "Medium"
        ).strip()
        if strength not in ("Low", "Medium", "High"):
            strength = "Medium"

        doc_type = (
            data.get("documentType") or data.get("document_type") or "other"
        )
        if isinstance(doc_type, str):
            doc_type = doc_type.strip().lower()
        if doc_type not in SUPPORTED_DOCUMENT_TYPES:
            doc_type = "other"

        fields = _coerce_extracted_fields(
            data.get("extractedFields") or data.get("extracted_fields")
        )

        return {
            "summary": summary,
            "evidentiary_strength": strength,
            "document_type": doc_type,
            "extracted_fields": fields,
        }
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "Document classification failed for {}: {}", filename, str(e)
        )
        return safe_default
