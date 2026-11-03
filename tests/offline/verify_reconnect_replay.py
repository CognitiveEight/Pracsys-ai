#!/usr/bin/env python3
"""
verify_reconnect_replay.py — Offline checks for the reconnect replay de-dup.

After a socket reconnect the service replays the cached panels of every
recently-armed workspace. A panel the backend already had was sent again, and
the frontend rendered the re-sent ``strategy_objective`` as a second chat
message. ``_emit_document_feature(..., replay=True)`` now skips a payload whose
identical copy was handed to the socket more than
``REPLAY_DELIVERY_GRACE_SECONDS`` before the last disconnect. These checks pin:

  * an unchanged, long-delivered panel is not re-sent, and the skip is logged;
  * a changed panel, a never-delivered panel, a panel delivered inside the grace
    window and a panel with no delivery record (restart) are still replayed;
  * a live (non-replay) emit is never skipped;
  * the full ``_reemit_cached_features`` path sends nothing it already sent;
  * a workspace purge clears the delivery records.

Socket, persistence and citations are stubbed. No API key, no backend, no
Qdrant. Fictional data only.

USAGE
    python tests/offline/verify_reconnect_replay.py            # run everything
    python tests/offline/verify_reconnect_replay.py --list     # show the group names
    python tests/offline/verify_reconnect_replay.py --only replay_skips_unchanged
"""

import argparse
import asyncio
import copy
import os
import sys
import time
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

from app.core.config import settings  # noqa: E402
from app.socket.events import event_handler as H  # noqa: E402
from app.socket.handlers.chat import REPLAY_DELIVERY_GRACE_SECONDS as GRACE  # noqa: E402

RESULTS = []
EMITS = []          # (event, envelope)
LOGS = []           # message text
RUN_ID = uuid.uuid4().hex[:8]
OWNER = "68b0e1f8d3a9c4b2e1f03456"


def _ws(name):
    return f"{name}_{RUN_ID}"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (f"\n        {detail}" if detail and not ok else ""))


class _FakeSocket:
    """Records emits; ``deliver`` decides what ``emit`` reports."""

    def __init__(self):
        self.deliver = True

    async def emit(self, event, data):
        if self.deliver:
            EMITS.append((event, copy.deepcopy(data)))
        return self.deliver

    async def emit_ai_response(self, data):
        return self.deliver


SOCKET = _FakeSocket()


def _sink(message):
    LOGS.append(message.record["message"])


def _sent(ws, feature=None):
    return [
        env for ev, env in EMITS
        if ev == "document_feature_response"
        and env.get("workspaceId") == ws
        and (feature is None or env.get("featureType") == feature)
    ]


def _strategy(note="Fictional objective for a sample matter."):
    return {
        "workspaceId": "x",
        "caseSummary": "A fictional contract dispute between Party A and Party B.",
        "strategicObjective": note,
        "possibleNextSteps": ["Step one", "Step two"],
    }


async def _emit(ws, payload, *, replay=False, feature="strategy_objective"):
    return await H._emit_document_feature(
        feature_type=feature,
        workspace_id=ws,
        user_id=OWNER,
        payload=payload,
        usage=None,
        replay=replay,
    )


def _backdate(ws, feature, seconds_before_disconnect):
    """Make the recorded delivery look ``seconds_before_disconnect`` older than the drop."""
    key = f"{ws}:{feature}"
    fp, _ = H._delivered_feature_fingerprints[key]
    H._last_disconnect_at = time.time()
    H._delivered_feature_fingerprints[key] = (fp, H._last_disconnect_at - seconds_before_disconnect)


# ------------------------------------------------------------------ groups

async def replay_skips_unchanged():
    ws = _ws("unchanged")
    payload = _strategy()
    ok = await _emit(ws, payload)
    check("a live emit is delivered and recorded",
          ok and len(_sent(ws)) == 1 and f"{ws}:strategy_objective" in H._delivered_feature_fingerprints)
    _backdate(ws, "strategy_objective", GRACE + 200)
    before = len(LOGS)
    ok = await _emit(ws, copy.deepcopy(payload), replay=True)
    check("the replay reports delivered", ok is True)
    check("the replay sends nothing", len(_sent(ws)) == 1, f"sent={len(_sent(ws))}")
    check("the skip is logged",
          any("replay skipped" in m for m in LOGS[before:]))


