#!/usr/bin/env python3
"""
Clean the master laws export (``masterLaws*.json``) into a register we can trust.

The export (16,037 rows, 182 acts) had three defects, all fixed here:

1. **Section lists filed under the wrong act.** Whole acts were copies of
   another act's section list. Sometimes the copy was the *better* version of
   the real act, so each cluster keeps its most complete version under the
   act it actually is (``REPLACE_WITH``), and every act that only held another
   act's sections is dropped (``DROP_ACTS``). Dropped acts must be re-sourced;
   they are listed in the report.
2. **BNS mixed with IPC.** 44 BNS headings were IPC headings or shifted rows
   (e.g. "302 Murder", "303 Punishment for murder by life-convict"), and 9 rows
   were IPC-only sections (354A–354D, 376A–376DA). The BNS block is rebuilt
   from the official headings and text (devgan.in mirror of the gazetted
   Act; ``--bns-cache`` keeps the downloaded pages so re-runs are offline).
3. **Cosmetics.** Stray " ." at the end of titles; omitted/repealed sections
   are flagged ``"omitted": true`` so they are never cited as live law.

Every other row is passed through unchanged. Descriptions outside BNS are
still templated filler (they do not contain the provision's text), which the
report states. The input file is never modified.

USAGE
    python scripts/clean_master_laws.py "masterLaws 1.json"
    python scripts/clean_master_laws.py in.json --out cleaned.json --report report.json
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from typing import Any, Dict, List

# Act code -> act code whose rows are the correct, more complete version of it.
# Established by comparing section lists: e.g. the rows exported as PNGRB/MRO
# are the Companies Act 2013 (s.135 CSR, s.166 duties of directors, s.188
# related-party transactions), while the rows exported as CA are misaligned.
REPLACE_WITH = {
    "CA": "PNGRB",   # Companies Act, 2013
    "RA": "ODA",     # Registration Act, 1908 (adds 16A, 23A, 32A)
    "LLP": "IPA",    # LLP Act, 2008 (adds 34A, 67A-67C, 68A, 76A, 77A)
    "UAPA": "CLND",  # UAPA, 1967 (adds 18A, 18B, 22A-22C, 24A, 43A-43F, 51A, 53)
}

# Acts whose rows are another act's sections. Value = what the rows really are.
DROP_ACTS = {
    "PNGRB": "Companies Act, 2013 (moved to CA)",
    "MRO": "Companies Act, 2013 (duplicate)",
    "ODA": "Registration Act, 1908 (moved to RA)",
    "IPA": "LLP Act, 2008 (moved to LLP)",
    "CLND": "UAPA, 1967 (moved to UAPA)",
    "MMTG": "Motor Vehicles Act, 1988 (duplicate of MVA)",
    "IRDA": "company-law sections, not the IRDA Act",
    "DICGC": "company-law sections, not the DICGC Act (duplicate of IRDA rows)",
    "SARFAESI": "Central Excise Act, 1944 (duplicate of CEA)",
    "RDB": "Central Excise Act, 1944 (duplicate of CEA)",
    "TOB": "Central Excise Act, 1944 (duplicate of CEA)",
    "IRWA": "Government of Union Territories Act sections",
    "PCMCS": "Government of Union Territories Act sections",
    "DMR": "Government of Union Territories Act sections",
    "BENAMI": "Government of Union Territories Act sections",
    "WBP": "Government of Union Territories Act sections",
    "DEL": "Government of Union Territories Act sections",
    "NWA": "RPWD Act, 2016 (duplicate of RPWD)",
    "BCIR": "Advocates Act, 1961 (duplicate of ADV)",
    "WDR": "APMC Act sections (duplicate of APMC)",
    "SPB": "APMC Act sections (duplicate of APMC)",
    "BDA": "PMLA, 2002 (duplicate of PMLA)",
    "FOR": "an unidentified tribunal act, not the Foreigners Act",
    "PASS": "an unidentified tribunal act (duplicate of FOR rows)",
    "DDA": "Delhi Municipal Corporation Act sections, not the Delhi Development Act",
}

BNS_SOURCE = "https://devgan.in/bns/chapter_{:02d}.php"
BNS_CHAPTERS = 20
BNS_SECTIONS = 358
_BNS_HEADING = re.compile(
    r'<h2[^>]*>\s*Section\s*<a name="s([0-9A-Z]+)">[^<]*</a>\s*(?:&#8211;|–|-)\s*(.*?)</h2>'
    r"\s*(?:<div class=.sectxt.[^>]*>(.*?)</div>)?",
    re.S,
)
_OMITTED = re.compile(r"^\[?\s*(omitted|repealed)\b", re.I)


def _clean_html(fragment: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))
    return re.sub(r"\s+", " ", text).strip()


def _clean_title(title: str) -> str:
    return re.sub(r"\s+\.$", "", (title or "").strip()).strip()


def load_bns_reference(cache_dir: str) -> Dict[str, Dict[str, str]]:
    """Official BNS headings and text, section -> {title, text}."""
    os.makedirs(cache_dir, exist_ok=True)
    ref: Dict[str, Dict[str, str]] = {}
    for chapter in range(1, BNS_CHAPTERS + 1):
        path = os.path.join(cache_dir, f"bns_ch{chapter:02d}.html")
        if not os.path.exists(path):
            req = urllib.request.Request(BNS_SOURCE.format(chapter), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(path, "wb") as fh:
                fh.write(resp.read())
            time.sleep(0.5)  # be polite to the mirror
        with open(path, encoding="utf-8", errors="replace") as fh:
            page = fh.read()
        for number, heading, body in _BNS_HEADING.findall(page):
            ref[number] = {"title": _clean_html(heading).rstrip(".").strip(), "text": _clean_html(body)}
    missing = [str(n) for n in range(1, BNS_SECTIONS + 1) if str(n) not in ref]
    if missing or any(not v["text"] for v in ref.values()):
        raise SystemExit(f"BNS reference incomplete (missing sections: {missing[:10]}); refusing to rebuild BNS")
    return ref


def _section_key(section: str):
    m = re.match(r"(\d+)(.*)", section or "")
    return (int(m.group(1)), m.group(2)) if m else (10**9, section or "")


def clean(rows: List[Dict[str, Any]], bns_ref: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    by_act: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_act[row["actCode"]].append(row)

    out: List[Dict[str, Any]] = []
    report: Dict[str, Any] = {"input_rows": len(rows), "input_acts": len(by_act)}

    # 1. Acts rebuilt from the cluster member that really holds them.
    replaced = {}
    for code, source in REPLACE_WITH.items():
        keep = by_act[code][0]  # act name/code stay the real act's
        rebuilt = []
        for row in by_act[source]:
            # The templated description names the act it was filed under.
            description = (row.get("description") or "").replace(row["act"], keep["act"])
            rebuilt.append({**row, "id": f"{code.lower()}-{row['section'].lower()}",
                            "actCode": code, "act": keep["act"], "description": description})
        replaced[code] = {"from": source, "rows_before": len(by_act[code]), "rows_after": len(rebuilt)}
        by_act[code] = rebuilt
    report["replaced_from_better_copy"] = replaced

    # 2. BNS rebuilt from the official text.
    old_bns = {r["section"]: r for r in by_act["BNS"]}
    template = next(iter(old_bns.values()))
    changed = [s for s, r in old_bns.items()
               if s in bns_ref and _norm(r["title"]) != _norm(bns_ref[s]["title"])]
    # "Wrong" = a different provision, not a rewording: under half the
    # official heading's words appear in the exported one.
    wrong = [s for s in changed if not _same_heading(old_bns[s]["title"], bns_ref[s]["title"])]
    not_in_bns = sorted((s for s in old_bns if s not in bns_ref), key=_section_key)
    by_act["BNS"] = [
        {"id": f"bns-{s}", "actCode": "BNS", "act": template["act"], "section": s,
         "title": bns_ref[s]["title"], "description": bns_ref[s]["text"]}
        for s in sorted(bns_ref, key=_section_key)
    ]
    report["bns"] = {
        "rows_before": len(old_bns), "rows_after": len(by_act["BNS"]),
        "headings_changed": len(changed), "headings_wrong_provision": sorted(wrong, key=_section_key),
        "removed_non_bns_sections": not_in_bns,
        "descriptions": "official section text",
    }

    # 3. Drop acts that only held another act's sections.
    report["dropped_acts"] = {
        code: {"act": by_act[code][0]["act"], "rows": len(by_act[code]), "actually": why}
        for code, why in DROP_ACTS.items() if code in by_act
    }
    for code in DROP_ACTS:
        by_act.pop(code, None)

    # 4. Cosmetics + omitted flag, then emit in a stable order.
    titles_fixed = omitted = 0
    for code in sorted(by_act):
        for row in sorted(by_act[code], key=lambda r: _section_key(r["section"])):
            row = dict(row)
            title = _clean_title(row["title"])
            titles_fixed += title != row["title"]
            row["title"] = title
            if _OMITTED.match(title):
                row["omitted"] = True
                omitted += 1
            out.append(row)

    ids = Counter(r["id"] for r in out)
    pairs = Counter((r["actCode"], r["section"]) for r in out)
    if any(n > 1 for n in ids.values()) or any(n > 1 for n in pairs.values()):
        raise SystemExit("cleaned output has duplicate ids or act+section pairs")

    report.update({
        "output_rows": len(out), "output_acts": len({r["actCode"] for r in out}),
        "titles_trailing_dot_fixed": titles_fixed, "omitted_flagged": omitted,
        "residual_cross_copies": _cross_copies(out),
        "note": "Descriptions outside BNS are templated summaries, not statutory text. "
                "Dropped acts must be re-sourced before they can be added back.",
    })
    return {"rows": out, "report": report}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _words(text: str) -> set:
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split() if len(w) > 2}


def _same_heading(exported: str, official: str) -> bool:
    want = _words(official)
    return bool(want) and len(want & _words(exported)) * 2 >= len(want)


def _cross_copies(rows: List[Dict[str, Any]]) -> List[str]:
    """Any pair of acts still sharing >= 10 identical (section, title) rows.
    Omitted/repealed rows are ignored: two acts can genuinely both have
    "Repealed" at the same section numbers."""
    sets: Dict[str, set] = defaultdict(set)
    for r in rows:
        if not r.get("omitted"):
            sets[r["actCode"]].add((r["section"], _norm(r["title"])))
    codes, found = sorted(sets), []
    for i, a in enumerate(codes):
        for b in codes[i + 1:]:
            if len(sets[a] & sets[b]) >= 10:
                found.append(f"{a}<->{b}")
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="master laws export (JSON list)")
    ap.add_argument("--out", help="cleaned output (default: <input dir>/masterLaws.cleaned.json)")
    ap.add_argument("--report", help="cleaning report (default: <input dir>/masterLaws.cleaning_report.json)")
    ap.add_argument("--bns-cache", default=os.path.join("data", "laws", "bns_source"),
                    help="where the downloaded BNS pages are kept (default: data/laws/bns_source)")
    args = ap.parse_args()

    base = os.path.dirname(os.path.abspath(args.input))
    out_path = args.out or os.path.join(base, "masterLaws.cleaned.json")
    report_path = args.report or os.path.join(base, "masterLaws.cleaning_report.json")
    if os.path.abspath(out_path) == os.path.abspath(args.input):
        sys.exit("refusing to overwrite the input file")

    with open(args.input, encoding="utf-8") as fh:
        rows = json.load(fh)
    result = clean(rows, load_bns_reference(args.bns_cache))
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result["rows"], fh, ensure_ascii=False, indent=2)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(result["report"], fh, ensure_ascii=False, indent=2)

    r = result["report"]
    print(f"{r['input_rows']} rows / {r['input_acts']} acts -> {r['output_rows']} rows / {r['output_acts']} acts")
    print(f"BNS: {r['bns']['headings_changed']} headings replaced "
          f"({len(r['bns']['headings_wrong_provision'])} named a different provision), "
          f"{len(r['bns']['removed_non_bns_sections'])} non-BNS sections removed")
    print(f"dropped {len(r['dropped_acts'])} mislabelled acts; residual cross-copies: {r['residual_cross_copies'] or 'none'}")
    print(f"written: {out_path}\nreport:  {report_path}")


if __name__ == "__main__":
    main()
