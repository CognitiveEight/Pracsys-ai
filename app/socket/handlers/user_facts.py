"""User-asserted fact corrections: the ledger, its prompt block, and the
deterministic substitution pass that enforces it.

When a lawyer corrects a fact in chat ("the disputed amount is 6 lakh USD, not
16 lakh"), two things have to happen or the correction does not stick:

1. Every panel that mentions the value has to be corrected now, and
2. the correction has to outlive the panels — otherwise the next document
   upload regenerates them from documents that still say 16 lakh and silently
   reverts it.

This mixin owns (2). It mirrors the REMOVED_DOCUMENTS contract in
``documents.py``: a persisted ledger, a prompt block that states the correction
authoritatively, and a deterministic Python pass that enforces the outcome
regardless of whether the model complied with the prompt.

Feature mixin composed into SocketEventHandler.
"""

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from loguru import logger

# Hard caps. The block is appended to every generator prompt, which already
# ships up to ~100k chars of document content, so it must not grow without
# bound.
MAX_USER_FACTS = 20
MAX_SUPERSEDED_PER_FACT = 5

# Fields a deterministic substitution is allowed to touch on a doc-derived
# panel. Deliberately narrow: `date` is an ISO field the FE parses, and
# `evidenceSnippet` is a VERBATIM quote from the evidence — rewriting either
# would falsify the record, which for a legal product is worse than leaving a
# stale figure in place.
FACT_SUBSTITUTION_FIELDS = ("description", "title", "event", "summary", "text")

_UNIT_FOLD = {
    "lakhs": "lakh", "lac": "lakh", "lacs": "lakh", "lakh": "lakh",
    "crores": "crore", "cr": "crore", "crore": "crore",
}
_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def _slug_slot(description: str) -> str:
    """Normalise a fact description into a stable dedupe/supersede key."""
    text = _ARTICLE_RE.sub("", (description or "").strip().lower())
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return _WS_RE.sub(" ", text).strip()


def _normalise_value(value: str) -> str:
    """Fold a value so ``16 lakh`` / ``16 Lakhs`` / ``16,00,000`` compare equal."""
    text = (value or "").strip().lower()
    text = re.sub(r"[₹$,]", "", text)
    text = re.sub(r"\b(?:rs\.?|inr|usd)\b", "", text)
    text = _WS_RE.sub(" ", text).strip()
    parts = text.split()
    parts = [_UNIT_FOLD.get(p, p) for p in parts]
    return " ".join(parts).strip()


def _numeric_variants(value: str) -> List[str]:
    """Literal spellings of a value that a panel might actually contain.

    ``16 lakh`` also appears as ``16,00,000``, ``1600000`` and ``16 lakhs``, so a
    substitution keyed on the user's exact wording alone would miss most hits.
    """
    raw = (value or "").strip()
    if not raw:
        return []
    out = [raw]
    m = re.search(
        r"(\d[\d,]*(?:\.\d+)?)\s*(lakhs?|lacs?|crores?|cr)\b", raw, re.IGNORECASE
    )
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            return _dedupe(out)
        unit = _UNIT_FOLD.get(m.group(2).lower(), m.group(2).lower())
        total = int(amount * (100_000 if unit == "lakh" else 10_000_000))
        out.append(f"{total}")
        out.append(f"{total:,}")
        out.append(_indian_grouping(total))
        num = m.group(1)
        for unit_word in (("lakh", "lakhs", "lac", "lacs") if unit == "lakh"
                          else ("crore", "crores", "cr")):
            out.append(f"{num} {unit_word}")
    return _dedupe(out)


def _indian_grouping(number: int) -> str:
    """1600000 -> '16,00,000' (the notation Indian legal documents actually use)."""
    text = str(number)
    if len(text) <= 3:
        return text
    head, tail = text[:-3], text[-3:]
    head = re.sub(r"(?<=\d)(?=(\d\d)+$)", ",", head)
    return f"{head},{tail}"


