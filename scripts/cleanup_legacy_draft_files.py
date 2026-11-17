"""Delete the pre-SIM-211 draft Word files (one-off; dry run by default).

Before SIM-211, every generated draft was uploaded to
``draft/<userId>/<32 hex>-<title>.docx`` in the drafts bucket with a signed
link valid until 2050. Those links cannot be revoked, the workspace purge
could not find the files, and the title in the name can identify the parties.
Deleting the objects is the only way to stop the links working.

Usage::

    python scripts/cleanup_legacy_draft_files.py             # dry run: list only
    python scripts/cleanup_legacy_draft_files.py --execute   # delete

Rules:
- Run only after the SIM-211 code is deployed, so nothing writes under
  ``draft/`` any more.
- Only objects matching the legacy pattern are deleted. Anything else under
  ``draft/`` is reported and left alone.
- The production ``--execute`` run needs explicit sign-off and is run by a
  person. The output never prints object names (they contain draft titles),
  only counts.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.storage import gcs  # noqa: E402
from app.storage.draft_files import LEGACY_DRAFT_PREFIX, draft_bucket  # noqa: E402

LEGACY_NAME = re.compile(
    rf"^{LEGACY_DRAFT_PREFIX}/[^/]+/[0-9a-f]{{32}}-[^/]*\.docx$"
)


def classify(names: list[str]) -> tuple[list[str], list[str]]:
    """Split object names into (legacy draft files, anything else)."""
    legacy = [n for n in names if LEGACY_NAME.match(n)]
    other = [n for n in names if not LEGACY_NAME.match(n)]
    return legacy, other


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--execute", action="store_true", help="delete (default: dry run)"
    )
    parser.add_argument(
        "--bucket", help="bucket to clean (default: GCS_DOCX_BUCKET_NAME)"
    )
    args = parser.parse_args(argv)

    bucket = args.bucket or draft_bucket()
    if not bucket:
        print("No bucket: set GCS_DOCX_BUCKET_NAME or pass --bucket.")
        return 2

    names = gcs.list_blob_names(bucket, f"{LEGACY_DRAFT_PREFIX}/")
    legacy, other = classify(names)
    print(f"Under {LEGACY_DRAFT_PREFIX}/: {len(names)} object(s)")
    print(f"  legacy draft files (would delete): {len(legacy)}")
    print(f"  not matching the legacy pattern (left alone): {len(other)}")

    if not args.execute:
        print("Dry run: nothing deleted. Re-run with --execute after sign-off.")
        return 0

    failed = 0
    for name in legacy:
        try:
            gcs.delete_blob_strict(bucket, name)
        except Exception as exc:  # report and continue; summary below
            failed += 1
            print(f"  delete failed ({type(exc).__name__})")
    print(f"Deleted {len(legacy) - failed} of {len(legacy)}; {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
