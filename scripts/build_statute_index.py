#!/usr/bin/env python3
"""
Build the statute section index the applicable-laws check uses
(``app/statutes/data/sections.json``) from the India Code rebuild.

Input is the output of ``scripts/fetch_laws_indiacode.py``: the laws file and
its report. Only rows fetched from India Code go in, because only those acts
are complete — that completeness is what lets the register call a section
outside them ``invalid``. Repealed acts are listed by name with the act that
repealed them, without sections.

The index is compact on purpose (number -> heading, plus an omitted flag): the
full statutory text stays in the laws file, which is too large to ship in the
image.

USAGE
    python scripts/build_statute_index.py masterLaws.indiacode.json masterLaws.indiacode_report.json
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.statutes.register import section_key  # noqa: E402

DEFAULT_OUT = os.path.join("app", "statutes", "data", "sections.json")
_RANGE = re.compile(r"^(\d+)\s*to\s*(\d+)$", re.I)


def build(rows, report) -> Dict[str, Any]:
    acts: Dict[str, Dict[str, Any]] = {}
    sections: Dict[str, Dict[str, Any]] = defaultdict(dict)
    for row in rows:
        if row.get("source") != "indiacode":
            continue
        code = row["actCode"]
        acts.setdefault(code, {"name": row["act"], "sourceActId": row.get("sourceActId")})
        entry = {"h": row["title"]}
        if row.get("omitted"):
            entry["o"] = 1
        span = _RANGE.match(row["section"].strip())
        numbers = ([str(n) for n in range(int(span.group(1)), int(span.group(2)) + 1)]
                   if span else [row["section"]])
        for number in numbers:
            key = section_key(number)
            if key and key not in sections[code]:
                sections[code][key] = entry
    for code, body in acts.items():
        body["sections"] = sections[code]

    repealed = {code: {"name": e["act"], "repealedBy": e["repealedBy"]}
                for code, e in (report.get("repealed") or {}).items()}
    return {
        "index_version": dt.date.today().isoformat(),
        "source": "India Code (indiacode.gov.in), via scripts/fetch_laws_indiacode.py",
        "acts": dict(sorted(acts.items())),
        "repealed": dict(sorted(repealed.items())),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("laws", help="masterLaws.indiacode.json")
    ap.add_argument("report", help="masterLaws.indiacode_report.json")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"index path (default: {DEFAULT_OUT})")
    args = ap.parse_args()

    with open(args.laws, encoding="utf-8") as fh:
        rows = json.load(fh)
    with open(args.report, encoding="utf-8") as fh:
        report = json.load(fh)
    index = build(rows, report)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))
    total = sum(len(a["sections"]) for a in index["acts"].values())
    print(f"{len(index['acts'])} acts, {total} sections, {len(index['repealed'])} repealed acts -> {args.out}")


if __name__ == "__main__":
    main()
