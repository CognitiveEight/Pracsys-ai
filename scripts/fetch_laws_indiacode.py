#!/usr/bin/env python3
"""
Rebuild the master laws file from India Code, the Government of India's
official statute repository (https://indiacode.gov.in).

Why: the master laws export had acts filed under the wrong name, BNS mixed
with IPC, and templated descriptions instead of statutory text (see
``scripts/clean_master_laws.py``). Every act that can be matched on India
Code is rebuilt from it here: official section numbers, official headings and
the full section text (including amendment notes).

How:
1. **Resolve.** Each act in the input is looked up on India Code's search
   API (``/server/api/discover/search/objects``, a DSpace backend) and matched
   to an India Code ``act_id`` by name similarity plus the enactment year.
   Matches below ``--min-score`` are not trusted and are reported instead.
   ``OVERRIDES`` pins acts whose names differ too much to match automatically.
2. **Fetch.** All SECTION items of the matched act, in official order
   (``order_number``), 100 per page. Responses are cached under ``--cache``,
   so a re-run is offline and a partial run resumes.
3. **Write.** One row per section: ``id``, ``actCode``, ``act``, ``section``,
   ``title``, ``description`` (the statutory text), plus ``omitted`` for
   omitted/repealed sections and ``source``/``sourceActId`` for provenance.
   Acts India Code does not have (state-law groups, rules, orders) are carried
   over from the input marked ``"source": "export-unverified"``, except the
   ones the cleaning script identified as mislabelled, which are dropped.

The input file is never modified.

USAGE
    python scripts/fetch_laws_indiacode.py "masterLaws 1.json"
    python scripts/fetch_laws_indiacode.py "masterLaws 1.json" --resolve-only   # review the act matches first
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import OrderedDict, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from rapidfuzz import fuzz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clean_master_laws import DROP_ACTS, REPLACE_WITH  # noqa: E402

API = "https://indiacode.gov.in/server/api/discover/search/objects"
PAGE_SIZE = 100
REQUEST_GAP_SECONDS = 0.4  # be polite to a government server

# actCode -> India Code act_id, for acts the name search does not rank first
# (looked up on India Code; FCA is the same Act, renamed in 2023).
PINNED: Dict[str, str] = {
    "FCA": "AC_CEN_16_18_00008_198069_1517807325332",    # Van (Sanrakshan Evam Samvardhan) Adhiniyam, 1980
    "JJ": "AC_CEN_13_14_000010_201602_1517807328168",
    "LOK": "AC_CEN_26_36_00014_201401_1517807327889",
    "COMP": "AC_CEN_22_29_00005_200312_1517807324781",
    "PMLA": "AC_CEN_2_2_00035_200315_1517807326550",
    "PCMA": "AC_CEN_13_14_00001_200706_1517807317592",
    "PHRA": "AC_CEN_5_24_000010_199410_1517807322704",
    "COSA": "AC_CEN_40_66_00002_200907_1517807325488",
    "LLP": "AC_CEN_22_29_00007_200906_1517807325904",
    "BDA": "AC_CEN_16_18_000010_200318_1517807327125",
    "SEBI": "AC_CEN_2_11_00014_199215_1517807319932",
    "PASS": "AC_CEN_10_10_00008_196715_1517807321481",
}

# Acts repealed per India Code's own repeal records ("<Act> (Rep., Act N of YYYY)").
# Their sections are no longer published, so exported rows are kept, flagged.
REPEALED: Dict[str, str] = {
    "ITA": "Act 30 of 2025 (Income-tax Act, 2025; not yet on India Code)",
    "FACT": "Act 37 of 2020 (Occupational Safety, Health and Working Conditions Code, 2020)",
    "WJ": "Act 37 of 2020 (Occupational Safety, Health and Working Conditions Code, 2020)",
    "ESI": "Act 36 of 2020 (Code on Social Security, 2020)",
    "EPF": "Act 36 of 2020 (Code on Social Security, 2020)",
    "PGA": "Act 36 of 2020 (Code on Social Security, 2020)",
    "AIRCR": "Act 16 of 2024 (Bharatiya Vayuyan Adhiniyam, 2024)",
    "FOR": "Act 13 of 2025 (Immigration and Foreigners Act, 2025)",
    "COGSA": "Act 19 of 2025 (Carriage of Goods by Sea Act, 2025)",
    "PRB": "Act 51 of 2023 (Press and Registration of Periodicals Act, 2023)",
    "PPF": "Act 13 of 2018 (merged into the Government Savings Promotion Act, 1873)",
    "GSCA": "Act 13 of 2018 (merged into the Government Savings Promotion Act, 1873)",
    "CMN": "Act 4 of 2018",
    "HPA": "Act 31 of 2005 (never brought into force)",
    "MGNREGA": "Act 36 of 2025 (Viksit Bharat-Guarantee for Rozgar and Ajeevika Mission (Gramin) Act, 2025)",
    # No repeal record found; the 1958 Act is absent from India Code's central
    # list and the Merchant Shipping Act, 2025 (Act 24 of 2025) is present.
    "MSA": "superseded by the Merchant Shipping Act, 2025 (Act 24 of 2025) - repeal record not found, confirm",
}

# Current acts that replace a repealed one, added under a new code.
SUCCESSORS: Dict[str, Tuple[str, str, str]] = {  # code -> (act name, India Code act_id, replaces)
    "BVA": ("Bharatiya Vayuyan Adhiniyam, 2024", "AC_CEN_36_0_000010_202416_1738131525716", "AIRCR"),
    "IFA": ("Immigration and Foreigners Act, 2025", "AC_CEN_5_40_00022_2025-13_1757572252406", "FOR"),
    "COGSA2025": ("Carriage of Goods by Sea Act, 2025", "AC_CEN_198_0_00005_2025-19_1757570478140", "COGSA"),
    "PRPA": ("Press and Registration of Periodicals Act, 2023", "AC_CEN_29_0_00007_202351_1724412396961", "PRB"),
    "MSA2025": ("Merchant Shipping Act, 2025", "AC_CEN_198_0_00006_2025-24_1777264737276", "MSA"),
    "GSPA": ("Government Savings Promotion Act, 1873", "AC_CEN_2_11_00026_187305_1523269408953", "PPF"),
    "VBGRAMG": ("Viksit Bharat-Guarantee for Rozgar and Ajeevika Mission (Gramin) Act, 2025",
                "AC_CEN_18_21_00003_2025-36_1782898495903", "MGNREGA"),
}

# Exported acts with no sections on India Code as one central act.
NOT_ON_INDIA_CODE = {
    "PRA": "group of state Panchayati Raj Acts",
    "APMC": "group of state APMC Acts",
    "BCIR": "Bar Council of India Rules (not an Act)",
    "ODA": "no central 'Official Documents Act'",
    "IT2025": "amendment rules, not an Act",
    "ITR": "rules, not an Act",
    "FCO": "control order, not an Act",
    "WBP": "Whistle Blowers Protection Act, 2011 (No. 17 of 2014) is listed as not enforced; no sections",
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _get(params: List[Tuple[str, str]], cache_path: Optional[str] = None) -> Dict[str, Any]:
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as fh:
            return json.load(fh)
    url = API + "?" + urllib.parse.urlencode(params)
    last: Optional[Exception] = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
            time.sleep(REQUEST_GAP_SECONDS)
            if cache_path:
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
            return data
        except Exception as exc:  # noqa: BLE001 - retried, then raised
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"India Code request failed after retries: {last}")


def _objects(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = data.get("_embedded", {}).get("searchResult", {})
    return [o["_embedded"]["indexableObject"] for o in result.get("_embedded", {}).get("objects", [])]


def _total(data: Dict[str, Any]) -> int:
    return data.get("_embedded", {}).get("searchResult", {}).get("page", {}).get("totalElements", 0)


def _md(item: Dict[str, Any], key: str) -> Optional[str]:
    values = item.get("metadata", {}).get(key) or []
    return values[0].get("value") if values else None


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


def _year(name: str) -> Optional[str]:
    m = re.search(r"\b(1[89]\d\d|20\d\d)\b", name or "")
    return m.group(1) if m else None


def _bare(name: str) -> str:
    name = re.sub(r"\b(1[89]\d\d|20\d\d)\b", " ", (name or "").lower())
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    return " ".join(w for w in name.split() if w not in {"the", "of", "and", "act"})


def _act_name(act_id: str, cache: str) -> str:
    data = _get([("f.act_id", f"{act_id},equals"), ("f.identifier_collection", "SECTION,equals"), ("size", "1")],
                os.path.join(cache, "names", f"{act_id}.json"))
    items = _objects(data)
    return (_md(items[0], "dc.identifier.act_name") or "") if items else ""


def resolve(code: str, act: str, cache: str) -> Dict[str, Any]:
    """Best central India Code act for an exported act name, with its score.

    Searches India Code's ACT records, then SECTION records, restricted to
    central acts. The year compared is the one in the act's NAME: India
    Code's ``act_year`` is the assent year (LLP Act, 2008 -> 2009)."""
    if code in PINNED:
        return {"match": {"act_id": PINNED[code], "name": _act_name(PINNED[code], cache)},
                "score": 100.0, "candidates": 1}
    query = re.sub(r"[(),.]", " ", re.sub(r"\b(1[89]\d\d|20\d\d)\b", "", act)).strip()
    candidates: Dict[str, Dict[str, Any]] = {}
    for collection in ("ACT", "SECTION"):
        data = _get(
            [("query", query), ("f.identifier_collection", f"{collection},equals"),
             ("f.identifier_state", "CENTRAL,equals"), ("size", "40")],
            os.path.join(cache, "resolve", f"{code}.{collection}.json"),
        )
        for item in _objects(data):
            act_id = _md(item, "dc.identifier.act_id")
            if not act_id or not act_id.startswith("AC_CEN") or act_id in candidates:
                continue
            name = _md(item, "dc.identifier.act_name") or _md(item, "dc.title") or ""
            candidates[act_id] = {"act_id": act_id, "name": name}
    want_year = _year(act)
    best, best_score = None, 0.0
    for cand in candidates.values():
        score = fuzz.token_sort_ratio(_bare(act), _bare(cand["name"]))
        year = _year(cand["name"])
        if want_year and year and year != want_year:
            score -= 25  # a different year is a different (or amending) act
        if score > best_score:
            best, best_score = cand, score
    return {"match": best, "score": round(best_score, 1), "candidates": len(candidates)}


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _section_text(raw: Optional[str]) -> str:
    text = re.sub(r"<br\s*/?>|<hr[^>]*>|</p>|</li>", "\n", raw or "")
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _order(value: Optional[str]) -> float:
    """India Code's position of a section; inserted sections can be decimal ("46.1")."""
    try:
        return float(value or 0)
    except ValueError:
        return float("inf")