async def replay_sends_changed():
    ws = _ws("changed")
    await _emit(ws, _strategy())
    _backdate(ws, "strategy_objective", GRACE + 200)
    await _emit(ws, _strategy("A different fictional objective."), replay=True)
    check("a changed payload is replayed", len(_sent(ws)) == 2, f"sent={len(_sent(ws))}")


async def replay_sends_inside_grace():
    ws = _ws("grace")
    await _emit(ws, _strategy())
    _backdate(ws, "strategy_objective", GRACE / 2)
    await _emit(ws, _strategy(), replay=True)
    check("a delivery inside the grace window is replayed",
          len(_sent(ws)) == 2, f"sent={len(_sent(ws))}")


async def replay_sends_never_delivered():
    ws = _ws("undelivered")
    SOCKET.deliver = False
    try:
        ok = await _emit(ws, _strategy())
    finally:
        SOCKET.deliver = True
    check("an undelivered emit reports not delivered", ok is False)
    check("an undelivered emit leaves no delivery record",
          f"{ws}:strategy_objective" not in H._delivered_feature_fingerprints)
    H._last_disconnect_at = time.time()
    await _emit(ws, _strategy(), replay=True)
    check("the replay delivers it", len(_sent(ws)) == 1, f"sent={len(_sent(ws))}")


async def replay_sends_without_record():
    ws = _ws("restart")
    await _emit(ws, _strategy())
    H._delivered_feature_fingerprints.pop(f"{ws}:strategy_objective", None)  # a restart
    H._last_disconnect_at = time.time()
    await _emit(ws, _strategy(), replay=True)
    check("with no delivery record (restart) the panel is replayed",
          len(_sent(ws)) == 2, f"sent={len(_sent(ws))}")


async def live_emit_never_skipped():
    ws = _ws("live")
    await _emit(ws, _strategy())
    _backdate(ws, "strategy_objective", GRACE + 200)
    await _emit(ws, _strategy())
    check("an identical live (non-replay) emit still goes out",
          len(_sent(ws)) == 2, f"sent={len(_sent(ws))}")


async def reemit_path_skips_delivered():
    ws = _ws("reemit")
    strategy = _strategy()
    dates = [{"date": "2025-03-04", "event": "Fictional notice sent"}]
    H._workspace_strategy_payload[ws] = strategy
    H._workspace_list_of_dates_payload[ws] = dates
    await _emit(ws, strategy)
    await _emit(ws, {"workspaceId": ws, "userId": OWNER, "listOfDates": dates},
                feature="list_of_dates")
    _backdate(ws, "strategy_objective", GRACE + 200)
    _backdate(ws, "list_of_dates", GRACE + 200)
    before = len(_sent(ws))
    await H._reemit_cached_features(ws, OWNER)
    check("the reconnect replay re-sends no delivered panel",
          len(_sent(ws)) == before, f"new={len(_sent(ws)) - before}")

    H._workspace_strategy_payload[ws] = _strategy("An updated fictional objective.")
    await H._reemit_cached_features(ws, OWNER)
    new = _sent(ws)[before:]
    check("a panel changed since delivery is replayed, and only that one",
          [e["featureType"] for e in new] == ["strategy_objective"],
          f"new={[e['featureType'] for e in new]}")


async def purge_clears_records():
    ws = _ws("purge")
    await _emit(ws, _strategy())
    check("a record exists before the purge",
          f"{ws}:strategy_objective" in H._delivered_feature_fingerprints)
    H._clear_workspace_memory(ws)
    check("the purge sweep clears the delivery records",
          not any(k.startswith(f"{ws}:") for k in H._delivered_feature_fingerprints))


CHECKS = [
    ("replay_skips_unchanged", replay_skips_unchanged),
    ("replay_sends_changed", replay_sends_changed),
    ("replay_sends_inside_grace", replay_sends_inside_grace),
    ("replay_sends_never_delivered", replay_sends_never_delivered),
    ("replay_sends_without_record", replay_sends_without_record),
    ("live_emit_never_skipped", live_emit_never_skipped),
    ("reemit_path_skips_delivered", reemit_path_skips_delivered),
    ("purge_clears_records", purge_clears_records),
]


async def _run(only):
    saved_citations = settings.citations_enabled
    settings.citations_enabled = False
    H._socket_client = SOCKET
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
        settings.citations_enabled = saved_citations


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
    logger.add(_sink, level="DEBUG")
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