def _dedupe(items: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for item in items:
        key = item.strip().lower()
        if item.strip() and key not in seen:
            seen.add(key)
            out.append(item.strip())
    return out


class UserFactsMixin:
    """Ledger of facts the lawyer corrected in chat, and its enforcement."""

    # ------------------------------------------------------------------ ledger

    def add_user_asserted_fact(
        self,
        workspace_id: str,
        *,
        now: str,
        description: str,
        was: str = "",
        source: str = "chat",
    ) -> Optional[Dict[str, Any]]:
        """Pin a corrected fact, superseding any earlier value for the same slot.

        Returns the stored entry, or None when the input is unusable.

        Supersession matters more than it looks: two entries for one slot leave
        the prompt asserting two contradictory values, and the model then picks
        one arbitrarily. So a repeat correction ("actually it's 8 lakh") REPLACES
        the previous entry and pushes the old value onto ``superseded`` — which
        the prompt then names explicitly as forbidden.
        """
        if not workspace_id:
            return None
        now_v = (now or "").strip()
        desc = (description or "").strip()
        if not now_v or not desc:
            return None
        was_v = (was or "").strip()

        ledger = list(self._workspace_user_facts.get(workspace_id) or [])
        ledger = [e for e in ledger if isinstance(e, dict)]
        slot = _slug_slot(desc)
        now_norm = _normalise_value(now_v)
        was_norm = _normalise_value(was_v)

        existing: Optional[Dict[str, Any]] = None
        for entry in ledger:
            if entry.get("slot") == slot:
                existing = entry
                break
            # "6 lakh -> 8 lakh": the new `was` is the old `now`, so this is the
            # same fact even when the user described it differently.
            if was_norm and _normalise_value(str(entry.get("now") or "")) == was_norm:
                existing = entry
                break

        if existing is not None:
            old_now = str(existing.get("now") or "").strip()
            superseded = [
                s for s in (existing.get("superseded") or []) if isinstance(s, str)
            ]
            if old_now and _normalise_value(old_now) != now_norm:
                superseded.append(old_now)
            existing["superseded"] = _dedupe(superseded)[-MAX_SUPERSEDED_PER_FACT:]
            existing["now"] = now_v
            existing["description"] = desc
            existing["slot"] = slot
            if was_v and not existing.get("was"):
                existing["was"] = was_v
            existing["assertedAt"] = datetime.utcnow().isoformat()
            existing["source"] = source
            entry_out = existing
        else:
            entry_out = {
                "id": f"fact_{len(ledger) + 1}",
                "slot": slot,
                "description": desc,
                "was": was_v,
                "now": now_v,
                "superseded": [],
                "assertedAt": datetime.utcnow().isoformat(),
                "source": source,
            }
            ledger.append(entry_out)

        # Newest first, capped — the prompt reads top-down.
        ledger.sort(key=lambda e: str(e.get("assertedAt") or ""), reverse=True)
        ledger = ledger[:MAX_USER_FACTS]
        for idx, entry in enumerate(ledger, start=1):
            entry["id"] = f"fact_{idx}"
        self._workspace_user_facts[workspace_id] = ledger
        logger.info(
            "Pinned user-asserted fact: {} = {!r} (was {!r}) — ledger now {} entry(ies)",
            desc,
            now_v,
            was_v or "unstated",
            len(ledger),
        )
        return entry_out

    def get_user_asserted_facts(self, workspace_id: str) -> List[Dict[str, Any]]:
        ledger = self._workspace_user_facts.get(workspace_id) or []
        return [e for e in ledger if isinstance(e, dict)] if isinstance(ledger, list) else []

    def clear_user_asserted_facts(self, workspace_id: str) -> None:
        """Drop the ledger. Called on workspace deletion — these entries hold
        verbatim case facts and must not outlive the workspace."""
        try:
            self._workspace_user_facts.pop(workspace_id, None)
        except Exception as e:  # pragma: no cover - best effort
            logger.debug("Clearing user-asserted facts failed (workspace={}): {}",
                         workspace_id, e)

    # ------------------------------------------------------------ prompt block

    def _build_user_facts_block(self, workspace_id: str) -> str:
        """Render the AUTHORITATIVE USER CORRECTIONS block, or "" when empty.

        Injected BEFORE the document content (the same placement as
        ``_build_removed_documents_block``) so it visibly outranks the material
        it contradicts. The wording is deliberately narrow: every generator
        prompt carries an anti-invention rule, and a blanket "override the
        documents" would negate it — this authorises substituting these specific
        values and nothing else.
        """
        facts = self.get_user_asserted_facts(workspace_id)
        if not facts:
            return ""
        lines: List[str] = []
        for entry in facts:
            desc = str(entry.get("description") or "").strip()
            now_v = str(entry.get("now") or "").strip()
            if not desc or not now_v:
                continue
            line = f"- {desc}: {now_v}"
            forbidden = _dedupe(
                [str(entry.get("was") or "")]
                + [str(s) for s in (entry.get("superseded") or [])]
            )
            if forbidden:
                line += ". This is NOT " + ", NOT ".join(forbidden)
            lines.append(line + ".")
        if not lines:
            return ""
        return (
            "AUTHORITATIVE USER CORRECTIONS (the lawyer corrected these facts "
            "directly; they outrank the documents):\n"
            + "\n".join(lines)
            + "\n\nCORRECTION RULES:\n"
            "- Where a source document states a superseded value, the document is "
            "out of date. Use the corrected value.\n"
            "- This authorises SUBSTITUTION OF THESE SPECIFIC VALUES ONLY. It does "
            "NOT authorise inventing any other fact, party, date, amount or "
            "statute — every other anti-invention rule in this prompt still "
            "applies in full.\n"
            "- Never reintroduce a superseded value anywhere in your output.\n"
            "- Entries are listed newest first; a later correction supersedes an "
            "earlier one for the same fact.\n\n"
        )

    def _with_user_facts(self, workspace_id: str, prompt: str) -> str:
        """Prepend the corrections block to a generator prompt.

        Placement is the point. Every generator prompt carries anti-invention
        rules in its body and ends with "Return ONLY ... JSON", so a trailing
        suffix would be both the weakest position for a constraint that must
        outrank the body AND appear after the output instruction. Leading the
        prompt puts the corrections ahead of the document content they
        contradict — the same reasoning as ``_build_removed_documents_block``,
        which is injected before STEP 2 "so it visibly outranks" what follows.

        No-ops (returns ``prompt`` unchanged) when the ledger is empty, so call
        sites need no guard.
        """
        try:
            block = self._build_user_facts_block(workspace_id)
        except Exception as e:  # pragma: no cover - never block generation
            logger.warning("Building user-facts block failed (non-fatal): {}", e)
            return prompt
        if not block:
            return prompt
        return block + (prompt or "")

    # -------------------------------------------------- deterministic enforcement

    def _apply_fact_corrections_to_payload(
        self,
        workspace_id: str,
        payload: Any,
        *,
        fields: Iterable[str] = FACT_SUBSTITUTION_FIELDS,
        label: str = "",
    ) -> int:
        """Replace superseded values with corrected ones, in place.

        The prompt block alone is a soft nudge across eight generators; this is
        what makes the guarantee real, exactly as
        ``_apply_removed_evidence_missing_info`` does for removed documents.

        Only the allowlisted ``fields`` are touched, so an ISO ``date`` or a
        verbatim ``evidenceSnippet`` is never rewritten. Returns the number of
        substitutions made.
        """
        facts = self.get_user_asserted_facts(workspace_id)
        if not facts or payload is None:
            return 0

        # (literal_to_replace, replacement) — longest first so "16,00,000" is
        # matched before "16".
        pairs: List[tuple] = []
        for entry in facts:
            now_v = str(entry.get("now") or "").strip()
            if not now_v:
                continue
            olds = _dedupe(
                [str(entry.get("was") or "")]
                + [str(s) for s in (entry.get("superseded") or [])]
            )
            for old in olds:
                for variant in _numeric_variants(old):
                    if _normalise_value(variant) == _normalise_value(now_v):
                        continue
                    pairs.append((variant, now_v))
        if not pairs:
            return 0
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        compiled = [
            (re.compile(re.escape(old), re.IGNORECASE), new) for old, new in pairs
        ]
        allowed = {f.lower() for f in fields}
        count = 0

        def _walk(node: Any, key: Optional[str] = None) -> Any:
            nonlocal count
            if isinstance(node, dict):
                for k, v in list(node.items()):
                    node[k] = _walk(v, k)
                return node
            if isinstance(node, list):
                return [_walk(v, key) for v in node]
            if isinstance(node, str) and key is not None and key.lower() in allowed:
                new_text = node
                for rx, replacement in compiled:
                    new_text, n = rx.subn(replacement, new_text)
                    count += n
                return new_text
            return node

        _walk(payload)
        if count:
            logger.info(
                "Applied {} fact substitution(s){} for workspace {}",
                count,
                f" to {label}" if label else "",
                workspace_id[:12] if workspace_id else "",
            )
        return count