def fetch_sections(act_id: str, cache: str) -> List[Dict[str, Any]]:
    sections, page = [], 0
    while True:
        data = _get(
            [("f.act_id", f"{act_id},equals"), ("f.identifier_collection", "SECTION,equals"),
             ("sort", "dc.identifier.order_number,ASC"), ("size", str(PAGE_SIZE)), ("page", str(page))],
            os.path.join(cache, "acts", act_id, f"page_{page:03d}.json"),
        )
        items = _objects(data)
        for item in items:
            sections.append({
                "order": _order(_md(item, "dc.identifier.order_number")),
                "section": (_md(item, "dc.identifier.section_number") or "").strip(),
                "title": (_md(item, "dc.title") or "").strip(),
                "text": _section_text(_md(item, "dc.identifier.section_page_note")),
                "repealed": (_md(item, "dc.identifier.repealed") or "").lower() == "true",
            })
        page += 1
        if not items or page * PAGE_SIZE >= _total(data):
            break
    sections.sort(key=lambda s: s["order"])
    return sections


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

_OMITTED = re.compile(r"^\[?\s*(omitted|repealed)\b", re.I)


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    return re.sub(r"\s*\.+\s*$", "", title).strip()


def build(rows: List[Dict[str, Any]], cache: str, min_score: float, resolve_only: bool) -> Dict[str, Any]:
    acts: "OrderedDict[str, str]" = OrderedDict()
    exported: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        acts.setdefault(row["actCode"], row["act"])
        exported[row["actCode"]].append(row)

    out: List[Dict[str, Any]] = []
    report: Dict[str, Any] = {"acts": {}, "repealed": {}, "added_successors": {},
                              "unresolved": {}, "dropped": {}}
    used_ids: Dict[str, str] = {}

    def wrong_rows(code: str) -> bool:
        # The cleaner proved these rows belong to another act: never carry them over.
        return code in DROP_ACTS and code not in REPLACE_WITH

    def carry_export(code: str, act: str, extra: Dict[str, Any]) -> None:
        source_rows = exported[REPLACE_WITH[code]] if code in REPLACE_WITH else exported[code]
        for row in source_rows:
            title = _clean_title(row["title"])
            out.append({**row, "id": f"{code.lower()}-{row['section'].lower()}", "actCode": code,
                        "act": act, "title": title, "source": "export-unverified", **extra,
                        **({"omitted": True} if _OMITTED.match(title) else {})})

    def emit_india_code(code: str, act: str, act_id: str, extra: Dict[str, Any]) -> int:
        seen = set()
        for sec in fetch_sections(act_id, cache):
            number = sec["section"]
            if not number or number.lower() in seen:
                continue  # India Code repeats a number only for a duplicate record
            seen.add(number.lower())
            title = _clean_title(sec["title"])
            row = {"id": f"{code.lower()}-{number.lower()}", "actCode": code, "act": act,
                   "section": number, "title": title, "description": sec["text"],
                   "source": "indiacode", "sourceActId": act_id, **extra}
            if sec["repealed"] or _OMITTED.match(title):
                row["omitted"] = True
            out.append(row)
        return len(seen)

    for code, act in acts.items():
        if code in REPEALED:
            entry = {"act": act, "repealedBy": REPEALED[code], "rows": len(exported[code])}
            if wrong_rows(code):
                entry["exportRows"] = f"dropped - they were {DROP_ACTS[code]}"
            elif not resolve_only:
                carry_export(code, act, {"repealed": True, "repealedBy": REPEALED[code]})
            report["repealed"][code] = entry
            continue

        res = ({"match": None, "score": 0.0, "candidates": 0} if code in NOT_ON_INDIA_CODE
               else resolve(code, act, cache))
        match = res["match"]
        trusted = match is not None and res["score"] >= min_score
        if trusted and match["act_id"] in used_ids:
            # Two exported acts resolving to one India Code act = a name clash; keep the first.
            trusted = False
            res["clash_with"] = used_ids[match["act_id"]]
        entry = {"act": act, "score": res["score"],
                 "indiaCode": match["name"] if match else None,
                 "actId": match["act_id"] if match else None}

        if not trusted:
            reason = NOT_ON_INDIA_CODE.get(code) or (
                f"clashes with {res['clash_with']}" if res.get("clash_with") else "no confident India Code match")
            if wrong_rows(code):
                report["dropped"][code] = {**entry, "reason": reason, "exportRowsWere": DROP_ACTS[code]}
                continue
            report["unresolved"][code] = {**entry, "reason": reason, "rows": len(exported[code])}
            if not resolve_only:
                carry_export(code, act, {})
            continue

        used_ids[match["act_id"]] = code
        report["acts"][code] = entry
        if not resolve_only:
            entry["sections"] = emit_india_code(code, act, match["act_id"], {})
            entry["exportSections"] = len(exported[code])

    for code, (act, act_id, replaces) in SUCCESSORS.items():
        entry = {"act": act, "actId": act_id, "replaces": replaces}
        if not resolve_only:
            entry["sections"] = emit_india_code(code, act, act_id, {"replaces": acts.get(replaces, replaces)})
        report["added_successors"][code] = entry

    report["summary"] = {
        "input_acts": len(acts), "input_rows": len(rows),
        "from_india_code": len(report["acts"]), "repealed_flagged": len(report["repealed"]),
        "successors_added": len(report["added_successors"]),
        "unresolved_kept": len(report["unresolved"]), "dropped": len(report["dropped"]),
        "output_rows": len(out), "output_acts": len({r["actCode"] for r in out}),
        "rows_from_india_code": sum(1 for r in out if r["source"] == "indiacode"),
    }
    return {"rows": out, "report": report}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="master laws export (JSON list)")
    ap.add_argument("--out", help="output (default: <input dir>/masterLaws.indiacode.json)")
    ap.add_argument("--report", help="report (default: <input dir>/masterLaws.indiacode_report.json)")
    ap.add_argument("--cache", default=os.path.join("data", "laws", "indiacode_cache"),
                    help="response cache (default: data/laws/indiacode_cache)")
    ap.add_argument("--min-score", type=float, default=80.0, help="minimum act-name match score (0-100)")
    ap.add_argument("--resolve-only", action="store_true", help="match acts and write the report only")
    args = ap.parse_args()

    base = os.path.dirname(os.path.abspath(args.input))
    out_path = args.out or os.path.join(base, "masterLaws.indiacode.json")
    report_path = args.report or os.path.join(base, "masterLaws.indiacode_report.json")
    if os.path.abspath(out_path) == os.path.abspath(args.input):
        sys.exit("refusing to overwrite the input file")

    with open(args.input, encoding="utf-8") as fh:
        rows = json.load(fh)
    result = build(rows, args.cache, args.min_score, args.resolve_only)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(result["report"], fh, ensure_ascii=False, indent=2)
    if not args.resolve_only:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result["rows"], fh, ensure_ascii=False, indent=2)
        print(f"written: {out_path}")
    print(json.dumps(result["report"]["summary"], indent=2))
    print(f"report:  {report_path}")


if __name__ == "__main__":
    main()
