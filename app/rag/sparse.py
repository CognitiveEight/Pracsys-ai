"""
Legal-aware lexical (sparse) vectors for hybrid search.

Dense embeddings are good at meaning and bad at exact strings. A lawyer asking
about "section 498A", "CR-2026-45871" or "₹6,00,000" needs the passage that
contains that exact token, and a semantically similar passage about a different
section is a wrong answer. The sparse vector carries those tokens; Qdrant
applies IDF on the server (``Modifier.IDF``), so rare tokens — case numbers,
section numbers — dominate, and common words do not.

Tokenisation keeps legal identifiers whole *and* emits their parts:
``s.302`` → ``s.302``, ``302``; ``cr-2026-45871`` → the whole id plus
``2026``, ``45871``. Amounts are normalised by removing digit-group commas, so
``6,00,000`` and ``600000`` match.
"""

import math
import re
import zlib
from collections import Counter
from typing import Dict, List, Tuple

#: Size of the hashed token space. Collisions at this size are negligible for
#: per-workspace corpora.
_DIM = 2**31 - 1

_TOKEN_RE = re.compile(r"[0-9a-zऀ-ॿ]+(?:[./\-][0-9a-zऀ-ॿ]+)*")
_DIGIT_GROUP_RE = re.compile(r"(?<=\d),(?=\d)")
_STOPWORDS = frozenset(
    """a an and are as at be been but by for from had has have he her his i if in
    into is it its of on or our she so that the their them then there these they
    this to was we were what when which who will with you your shall may said such
    any all other been being do does did""".split()
)


def tokens(text: str) -> List[str]:
    """Normalised tokens, including the parts of compound identifiers."""
    text = _DIGIT_GROUP_RE.sub("", (text or "").lower())
    out: List[str] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        parts = re.split(r"[./\-]", tok)
        if len(parts) > 1:
            out.append(tok)
            out.extend(p for p in parts if p and p not in _STOPWORDS)
        elif tok not in _STOPWORDS and (len(tok) > 1 or tok.isdigit()):
            out.append(tok)
    return out


def _index(token: str) -> int:
    return zlib.crc32(token.encode("utf-8")) % _DIM


def _to_sparse(weights: Dict[int, float]) -> Tuple[List[int], List[float]]:
    items = sorted(weights.items())
    return [i for i, _ in items], [w for _, w in items]


def encode_document(text: str) -> Tuple[List[int], List[float]]:
    """Sparse vector for a stored chunk: log-scaled term frequency.

    Log scaling stops a token repeated 40 times in one chunk from outweighing
    a rarer, more specific one; IDF is applied by the server.
    """
    counts = Counter(tokens(text))
    weights: Dict[int, float] = {}
    for tok, tf in counts.items():
        idx = _index(tok)
        weights[idx] = weights.get(idx, 0.0) + 1.0 + math.log(tf)
    return _to_sparse(weights)


def encode_query(text: str) -> Tuple[List[int], List[float]]:
    """Sparse vector for a query: each distinct token weighted 1."""
    weights = {_index(tok): 1.0 for tok in set(tokens(text))}
    return _to_sparse(weights)
