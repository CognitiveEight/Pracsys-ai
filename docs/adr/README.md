# Architecture decision records

An ADR records one architectural decision: the problem, what was chosen, and
what follows from it. ADRs explain *why*. `CLAUDE.md` and the topic documents
in `docs/` say *how* the code works today; if they disagree with an ADR, the
code and `CLAUDE.md` are current and the ADR needs a superseding record.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-provider-neutral-model-router.md) | Provider-neutral model router behind `gemini_client` | Accepted (documented retroactively) |
| [0002](0002-gate0-enforce-only-model-path.md) | `GATE0_ENFORCE` makes Gate0 the only model path | Accepted (documented retroactively) |
| [0003](0003-socket-client-and-stop-fence.md) | Socket.IO client with stop-as-fence run scopes | Accepted (documented retroactively) |
| [0004](0004-workspace-deletion-is-compliance-erasure.md) | Workspace deletion is a compliance erasure | Accepted (documented retroactively) |

## When to write one

Write an ADR when a change:

- picks between real alternatives that a later reader would question;
- adds or removes an external dependency (provider, store, gateway);
- sets a rule that other code must follow (a fence, a chokepoint, a gate);
- reverses or narrows an earlier ADR.

A bug fix or a change that follows an existing rule does not need one.

## How to write one

1. Copy `0000-template.md` to `NNNN-short-title.md`, using the next free
   number. Numbers are never reused.
2. Fill Context, Decision, Consequences, Status and Sources. Keep it to
   facts: no case data, names, secrets or private URLs.
3. Status starts as `Proposed`. It becomes `Accepted` when the tech lead
   approves the PR that adds it.
4. Never rewrite an accepted ADR's decision. To change it, write a new ADR
   and set the old one's status to `Superseded by NNNN`.
5. Add a row to the index above in the same PR.
6. Link the ADR from the spec's technical design (`specs/technical-design/SIM-<n>-….md`) when a
   ticket makes or depends on the decision.

ADRs 0001-0004 were written after the fact, from decisions already made and
documented in `CLAUDE.md` and `docs/`. Their status says so, and they carry no
decision date because none is recorded.
